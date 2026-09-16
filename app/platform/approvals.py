"""待决闸门请求的队列 + 送达（统一 broker）；**本模块不做判定**。

位置：`app/platform/approvals.py`（从 app/tui/approval_inbox.py 平移；2026-09-10 基座/前端
分家后归基座——闸门队列与送达是 run 生命周期的事，与终端显示无关）。

职责边界（对齐 docs/MULTI_AGENT.md §6）：判定"要不要放行某动作"单点在**图的审核节点**
（ReviewNode + interrupt）——人答 y/n 只发生在图的审核节点，经 interrupt→Command(resume)
语义；"这段回答够不够格当作选项被选中"也判在那边（`_normalize_ask_answer`）。本模块只承接
"待决请求的排队 + 决定的送达"，是纯队列/收件箱，不是判定权威。

**统一 broker 语义（2026-09-07 起）**：本地主 agent 的 graph interrupt 与远端 worker 的
HTTP 审批请求**同入这一个队列**——差异只在 payload 的 `worker_id` 标记（本地="main"、
远端=真实 worker id），决定路径一致：
- 本地 park：agent turn 驱动 `enqueue(payload)` 拿 id → `await wait(id)` 挂起
  （**阻塞 run、不阻塞进程**）；审核方 `complete(id, decision)` → wait 唤醒 → 以
  `Command(resume=…)` 续跑；
- 远端 worker：HTTP POST /requests 入队 → 审核方 complete → worker 长轮询
  GET /requests/{id}?block=1 取回决定。

**队列不关心闸门的种类**：载荷的 `type`（审批 / 提问）只影响前端怎么问与人答什么，
回填一律是 `Decision`（`app/schema/approval_schema.py`）。HTTP 面**只承载审批**——
worker 只会审批，提问在它那侧结构性不可达（`app/agent/tools.py::worker_tools`）。

"谁来问人、怎么问"由前端定：`drain_approvals` 把每条待决请求交 `UI.decide`，它按载荷的
`type` 选面板（终端：审批 = y/n 面板，提问 = 选项 + 补充两段），本模块只负责排队、回填与唤醒。

- `ApprovalInbox`：纯异步、无网络/UI 的请求队列。enqueue 登记待审 → 等待者 wait 阻塞到
  审核方 complete(id, approved) 回填决定才返回。complete 只做记录 + 唤醒，不产生决定。
- `ApprovalInboxServer`：薄 HTTP 面（starlette app + uvicorn，在**已运行的事件循环**里以
  任务方式跑，不阻塞调用方）。worker 用 httpx POST /requests 入队、GET /requests/{id}
  ?block=1 长轮询等决定、审核方 POST /requests/{id}/decision 回填。仅绑 127.0.0.1、无鉴权。

schema 的 `ApprovalRequest/ApprovalRecord/ApprovalStatus`（app/schema/approval_schema.py）
仍描述"审批请求"，保持不变（payload 语义未变；每条待审请求即一条审批请求，id 落在
ApprovalRecord.approval_id）。

本模块不 import `app.agent.*` → 测试无需 .env。
"""
from __future__ import annotations

import asyncio
import itertools
import os
import socket
from collections import deque
from dataclasses import dataclass, field

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.exception import ConfigError
from app.platform.ui import UI
from app.schema.approval_schema import (
    DEFAULT_INBOX_HOST,
    DEFAULT_INBOX_PORT,
    GATE_ASK_USER,
    GATE_TOOL_APPROVAL,
    INBOX_BLOCK_SECONDS,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalStatus,
    Decision,
)

# 本地主 agent 中断入 broker 时用的 worker_id 标记（远端 worker 用真实 id）
LOCAL_WORKER_ID = "main"

# 已回填记录保留的条数上限（见 ApprovalInbox.complete）：够长到"刚拿到决定再查一次状态"
# 一定命中，又足够小到长会话不会无界堆积。数很小，不按时间做（时间要靠定时器或惰性清理，
# 为这点内存不值得）。
_DECIDED_HISTORY = 256


