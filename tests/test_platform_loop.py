"""app/platform/loop.py::AgentPlatform 的循环测试：假前端 + 假 step 驱动整条事件循环。

基座只认 `UI` 协议（emit / read_line / decide），所以这里用 `FakeUI` 当"无终端的前端"：
脚本化 read_line 的返回、自动决定审批、录下全部 emit 事件。`step_factory` 注入假 step
→ 不建 db、不拉 MCP、不需 .env。

覆盖：事件顺序（SessionStarted → ReadyForInput → TurnFinished）、空行不重复提示、
审批 park/resume 走通且决定透传、turn 抛错翻成 TurnFailed 不静默、EOF 收尾。

落盘侧防污染：`remove_legacy_single_db` 的 RESOURCE_ROOT 指到 tmp；inbox 端口用
AGENT_INBOX_PORT=0 让 OS 选空闲端口（避免与正在跑的 TUI 抢 8010）。
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Command

import app.platform.commands as commands_mod
import app.resource as resource
from app.platform.commands import PROMPT_COMMANDS
from app.platform.loop import AgentPlatform
from app.schema.ui_schema import Notice, ReadyForInput, SessionStarted, TurnFailed, TurnFinished

REPLY = "答复正文"


class FakeUI:
    """无终端的假前端：脚本化输入 + 自动决定 + 录事件（结构上满足 UI 协议）。"""

    def __init__(self, lines: list[str | None], approve: bool = True) -> None:
        self._lines = list(lines)
        self.approve = approve
        self.events: list = []
        self.decisions: list[dict] = []  # decide 收到的待审 value

    def emit(self, event) -> None:
        self.events.append(event)

    async def read_line(self) -> str | None:
        # 让出一步再取（模拟真实前端的 await）：被基座 cancel 时不会误吞一行
        await asyncio.sleep(0)
        if not self._lines:
            return None
        line = self._lines.pop(0)
        if line is None:  # 脚本里的 None 就是 EOF
            return None
        # 与终端前端一致：行先 strip（app/tui/input.py::_pump_stdin 就是这么做的），
        # 于是纯空白行 = 空串 = "无输入"，基座会继续等同一提示
        return line.strip()

    async def decide(self, value: dict) -> bool:
        self.decisions.append(value)
        return self.approve


class FakeStep:
    """假 step：按序弹中断 value；弹完给终态（或抛错）。记录收到的 resume 值。"""

    def __init__(self, interrupt_values=(), raise_exc: Exception | None = None) -> None:
        self.interrupt_values = list(interrupt_values)
        self.raise_exc = raise_exc
        self.resumes: list = []
        self.inputs: list[dict] = []  # 本轮初始 state（含 HumanMessage）
        self.calls = 0

    async def __call__(self, inputs):
        self.calls += 1
        if isinstance(inputs, Command):
            self.resumes.append(inputs.resume)
        else:
            self.inputs.append(inputs)
        if self.interrupt_values:
            value = self.interrupt_values.pop(0)
            return {"__interrupt__": [type("_It", (), {"value": value})()]}
        if self.raise_exc is not None:
            raise self.raise_exc
        return {"messages": [AIMessage(content=REPLY)]}


def _factory(step):
    """把假 step 包成基座要的 step_factory（返回 (connection, step)）。"""

    async def build(workspace: str, session_id: str):
        return None, step  # 假 step 不需要 db 连接

    return build


def _platform(tmp_path, monkeypatch, ui, step):
    monkeypatch.setattr(resource, "RESOURCE_ROOT", tmp_path / "res")  # 不碰真实 resource
    monkeypatch.setenv("AGENT_INBOX_PORT", "0")  # 让 OS 选空闲端口，别抢 8010
    return AgentPlatform(
        workspace=str(tmp_path / "ws"),
        session_id="s1",
        ui=ui,
        step_factory=_factory(step),
    )


def test_loop_emits_events_in_order(tmp_path, monkeypatch):
    ui = FakeUI(["你好", None])  # 一条输入，然后 EOF

    asyncio.run(_platform(tmp_path, monkeypatch, ui, FakeStep()).run())

    kinds = [type(e) for e in ui.events]
    assert kinds == [SessionStarted, ReadyForInput, TurnFinished, ReadyForInput]
    started = ui.events[0]
    assert started.session_id == "s1" and started.workspace.endswith("ws")
    assert ui.events[2].text == REPLY
    assert ui.decisions == []  # 没有待审


def test_loop_ignores_blank_line_without_extra_prompt(tmp_path, monkeypatch):
    ui = FakeUI(["", "  ", "干活", None])

    asyncio.run(_platform(tmp_path, monkeypatch, ui, FakeStep()).run())

    # 空行不重复画提示：每个空闲段只 emit 一次 ReadyForInput
    assert [type(e) for e in ui.events].count(ReadyForInput) == 2
    assert [type(e) for e in ui.events].count(TurnFinished) == 1


@pytest.mark.parametrize("approved", [True, False])
def test_loop_parks_and_resumes_on_approval(tmp_path, monkeypatch, approved):
    """审批经 FakeUI.decide 收决定 → broker.complete → run 以 Command(resume) 续跑。"""
    ui = FakeUI(["跑个命令", None], approve=approved)
    step = FakeStep([{"tool_name": "run_command", "tool_args": {"command": "ls"}}])

    asyncio.run(_platform(tmp_path, monkeypatch, ui, step).run())

    assert [d["tool_name"] for d in ui.decisions] == ["run_command"]
    assert step.resumes == [{"approved": approved}]  # 决定透传给图
    assert step.calls == 2  # 中断 + 终态
    assert [type(e) for e in ui.events].count(TurnFinished) == 1


def test_init_command_starts_turn_with_preset_prompt(tmp_path, monkeypatch):
    """`/init` = 基座把预设提示词当本轮输入投出去：起的 turn 首条消息正是那段 prompt。

    （假 step 断言，不花钱——真实跑一次会调模型 + 触发写文件审批。）
    """
    ui = FakeUI(["/init", None])
    step = FakeStep()

    asyncio.run(_platform(tmp_path, monkeypatch, ui, step).run())

    expected = PROMPT_COMMANDS["/init"]["prompt"]
    assert [m.content for m in step.inputs[0]["messages"]] == [expected]
    assert [type(e) for e in ui.events].count(TurnFinished) == 1
    assert ui.decisions == []  # 命令本身不触发审批


def test_action_command_slot_runs_handler_without_turn(tmp_path, monkeypatch):
    """基座动作型命令（`/session`、`/content` 之类的位置）：handler 被调用、不起 turn。

    本轮没有真实动作命令，故注册一个假的来钉住这条通路——否则将来加 `/session` 才发现槽没接。
    """
    seen: list = []

    async def fake_handler(platform):
        seen.append(platform)
        platform.ui.emit(Notice(text="动作命令已执行"))

    monkeypatch.setitem(
        commands_mod.ACTION_COMMANDS, "/fake", {"usage": "/fake 测试", "handler": fake_handler}
    )
    ui = FakeUI(["/fake", None])
    step = FakeStep()

    asyncio.run(_platform(tmp_path, monkeypatch, ui, step).run())

    assert len(seen) == 1  # handler 拿到的是基座本身
    assert step.inputs == []  # 没起 turn、也没喂模型
    assert any(isinstance(e, Notice) and "已执行" in e.text for e in ui.events)


def test_help_command_shows_all_commands_without_turn(tmp_path, monkeypatch):
    """`/help`：回一条列全命令的 Notice，不起 turn、不喂模型。"""
    ui = FakeUI(["/help", None])
    step = FakeStep()

    asyncio.run(_platform(tmp_path, monkeypatch, ui, step).run())

    notices = [e for e in ui.events if isinstance(e, Notice)]
    assert len(notices) == 1
    assert "/help" in notices[0].text and "/init" in notices[0].text
    assert "显示所有可用命令与说明" in notices[0].text  # 描述也在
    assert step.inputs == []  # 没起 turn
    assert [type(e) for e in ui.events].count(ReadyForInput) == 2  # 提示完回到可输入


def test_unknown_command_only_notifies(tmp_path, monkeypatch):
    """未识别的 `/xxx` 不喂给模型：只 emit Notice，随后那条普通消息照常起 turn。"""
    ui = FakeUI(["/nope", "正常消息", None])
    step = FakeStep()

    asyncio.run(_platform(tmp_path, monkeypatch, ui, step).run())

    notices = [e for e in ui.events if isinstance(e, Notice)]
    assert len(notices) == 1 and "/init" in notices[0].text  # 提示里列出可用命令
    assert [m.content for m in step.inputs[0]["messages"]] == ["正常消息"]  # 只起了这一轮
    assert len(step.inputs) == 1


def test_loop_reports_turn_failure_without_swallowing(tmp_path, monkeypatch):
    """图运行抛错 → TurnFailed（不静默），循环仍回到空闲、能正常收尾。"""
    ui = FakeUI(["触发错误", None])
    step = FakeStep(raise_exc=RuntimeError("图炸了"))

    asyncio.run(_platform(tmp_path, monkeypatch, ui, step).run())

    failed = [e for e in ui.events if isinstance(e, TurnFailed)]
    assert len(failed) == 1
    assert "RuntimeError" in failed[0].message and "图炸了" in failed[0].message
    assert [type(e) for e in ui.events].count(ReadyForInput) == 2  # 出错后仍回到空闲
