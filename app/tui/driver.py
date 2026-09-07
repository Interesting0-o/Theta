"""TUI 的事件驱动 driver：agent 图是**一个模块**，本文件是承载它的壳。

背景（docs/MULTI_AGENT.md §6/§9/§10，2026-09-07）：interrupt 的契约 = **阻塞图、不阻塞
进程**。主 agent 自身 interrupt 与 worker 审批对齐成同一种 ApprovalPending：本地 park 把
一条 ApprovalRequest 入统一 broker（app/tui/approval_inbox.py 的 ApprovalInbox）、`await
wait(id)` 挂起（run 停、进程自由），审核方 `complete(id, approved)` 唤醒后以
`Command(resume)` 续跑。判定单点在 app/tui/approval.py（drain_approvals：pending 即服务，
本地 worker_id="main" 与远端 worker 一视同仁）。

模块划分：输入解耦在 input.py；审批判定面在 approval.py；队列/HTTP 在 approval_inbox.py；
本文件只做**装配 + 事件循环调度**。`app/main.py` 是薄入口，调 `run_tui()`。
`app.agent.*`（graph/model）在 run_tui 内懒加载——保持 `import app.tui` 无 .env、无副作用。

- `get_agent_db_path`：checkpoint 的 SQLite 路径（resource/agent.db）。
- `drive_turn(step, initial, queue)`：agent turn 核心（step 可注入便于测试）；中断即入
  broker park，返回终态。
- `run_tui`：事件循环——drain 待批（pending 即服务）→ 无批且 turn 在跑则等
  turn_done / new_pending → 无批且空闲则读用户行起新 turn。
"""
import asyncio
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from app.schema.approval_schema import ApprovalRequest
from app.tui.approval import LOCAL_WORKER_ID, drain_approvals
from app.tui.approval_inbox import ApprovalInboxServer
from app.tui.input import _pump_stdin, _start_stdin_reader

# 会话 thread_id（与旧 app/main.py 保持一致；checkpoint 持久化按它续）
THREAD_ID = "conversation_456"


def get_agent_db_path() -> Path:
    """agent checkpoint 的持久化 SQLite 路径（项目根/resource/agent.db，自动建目录）。"""
    db_dir = Path(__file__).resolve().parent.parent.parent / "resource"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "agent.db"


# ---------------------------------------------------------------------------
# agent turn 核心：segment → 中断入统一 broker park → Command(resume) 续跑
# ---------------------------------------------------------------------------


def _interrupt_value_to_request(value: dict) -> ApprovalRequest:
    """一条 graph interrupt value（ReviewNode payload 同构）→ 本地审批请求。

    worker_id="main" 标记本地来源（与远端 worker 区分，只用于渲染 provenance；
    决定路径与远端完全一致——都走 broker 的 enqueue/wait 与 complete）。
    """
    return {
        "worker_id": LOCAL_WORKER_ID,
        "tool_name": value.get("tool_name", "?"),
        "tool_args": value.get("tool_args") or {},
        "description": value.get("description"),
    }


async def drive_turn(step, initial, queue):
    """跑一轮 agent turn：step(inputs) 返回 state；遇 __interrupt__ 入 broker park。

    - step：一次图执行（默认 compiled.ainvoke 的闭包），可注入假 step 便于测试。
    - initial：本轮首个输入（state 字典）。
    - queue：统一 broker（ApprovalInbox）。中断 → `enqueue` 拿 id → `await wait(id)`
      **park**（run 挂起、进程自由）→ 审核方 complete 唤醒 → `Command(resume)` 续跑。
    - 返回终态（无 __interrupt__ 的 result），由调用方打印/收尾。

    多值 __interrupt__（罕见，ReviewNode 正常逐条 drain）合成一条审批兜底、resume 一次，
    与旧 app/main.py 的兜底语义一致。
    """
    inputs = initial
    while True:
        result = await step(inputs)
        interrupts = result.get("__interrupt__")
        if not interrupts:
            return result
        values = [getattr(it, "value", it) for it in list(interrupts)]
        if len(values) == 1 and isinstance(values[0], dict):
            payload: ApprovalRequest = _interrupt_value_to_request(values[0])
        else:
            # 兜底：整表一次审批（正常路径不会到这）
            names = [
                (v.get("tool_name") if isinstance(v, dict) else "?") for v in values
            ]
            payload = {
                "worker_id": LOCAL_WORKER_ID,
                "tool_name": "多工具审批",
                "tool_args": {},
                "description": "一次请求审批多项工具：" + "、".join(names),
            }
        approval_id = queue.enqueue(payload)
        approved = await queue.wait(approval_id)  # park：阻塞 run、不阻塞进程
        inputs = Command(resume={"approved": approved})


# ---------------------------------------------------------------------------
# 事件循环装配 + 调度
# ---------------------------------------------------------------------------