@dataclass
class _Pending:
    """一条待审请求的**运行时**记录：schema 的纯数据 ApprovalRecord + 等待回填的 Future。

    含 asyncio.Future（不可序列化），故不进 app/schema——那里只放纯结构。
    """

    record: ApprovalRecord
    decision: asyncio.Future = field(default_factory=asyncio.Future)


class ApprovalInbox:
    """本地待审请求队列：登记 / 查询 / 回填 / 等待回填，全异步无网络。

    本类不产生审批决定——approved 由审核方（图的审核节点，或人审入口）给出后经 complete
    回填，这里只负责排队 + 唤醒等待者。
    """

    def __init__(self) -> None:
        self._items: dict[str, _Pending] = {}
        self._seq = itertools.count(1)
        # 已回填记录按**回填顺序**排队，用于限长回收（见 complete）——deque 是为了 popleft O(1)
        self._decided_order: deque[str] = deque()
        # 有新请求置位（idle 循环用事件竞争唤醒）；观察方处理后自行 clear。
        self.new_pending = asyncio.Event()

    def enqueue(self, payload: ApprovalRequest) -> str:
        """登记一条待审请求（payload 为 schema 的 ApprovalRequest），返回其 approval_id。"""
        approval_id = f"appr-{next(self._seq)}"
        record: ApprovalRecord = {
            "approval_id": approval_id,
            "payload": dict(payload),
            "decided": None,
        }
        self._items[approval_id] = _Pending(record=record)
        self.new_pending.set()
        return approval_id

    def pending(self) -> list[ApprovalRecord]:
        """未回填的请求（按登记序，ApprovalRecord），供人审 UI 渲染。"""
        return [
            item.record for item in self._items.values() if item.record["decided"] is None
        ]

    def status(self, approval_id: str) -> ApprovalStatus | None:
        """某条请求的状态；未知 id → None。

        `approved` 是**审批的 bool 投影**：HTTP 长轮询只有 worker 在用，而 worker 只会审批
        （提问在 worker 侧结构性不可达，见 app/agent/tools.py::worker_tools）——所以这里不随
        Decision 一起泛化，未决与提问都投影成 None。
        """
        item = self._items.get(approval_id)
        if item is None:
            return None
        decided = item.record["decided"]
        return {
            "status": "decided" if decided is not None else "pending",
            "approved": decided.approved if decided is not None else None,
        }

    def complete(self, approval_id: str, decision: Decision) -> bool:
        """审核方回填一条请求的决定并唤醒其等待者。幂等：未知/已回填返回 False。

        只做记录 + 唤醒，不做判定——决定由调用方（人审入口 / worker 的 HTTP 回包）给出。
        """
        item = self._items.get(approval_id)
        if item is None or item.record["decided"] is not None:
            return False
        item.record["decided"] = decision
        if not item.decision.done():
            item.decision.set_result(decision)
        # 已回填的记录**保留一段历史**再回收：worker 拿到决定后可能再查一次状态（状态查询
        # 也有非阻塞形态），所以不能一回填就删。但若永不回收，长会话里 _items 会无界增长、
        # 而 pending() 每次都要全表扫描（2026-09-15 审计发现）。按回填顺序保留最近
        # _DECIDED_HISTORY 条，更早的丢弃——未回填的条目**不参与回收**。
        self._decided_order.append(approval_id)
        while len(self._decided_order) > _DECIDED_HISTORY:
            self._items.pop(self._decided_order.popleft(), None)
        return True

    async def wait(self, approval_id: str, timeout: float | None = None) -> Decision:
        """阻塞到该请求被 complete；返回人的决定。超时抛 asyncio.TimeoutError。"""
        item = self._items.get(approval_id)
        if item is None:
            raise KeyError(approval_id)
        if item.record["decided"] is not None:
            return item.record["decided"]
        if timeout is not None:
            return await asyncio.wait_for(asyncio.shield(item.decision), timeout)
        return await item.decision

    def deny_pending(self) -> int:
        """把**仍未回填**的请求一律按"没人回答"回填，返回处理条数（收尾用）。

        进程要退出了，这些面板不会再有人回答。不这么做的话，远端 worker 会一直等到自己的
        超时才失败，还会把"没人应答"记成网络错误——立刻回一个决定更准确，也让 worker 当场
        收尾。两种闸门各有自己的 fail-closed 语义（见 `app/platform/ui.py::UI.decide`）：
        **审批 = 不放行**，**提问 = 未回答**（模型据此带假设继续，而不是被当成"用户否决"）。
        """
        undecided = [
            item for item in self._items.values() if item.record["decided"] is None
        ]
        for item in undecided:
            asked = (item.record["payload"] or {}).get("type") == GATE_ASK_USER
            decision = Decision(kind="answer") if asked else Decision(kind="approval", approved=False)
            self.complete(item.record["approval_id"], decision)
        return len(undecided)

    def clear_new(self) -> None:
        """观察方已处理完待批后清事件（避免下轮空转）。"""
        self.new_pending.clear()


