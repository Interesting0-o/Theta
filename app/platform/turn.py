"""一次 run 的驱动原语：park/resume + 多源唤醒竞速（零显示，归基座）。

位置：`app/platform/turn.py`（从 app/tui/driver.py 平移；2026-09-10 基座/前端分家后归基座）。
本模块**不 print、不读 stdin**：run 的终态由调用方（loop.py）翻成事件交前端渲染。

interrupt 的契约（docs/MULTI_AGENT.md §6 统一审批视图）：**阻塞图、不阻塞进程**。run 停在
checkpoint、事件循环自由——本模块把一条 run 的中断送进统一 broker（approvals.py）park 住，
等审核方 `complete` 唤醒后以 `Command(resume={"approved": …})` 续跑；审批面板由前端另画。
"""
import asyncio

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from app.platform.approvals import LOCAL_WORKER_ID
from app.schema.approval_schema import ApprovalRequest


def _interrupt_value_to_request(value: dict) -> ApprovalRequest:
    """一条 graph interrupt value（ReviewNode payload 同构）→ 本地审批请求。

    worker_id="main" 标记本地来源（与远端 worker 区分，只用于渲染 provenance；
    决定路径与远端完全一致——都走 broker 的 enqueue/wait 与 complete）。
    current_step / tool_call_id 是**纯展示字段**（复用 ReviewNode 的"步骤 1/1"与调用 ID，
    让审批面板保持旧样式），不参与决定逻辑；远端 worker 不带。
    """
    return {
        "worker_id": LOCAL_WORKER_ID,
        "tool_name": value.get("tool_name", "?"),
        "tool_args": value.get("tool_args") or {},
        "description": value.get("description"),
        "current_step": value.get("current_step") or "1/1",
        "tool_call_id": value.get("tool_call_id"),
    }


async def drive_turn(step, initial, queue):
    """跑一轮 agent turn：step(inputs) 返回 state；遇 __interrupt__ 入 broker park。

    - step：一次图执行（默认 compiled.ainvoke 的闭包），可注入假 step 便于测试。
    - initial：本轮首个输入（state 字典）。
    - queue：统一 broker（ApprovalInbox）。中断 → `enqueue` 拿 id → `await wait(id)`
      **park**（run 挂起、进程自由）→ 审核方 complete 唤醒 → `Command(resume)` 续跑。
    - 返回终态（无 __interrupt__ 的 result），由调用方决定怎么呈现。

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


def build_turn_state(session_id: str, user_input: str) -> dict:
    """一轮 turn 的初始 state（新用户消息 + 清空的审批队列）。"""
    return {
        "session_id": session_id,
        "messages": [HumanMessage(content=user_input)],
        "pending_tool_calls": [],
        "approved_tool_calls": [],
    }
