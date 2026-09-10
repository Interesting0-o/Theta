"""待审请求的队列 + 送达（统一审批 broker）；**本模块不做审批判定**。

位置：`app/platform/approvals.py`（从 app/tui/approval_inbox.py 平移；2026-09-10 基座/前端
分家后归基座——审批队列与送达是 run 生命周期的事，与终端显示无关）。

职责边界（对齐 docs/MULTI_AGENT.md §6）：判定"要不要放行某动作"单点在**图的审核节点**
（ReviewNode + interrupt）——人答 y/n 只发生在图的审核节点，经 interrupt→Command(resume)
语义。本模块只承接"待审请求的排队 + 决定的送达"，是纯队列/收件箱，不是审批权威。

**统一 broker 语义（2026-09-07 起）**：本地主 agent 的 graph interrupt 与远端 worker 的
HTTP 审批请求**同入这一个队列**——差异只在 payload 的 `worker_id` 标记（本地="main"、
远端=真实 worker id），决定路径一致：
- 本地 park：agent turn 驱动 `enqueue(payload)` 拿 id → `await wait(id)` 挂起
  （**阻塞 run、不阻塞进程**）；审核方 `complete(id, approved)` → wait 唤醒 → 以
  `Command(resume={approved})` 续跑；
- 远端 worker：HTTP POST /requests 入队 → 审核方 complete → worker 长轮询
  GET /requests/{id}?block=1 取回决定。

"谁来问人"由前端定：`drain_approvals` 把每条待审交 `UI.decide`（终端 = 面板 + y/n），
本模块只负责排队、回填与唤醒。

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
import time
from dataclasses import dataclass, field

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.platform.ui import UI
from app.schema.approval_schema import ApprovalRecord, ApprovalRequest, ApprovalStatus

# 本地主 agent 中断入 broker 时用的 worker_id 标记（远端 worker 用真实 id）
LOCAL_WORKER_ID = "main"


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
        # 有新请求置位（idle 循环用事件竞争唤醒）；观察方处理后自行 clear。
        self.new_pending = asyncio.Event()

    def enqueue(self, payload: ApprovalRequest) -> str:
        """登记一条待审请求（payload 为 schema 的 ApprovalRequest），返回其 approval_id。"""
        approval_id = f"appr-{next(self._seq)}"
        record: ApprovalRecord = {
            "approval_id": approval_id,
            "payload": dict(payload),
            "created": time.time(),
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
        """某条请求的状态；未知 id → None。"""
        item = self._items.get(approval_id)
        if item is None:
            return None
        return {
            "status": "decided" if item.record["decided"] is not None else "pending",
            "approved": item.record["decided"],
        }

    def complete(self, approval_id: str, approved: bool) -> bool:
        """审核方回填一条请求的决定并唤醒其等待者。幂等：未知/已回填返回 False。

        只做记录 + 唤醒，不做判定——approved 由调用方（图的审核节点/人审入口）给出。
        """
        item = self._items.get(approval_id)
        if item is None or item.record["decided"] is not None:
            return False
        item.record["decided"] = bool(approved)
        if not item.decision.done():
            item.decision.set_result(bool(approved))
        return True

    async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
        """阻塞到该请求被 complete；返回是否批准。超时抛 asyncio.TimeoutError。"""
        item = self._items.get(approval_id)
        if item is None:
            raise KeyError(approval_id)
        if item.record["decided"] is not None:
            return bool(item.record["decided"])
        if timeout is not None:
            return await asyncio.wait_for(asyncio.shield(item.decision), timeout)
        return await item.decision

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
            approved = await queue.wait(approval_id, timeout=120.0)
            return JSONResponse({"status": "decided", "approved": approved})
        except asyncio.TimeoutError:
            return JSONResponse({"status": "pending"})

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
        queue.complete(approval_id, approved)
        return JSONResponse(queue.status(approval_id))

    async def list_requests(request: Request):
        return JSONResponse({"pending": queue.pending()})

    return Starlette(
        routes=[
            Route("/requests", enqueue_request, methods=["POST"]),
            Route("/requests", list_requests, methods=["GET"]),
            Route("/requests/{approval_id}", get_request, methods=["GET"]),
            Route("/requests/{approval_id}/decision", post_decision, methods=["POST"]),
        ]
    )


class ApprovalInboxServer:
    """待审请求队列 + 薄 HTTP 面：在**已运行的事件循环**里以任务方式跑 uvicorn。"""

    def __init__(self, host: str = "127.0.0.1", port: int | None = None) -> None:
        self.host = host
        self.port = port if port is not None else int(os.environ.get("AGENT_INBOX_PORT", "8010"))
        self.queue = ApprovalInbox()
        self._app = build_app(self.queue)
        self._uvicorn = None
        self._task: asyncio.Task | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self) -> int:
        """在当前事件循环里拉起 uvicorn，阻塞到已监听；port=0 时返回实际端口。"""
        import uvicorn

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
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("approval server 启动超时")
        if self.port == 0:
            try:
                self.port = server.servers[0].sockets[0].getsockname()[1]
            except (IndexError, OSError):
                pass
        return self.port

    async def stop(self) -> None:
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


# ------------------------- 判定面的基座部分（渲染归前端） -------------------------


def _record_to_value(record: ApprovalRecord) -> dict:
    """把 broker 里一条待审记录归一成 interrupt 同构的 value，交前端渲染。

    value 与 ReviewNode interrupt payload 对齐：{tool_name, current_step, tool_args,
    description?, tool_call_id?}。
    - 步骤：本地主 agent 中断带 driver 塞的 ReviewNode current_step（如 "1/1"，保持旧面板
      样式）；远端 worker 无该字段时回退为来源标记（主 agent / worker:<id>）。
    - tool_call_id：仅本地中断携带（纯展示）。
    description 在 payload 层（ApprovalRequest 语义），前端主要展示 tool_args.description。
    """
    payload = record.get("payload") or {}
    worker_id = payload.get("worker_id", "")
    step = payload.get("current_step") or (
        "主 agent" if worker_id == LOCAL_WORKER_ID else f"worker:{worker_id}"
    )
    value: dict = {
        "tool_name": payload.get("tool_name", "?"),
        "tool_args": payload.get("tool_args") or {},
        "description": payload.get("description"),
    }
    if step:
        value["current_step"] = step
    if payload.get("tool_call_id"):
        value["tool_call_id"] = payload["tool_call_id"]
    return value


async def drain_approvals(inbox: ApprovalInbox, ui: UI) -> None:
    """排空 broker 里全部 pending（本地主 agent interrupt + worker HTTP 请求）。

    逐条交前端问人（`ui.decide`）→ `inbox.complete` 回填（唤醒 park 的本地 run / 长轮询的
    worker）。判定权威在人；一条面板只服务一个请求，判定互不阻塞进程。
    """
    for entry in inbox.pending():
        approved = await ui.decide(_record_to_value(entry))
        inbox.complete(entry["approval_id"], approved)
    inbox.clear_new()