# ------------------------- 薄 HTTP 面 -------------------------


def _bad_request(reason: str) -> JSONResponse:
    return JSONResponse({"error": reason}, status_code=400)


def build_app(queue: ApprovalInbox) -> Starlette:
    """把队列包成 starlette 应用（仅本机调试用，无鉴权）。"""

    async def enqueue_request(request: Request):
        try:
            body = await request.json()
        except Exception:
            return _bad_request("请求体必须是 JSON")
        if not isinstance(body, dict):
            return _bad_request("请求体必须是 JSON 对象")
        tool_name = body.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name.strip():
            return _bad_request("缺少 tool_name")
        worker_id = body.get("worker_id")
        if not isinstance(worker_id, str) or not worker_id.strip():
            return _bad_request("缺少 worker_id")
        # 只保留已知字段，避免把意外键带进后续渲染/回包
        payload: ApprovalRequest = {
            "worker_id": worker_id,
            "tool_name": tool_name,
            "tool_args": body.get("tool_args") if isinstance(body.get("tool_args"), dict) else {},
            "description": body.get("description"),
        }
        # worker 派给自己的那条子任务原文（可选；本地中断不带）：审批面板靠它给出上下文
        if isinstance(body.get("subtask"), str) and body["subtask"].strip():
            payload["subtask"] = body["subtask"]
        approval_id = queue.enqueue(payload)
        return JSONResponse({"approval_id": approval_id, "status": "pending"})

    async def get_request(request: Request):
        approval_id = request.path_params["approval_id"]
        if queue.status(approval_id) is None:
            return JSONResponse({"error": "请求不存在"}, status_code=404)
        block = request.query_params.get("block") in ("1", "true", "True")
        if not block:
            return JSONResponse(queue.status(approval_id))
        try:
            # 唤醒后**回查 status** 而不是直接用 wait 的返回值：wire 上要的是审批的 bool 投影，
            # 而 wait 给的是 Decision——投影口径只在 status() 里写一份（见其 docstring）。
            await queue.wait(approval_id, timeout=INBOX_BLOCK_SECONDS)
        except asyncio.TimeoutError:
            return JSONResponse({"status": "pending"})
        return JSONResponse(queue.status(approval_id))

    async def post_decision(request: Request):
        approval_id = request.path_params["approval_id"]
        try:
            body = await request.json()
        except Exception:
            return _bad_request("请求体必须是 JSON")
        approved = body.get("approved") if isinstance(body, dict) else None
        if not isinstance(approved, bool):
            return _bad_request("approved 必须是 bool")
        if queue.status(approval_id) is None:
            return JSONResponse({"error": "请求不存在"}, status_code=404)
        # HTTP 面**只承载审批**（worker 只会审批，提问在它那侧结构性不可达）——所以这里把
        # wire 上的 bool 包成 Decision，broker 内部则只认 Decision 一种表示。
        queue.complete(approval_id, Decision(kind="approval", approved=approved))
        return JSONResponse(queue.status(approval_id))

    return Starlette(
        routes=[
            Route("/requests", enqueue_request, methods=["POST"]),
            Route("/requests/{approval_id}", get_request, methods=["GET"]),
            Route("/requests/{approval_id}/decision", post_decision, methods=["POST"]),
        ]
    )


