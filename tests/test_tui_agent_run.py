"""app/tui/driver.py::drive_turn 的 hermetic 单测（统一 broker park/resume）。

drive_turn 是 agent turn 核心：step 可注入（默认 compiled.ainvoke 的闭包），故这里用假
step——先返回若干 __interrupt__（模拟 ReviewNode 中断），由假 decider 对统一 broker
ApprovalInbox complete 回填，再返回终态。断言：
- interrupt 被入 broker、payload 带 worker_id="main" 标记；
- park 期间 run 挂起（wait 阻塞到 complete）；
- resume 以 Command(resume={"approved": bool}) 续跑，approved 透传决定；
- 多个中断依次处理；无中断直通终态。

不 import app.agent → 无需 .env / WORKSPACE_PATH。
"""
import asyncio

from langgraph.types import Command

from app.tui.approval_inbox import ApprovalInbox
from app.tui.driver import _race, drive_turn

FINAL = {"messages": ["done"]}


class FakeStep:
    """假 step：按序弹出若干中断 value；中断弹完给终态。记录收到的 resume 值。"""

    def __init__(self, interrupt_values, final=FINAL):
        self.interrupt_values = list(interrupt_values)
        self.final = final
        self.resumes: list = []  # 收到的 Command(resume=...) 值
        self.calls = 0

    async def __call__(self, inputs):
        self.calls += 1
        if isinstance(inputs, Command):
            self.resumes.append(inputs.resume)
        if self.interrupt_values:
            value = self.interrupt_values.pop(0)
            # 与 ReviewNode interrupt 结果同构：__interrupt__ 里的条目带 value
            return {"__interrupt__": [type("_It", (), {"value": value})()]}
        return self.final


async def _approve_next(queue: ApprovalInbox, approved: bool) -> str:
    """假 decider：等下一条 pending 入队后 complete，返回其 approval_id。"""
    for _ in range(1000):
        pending = queue.pending()
        if pending:
            aid = pending[0]["approval_id"]
            assert queue.complete(aid, approved) is True
            return aid
        await asyncio.sleep(0)
    raise AssertionError("收件箱一直没收到请求")


def test_drive_turn_no_interrupt_returns_final():
    async def go():
        step = FakeStep([])
        result = await drive_turn(step, {"messages": []}, ApprovalInbox())
        assert result == FINAL
        assert step.resumes == []

    asyncio.run(go())


def test_drive_turn_single_interrupt_approved():
    async def go():
        queue = ApprovalInbox()
        step = FakeStep([{"tool_name": "run_command", "tool_args": {"command": "rm -rf dist"}}])

        async def decider():
            aid = await _approve_next(queue, True)
            rec = queue.status(aid)
            assert rec == {"status": "decided", "approved": True}

        dec = asyncio.create_task(decider())
        result = await drive_turn(step, {"messages": []}, queue)
        await dec

        assert result == FINAL
        # resume 值透传：Command(resume={"approved": True})
        assert step.resumes == [{"approved": True}]

    asyncio.run(go())


def test_drive_turn_single_interrupt_denied():
    async def go():
        queue = ApprovalInbox()
        step = FakeStep([{"tool_name": "write_file", "tool_args": {"path": "x.md"}}])

        async def decider():
            await _approve_next(queue, False)

        dec = asyncio.create_task(decider())
        result = await drive_turn(step, {"messages": []}, queue)
        await dec

        assert result == FINAL
        assert step.resumes == [{"approved": False}]

    asyncio.run(go())


def test_drive_turn_multiple_interrupts_sequential():
    async def go():
        queue = ApprovalInbox()
        step = FakeStep(
            [
                {"tool_name": "create_file", "tool_args": {"path": "a.py"}},
                {"tool_name": "run_command", "tool_args": {"command": "pytest"}},
            ]
        )

        async def decider():
            await _approve_next(queue, True)
            await _approve_next(queue, False)

        dec = asyncio.create_task(decider())
        result = await drive_turn(step, {"messages": []}, queue)
        await dec

        assert result == FINAL
        # 两次中断各自一次 resume，approved 分别透传
        assert step.resumes == [{"approved": True}, {"approved": False}]
        assert step.calls == 3  # 中断、中断、终态

    asyncio.run(go())


def test_interrupt_payload_marks_main_worker():
    async def go():
        queue = ApprovalInbox()
        step = FakeStep(
            [{"tool_name": "run_command", "tool_args": {"command": "ls"}}]
        )

        async def decider():
            for _ in range(1000):
                pending = queue.pending()
                if pending:
                    payload = pending[0]["payload"]
                    # 本地中断 worker_id 标记为 "main"（远端 worker 才是真实 id）
                    assert payload["worker_id"] == "main"
                    assert payload["tool_name"] == "run_command"
                    assert payload["tool_args"] == {"command": "ls"}
                    queue.complete(pending[0]["approval_id"], True)
                    return
                await asyncio.sleep(0)
            raise AssertionError("收件箱一直没收到请求")

        dec = asyncio.create_task(decider())
        await drive_turn(step, {"messages": []}, queue)
        await dec

    asyncio.run(go())


# --- run_tui 用的事件唤醒 _race：位置参数 + 多源竞态/取消语义 ---


def test_race_returns_first_completed_index():
    async def go():
        a, b = asyncio.Event(), asyncio.Event()
        b.set()
        # 位置参数传多个事件.wait（修复回归：曾误把 *factories 当单参 list）
        idx, val = await _race(a.wait, b.wait)
        assert idx == 1
        assert val is True  # Event.wait 置位时返回 True（driver 不消费此值，仅取其唤醒）

    asyncio.run(go())


def test_race_wakes_on_later_event_and_cancels_others():
    async def go():
        a, b = asyncio.Event(), asyncio.Event()

        async def set_b_later():
            await asyncio.sleep(0.01)
            b.set()

        setter = asyncio.create_task(set_b_later())
        idx, _ = await _race(a.wait, b.wait)
        await setter
        assert idx == 1
        assert not a.is_set()  # 另一个事件被取消，未置位

    asyncio.run(go())