async def _race(*factories):
    """并发跑若干协程工厂，返回先完成者的 (index, result)，其余取消。

    用于"等 user 行 / 等审批 / 等 turn 结束"这类多来源唤醒，避免忙轮询。
    """
    tasks = [asyncio.create_task(f()) for f in factories]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for t in pending:
        # 吞掉被取消任务抛出的 CancelledError；也兜住"取消前刚好以异常结束"的竞态。
        # （CancelledError 是 BaseException 子类，须与 Exception 并列才能都接住。）
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    for i, t in enumerate(tasks):
        if t in done:
            return i, t.result()
    return -1, None


async def _run_turn(step, initial, queue, turn_done: asyncio.Event) -> None:
    """后台跑一个 user turn，终态打印末条 AI 正文；结束置 turn_done。

    图运行抛错（内部 bug / 工具异常漏网等）不静默：打印错误提示，仍置 turn_done 让
    事件循环回到空闲，避免 "Task exception was never retrieved" 与死等的观感。
    """
    try:
        result = await drive_turn(step, initial, queue)
        messages = result.get("messages") or []
        if messages:
            print("\n[agent]")
            print(messages[-1].content)
    except asyncio.CancelledError:
        raise  # 关闭清理主动取消，放行
    except Exception as exc:  # noqa: BLE001 —— 用户态错误提示，不让任务静默失败
        print(f"\n[agent 运行出错] {type(exc).__name__}: {exc}")
    finally:
        turn_done.set()


def _build_turn_state(user_input: str) -> dict:
    return {
        "session_id": THREAD_ID,
        "messages": [HumanMessage(content=user_input)],
        "pending_tool_calls": [],
        "approved_tool_calls": [],
    }


async def run_tui() -> None:
    """事件驱动 TUI：装配 db/graph/checkpointer/config + 收件箱 + stdin，然后循环调度。

    - pending 审批（本地 park 或 worker HTTP）**即服务**：drain_approvals 逐条渲染收 y/n；
    - 无审批且 turn 在跑 → 等 turn_done 或 new_pending（park 或新 worker 请求都会唤醒）；
    - 无审批且空闲 → 读用户行起 turn（q/quit/exit 退出；EOF 结束）。
    app.agent.* 在此懒加载（入口运行期才有 .env；import app.tui 无需 .env）。
    """
    import aiosqlite  # noqa: PLC0415
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: PLC0415
    from app.agent.graph import get_graph  # noqa: PLC0415

    db_path = get_agent_db_path()
    connection = await aiosqlite.connect(str(db_path))
    checkpointer = AsyncSqliteSaver(connection)
    await checkpointer.setup()

    graph = await get_graph()
    compiled = graph.compile(checkpointer=checkpointer)
    config: dict = {"configurable": {"thread_id": THREAD_ID}}
    step = lambda inputs: compiled.ainvoke(inputs, config)  # noqa: E731

    inbox = ApprovalInboxServer()
    pump: asyncio.Task | None = None
    try:
        await inbox.start()
        print(f"[收件箱] 监听 {inbox.url}  (worker 待审请求 POST /requests)")
        print("[提示] 输入 q/quit/exit 退出\n")

        reader_q = _start_stdin_reader()
        out_q: asyncio.Queue = asyncio.Queue()
        pump = asyncio.create_task(_pump_stdin(reader_q, out_q))

        turn: asyncio.Task | None = None
        turn_done = asyncio.Event()

        while True:
            # 1) 审批 pending 即服务（本地 park + worker HTTP 一视同仁）
            if inbox.queue.pending():
                await drain_approvals(inbox.queue, out_q)
                continue

            # 2) 有 turn 在跑（含 park 挂起）→ 等它结束或新审批到达
            if turn is not None and not turn.done():
                inbox.queue.clear_new()  # pending 已空，清掉过期 set 再等
                await _race(turn_done.wait, inbox.queue.new_pending.wait)
                continue

            # 3) 空闲 → 等用户行或新 worker 审批
            inbox.queue.clear_new()
            idx, line = await _race(
                out_q.get, inbox.queue.new_pending.wait
            )
            if idx == 1:  # 审批先到（worker 请求），回顶部 drain
                continue
            if line is None:
                break  # EOF
            if line in ("quit", "q", "exit"):
                break
            if not line:
                continue

            print("\n[user] " + line)
            turn_done.clear()
            turn = asyncio.create_task(_run_turn(step, _build_turn_state(line), inbox.queue, turn_done))
            # 回循环顶部：新 turn 的审批/终态都从 drain 与 turn_done 两个信号驱动
    finally:
        if pump is not None:
            pump.cancel()
        if turn is not None and not turn.done():
            turn.cancel()
        await inbox.stop()
        await connection.close()