def _task_failure(task: asyncio.Task) -> str:
    """描述一个已结束任务的失败原因（只取不抛，供报错带上下文）。"""
    try:
        exc = task.exception()
    except BaseException as graceful:  # 被取消 / 任务里冒出 SystemExit 等
        return type(graceful).__name__
    return f"{type(exc).__name__}: {exc}" if exc is not None else "任务已结束"


class ApprovalInboxServer:
    """待审请求队列 + 薄 HTTP 面：在**已运行的事件循环**里以任务方式跑 uvicorn。"""

    def __init__(self, host: str = DEFAULT_INBOX_HOST, port: int | None = None) -> None:
        self.host = host
        self.port = (
            port if port is not None else int(os.environ.get("AGENT_INBOX_PORT", DEFAULT_INBOX_PORT))
        )
        self.queue = ApprovalInbox()
        self._app = build_app(self.queue)
        self._uvicorn = None
        self._task: asyncio.Task | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self) -> int:
        """在当前事件循环里拉起 uvicorn，阻塞到已监听；port=0 时返回实际端口。

        **先自己试绑一次端口**（`_precheck_port`）再起 uvicorn：uvicorn 绑不上时会在自己的
        serve 任务里 `sys.exit(1)`，而 SystemExit 会被 asyncio 从事件循环里再抛出去、**整个进程
        随之退出**——那时再做"事后翻译"已经来不及（只收得到一个 CancelledError）。预检把
        "端口被占"变成一条带解决办法的 `ConfigError`，且压根不会起 uvicorn。

        启动成功后把**实际**地址写进 `AGENT_INBOX_URL`：worker 子进程的 env 是整体替换、不继承
        父进程，spawn 时按这个 env 转发（`app/agent/tools.py::_spawn_subagent_worker`），
        这样"主侧换了端口"与"worker 往哪回传"才不会各说各话。
        """
        import uvicorn

        await self._precheck_port(self.host, self.port)

        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
            lifespan="off",
        )
        server = uvicorn.Server(config)
        self._uvicorn = server
        self._task = asyncio.create_task(server.serve())
        for _ in range(500):
            if server.started:
                break
            if self._task.done():  # 已经结束却没 started：多半是端口被占，别干等 5 秒
                raise ConfigError(
                    f"审批收件箱无法监听 {self.host}:{self.port}（端口可能已被占用）。"
                    f"设 AGENT_INBOX_PORT 换个端口再启动即可；底层错误：{_task_failure(self._task)}"
                )
            await asyncio.sleep(0.01)
        else:
            raise ConfigError(
                f"审批收件箱启动超时（{self.host}:{self.port}）；设 AGENT_INBOX_PORT 换端口再试。"
            )
        if self.port == 0:
            try:
                self.port = server.servers[0].sockets[0].getsockname()[1]
            except (IndexError, OSError):
                pass
        os.environ["AGENT_INBOX_URL"] = self.url  # 供 spawn worker 时转发（见 docstring）
        return self.port

    @staticmethod
    async def _precheck_port(host: str, port: int) -> None:
        """起 uvicorn 前先自己试绑一次端口；绑不上就抛可读的 `ConfigError`。

        端口 0（让 OS 挑）无需预检。试绑放在线程里（阻塞调用）；拔掉即关，不留监听。
        与真正 bind 之间有极窄的竞态（这一瞬被别人抢走），那时仍会走 uvicorn 的失败路径——
        但绝大多数"端口被占"都在这里被拦住，用户看到的是原因与解法，不是 `SystemExit: 1`。
        """
        if port == 0:
            return

        def _try_bind() -> str | None:
            sock = socket.socket()
            try:
                sock.bind((host, port))
                return None
            except OSError as exc:  # EADDRINUSE / WSAEADDRINUSE 等
                return f"{type(exc).__name__}: {exc}"
            finally:
                sock.close()

        failure = await asyncio.to_thread(_try_bind)
        if failure:
            raise ConfigError(
                f"审批收件箱无法监听 {host}:{port}（端口可能已被占用）：{failure}。"
                f"设 AGENT_INBOX_PORT 换个端口再启动即可。"
            )

    async def stop(self) -> None:
        """停掉收件箱——**清理路径绝不抛**：否则会盖掉真正的失败原因（如端口被占的启动失败）。"""
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except BaseException:  # noqa: BLE001 —— CancelledError 与 SystemExit（uvicorn 起不来）都在内
                pass
            self._task = None


# ------------------------- 判定面的基座部分（渲染归前端） -------------------------


def _record_to_value(record: ApprovalRecord) -> dict:
    """把 broker 里一条待决记录归一成 interrupt 同构的 value，交前端渲染与问人。

    value 与 ReviewNode interrupt payload 对齐，**`type` 决定前端怎么问**（审批面板 / 提问面板）：
    - 审批：{type, tool_name, current_step?, tool_args, description?, tool_call_id?}；
    - 提问：{type, why, question, options, tool_call_id?}。

    description 在 payload 层（ApprovalRequest 语义），前端主要展示 tool_args.description；
    tool_call_id 仅本地中断携带，纯展示（审批面板的"调用ID"与提问的追溯都用它）。
    """
    payload = record.get("payload") or {}
    worker_id = payload.get("worker_id", "")
    kind = payload.get("type") or GATE_TOOL_APPROVAL
    value: dict = {"type": kind}
    if kind == GATE_ASK_USER:
        value["why"] = payload.get("why") or ""
        value["question"] = payload.get("question") or ""
        value["options"] = list(payload.get("options") or [])
    else:
        value["tool_name"] = payload.get("tool_name", "?")
        value["tool_args"] = payload.get("tool_args") or {}
        value["description"] = payload.get("description")
        # 步骤：本地主 agent 中断带 driver 塞的 current_step（保持旧面板 "步骤 1/1" 样式）；
        # 远端 worker 无该字段时回退为来源标记（提问面板不用这一行，故只在审批分支里给）
        step = payload.get("current_step") or (
            "主 agent" if worker_id == LOCAL_WORKER_ID else f"worker:{worker_id}"
        )
        if step:
            value["current_step"] = step
    if payload.get("tool_call_id"):
        value["tool_call_id"] = payload["tool_call_id"]
    # worker 的子任务原文透传给前端——人靠它判断"这个 worker 想跑的命令是否有来由"
    if payload.get("subtask"):
        value["subtask"] = payload["subtask"]
    return value


async def drain_approvals(inbox: ApprovalInbox, ui: UI) -> None:
    """排空 broker 里全部 pending（本地主 agent 的两种 interrupt + worker 的 HTTP 请求）。

    逐条交前端问人（`ui.decide`）→ `inbox.complete` 回填（唤醒 park 的本地 run / 长轮询的
    worker）。判定权威在人；一条面板只服务一个请求，判定互不阻塞进程。
    """
    for entry in inbox.pending():
        decision = await ui.decide(_record_to_value(entry))
        inbox.complete(entry["approval_id"], decision)
    inbox.clear_new()
