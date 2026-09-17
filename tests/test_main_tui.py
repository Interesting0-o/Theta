"""app/tui 的终端辅助函数测试：truncate / format_tool_approval / render_markdown /
resolve_tui_workspace。

truncate / format_tool_approval / render_markdown 在 app/tui/panels.py（终端渲染素材），
resolve_tui_workspace 在 app/tui/runner.py（**前端策略**：工作区=启动目录）。checkpoint 落点
路径已单点到 app/resource.py（见 tests/test_resource.py）。app.tui 模块 import 期不触发
app.agent → 本文件无需 .env。
"""
from io import StringIO
from types import SimpleNamespace

import asyncio

import pytest

from app.schema.approval_schema import GATE_ASK_USER, Decision
from app.schema.ui_schema import TurnFinished
from app.tui import panels
from app.tui import ui as tui_ui
from app.tui.panels import format_ask, format_tool_approval, render_markdown, truncate
from app.tui.runner import resolve_tui_workspace
from app.tui.ui import TerminalUI, parse_option_indexes


def test_terminal_ui_eof_makes_reads_return_immediately():
    """EOF（stdin 关闭 / 管道耗尽）后 `read_line` 与 `decide` **立即返回**，不再干等。

    EOF 之后 pump 就结束了、队列不会再有内容：若还照旧 `await queue.get()`，一旦此时有 turn
    要审批，就是永久阻塞（只能 Ctrl+C）。用 wait_for 兜住——真卡住会变成测试失败而不是挂死。
    """

    async def go():
        ui = TerminalUI()
        ui._out_q = asyncio.Queue()  # 白盒注入：不真起 stdin reader 线程
        ui._out_q.put_nowait(None)  # pump 在 EOF 时放的哨兵

        assert await ui.read_line() is None
        assert await ui.read_line() is None  # 第二次也不再阻塞
        # 审批：无人确认 → 不批准；提问：无人作答 → 未回答（两段皆空，语义不同见 UI 协议）
        assert await ui.decide({"tool_name": "write_file"}) == Decision(
            kind="approval", approved=False
        )
        asked = await ui.decide({"type": GATE_ASK_USER, "question": "?", "options": ["甲"]})
        assert asked == Decision(kind="answer")
        assert asked.unanswered is True

    asyncio.run(asyncio.wait_for(go(), timeout=2))


def test_decide_holds_back_lines_typed_before_the_panel(capsys):
    """面板出现**之前**排队的行属于下一个 turn，不能被当 y/n 答案吞掉。

    实际场景：用户在模型思考时先敲好下一条消息 → turn 撞上审批 → 若 decide 直接取队首，那行
    消息就会被当成答案，不匹配 y/n 判为"拒绝"，而且**字丢了**（2026-09-15 审计发现）。
    """

    async def go():
        ui = TerminalUI()
        ui._out_q = asyncio.Queue()  # 白盒注入：不真起 stdin reader 线程
        ui._out_q.put_nowait("帮我改一下 X")  # 模型思考时预打的下一条消息

        task = asyncio.create_task(ui.decide({"tool_name": "run_command"}))
        await asyncio.sleep(0)  # 让 decide 走完打印、挪走预输入，停在等答案处
        ui._out_q.put_nowait("y")  # 用户看到面板后敲的答案
        decision = await asyncio.wait_for(task, timeout=2)

        assert decision == Decision(kind="approval", approved=True)  # 没被预输入顶掉
        assert ui._held == ["帮我改一下 X"]  # 预输入既没丢、也没被当答案
        assert await ui.read_line() == "帮我改一下 X"  # 按原样进下一个 turn
        assert "已留给下一个回合" in capsys.readouterr().out

    asyncio.run(go())


def test_terminal_ui_aclose_finishes_pump_task():
    """`aclose` 要 **await 掉**被取消的 pump：只 cancel 不 await 会留下 pending 任务噪声。"""

    async def go():
        ui = TerminalUI()
        pump = asyncio.create_task(asyncio.sleep(3600))
        ui._pump = pump

        await ui.aclose()

        assert ui._pump is None
        assert pump.done() and pump.cancelled()  # 已收尾，不是悬着的 pending 任务

    asyncio.run(go())


def test_resolve_tui_workspace_is_cwd(tmp_path, monkeypatch):
    """TUI 工作区 = 启动目录（cwd），作为 workspace_path 参量传给 graph。"""
    monkeypatch.chdir(tmp_path)
    assert resolve_tui_workspace() == str(tmp_path)


def test_truncate_keeps_short_text():
    assert truncate("abc") == "abc"


def test_truncate_long_text_marks_cut():
    out = truncate("x" * 300)
    assert out.startswith("x" * 200)
    assert "已截断" in out


def test_format_tool_approval_extracts_fields():
    items = [
        SimpleNamespace(
            value={
                "tool_name": "write_file",
                "current_step": 3,
                "tool_args": {"path": "/ws/demo.txt", "content": "y" * 300},
                "tool_call_id": "call_1",
            }
        )
    ]
    text = format_tool_approval(items)
    assert "[write_file]" in text
    assert "步骤 3" in text
    assert "调用ID: call_1" in text
    assert "已截断" in text  # 超长参数内容被 truncate


# ------------------------- 提问面板（闸门 type=ask_user） -------------------------

ASK = {
    "type": GATE_ASK_USER,
    "why": "工作区里有两个同名函数，猜错要重做",
    "question": "改哪个？",
    "options": ["a.py 里的", "c.py 里的"],
    "tool_call_id": "call_ask",
}

# 复选题（`select="many"`）：选项互相独立，用户可一次勾多项
ASK_MANY = {**ASK, "options": ["a.py 里的", "b.py 里的", "c.py 里的"], "select": "many"}


async def _ask(lines: list[str | None], value: dict = ASK):
    """驱动一次提问面板：`lines` 是用户**看到面板之后**依次敲的行（None = EOF）。

    必须等面板画完再喂——面板出现**之前**排队的行会被 `_hold_pretyped` 留给下一个 turn
    （见 `TerminalUI.decide`），直接预排队列会被当成预输入而拿不到。
    """
    ui = TerminalUI()
    ui._out_q = asyncio.Queue()  # 白盒注入：不真起 stdin reader 线程

    async def typist():
        for line in lines:
            await asyncio.sleep(0.01)  # 让 decide 打印完这一段提示、停在读行处
            ui._out_q.put_nowait(line)

    typist_task = asyncio.create_task(typist())
    decision = await asyncio.wait_for(ui.decide(value), timeout=2)
    await typist_task
    return ui, decision


def test_format_ask_renders_why_question_and_numbered_options():
    text = format_ask(ASK)
    assert "为什么问: 工作区里有两个同名函数，猜错要重做" in text
    assert "问题: 改哪个？" in text
    assert "1. a.py 里的" in text and "2. c.py 里的" in text  # 带序号：终端侧靠序号选
    assert "调用ID: call_ask" in text


def test_format_ask_without_options_says_free_form():
    text = format_ask({"type": GATE_ASK_USER, "question": "还有别的想法吗？", "options": []})
    assert "没有给定选项" in text
    assert "为什么问" not in text  # why 为空就不打这一行，不留空壳


def test_ask_takes_option_and_supplement_both():
    """两段都收：选了第 2 项 + 补一句——两段一起带回（补充不是"替代选项"，是叠加）。"""
    _, decision = asyncio.run(_ask(["2", "别动 b.py"]))

    assert decision == Decision(
        kind="answer", option_indexes=[1], supplement="别动 b.py"
    )
    assert decision.unanswered is False


def test_ask_free_text_on_first_line_skips_second_question(capsys):
    """第一行不是序号而是文本 → 用户懒得先选、直接说方案：当作补充，**不再问第二行**。"""
    _, decision = asyncio.run(_ask(["两个都改"]))

    assert decision == Decision(kind="answer", supplement="两个都改")
    assert "补充说明" not in capsys.readouterr().out  # 第二问没问


def test_ask_option_only_is_a_valid_half_answer():
    _, decision = asyncio.run(_ask(["1", ""]))

    assert decision.option_indexes == [0]
    assert decision.supplement is None
    assert decision.unanswered is False  # 只选不补是完整回答，不是没答
    # 选项原文不再随决定走（工具自己持有 options，消费端按序号取）——字段被删掉，别加回来
    assert not hasattr(decision, "option_text")


def test_ask_supplement_only_is_a_valid_answer():
    """**没选任何给定选项、只写了一段方案，是有效回答**——模型最容易在这里读成"没回答"。"""
    _, decision = asyncio.run(_ask(["", "我改 t.py 里那个"]))

    assert decision.option_indexes == [] and decision.supplement == "我改 t.py 里那个"
    assert decision.unanswered is False


def test_ask_all_blank_is_unanswered():
    _, decision = asyncio.run(_ask(["", ""]))

    assert decision == Decision(kind="answer")
    assert decision.unanswered is True


def test_ask_out_of_range_index_is_reasked(capsys):
    """单选严格：越界序号**提示并重问**，不是静默丢成"没选"（重输一次比丢一半强）。"""
    _, decision = asyncio.run(_ask(["9", "还是改 a"]))

    out = capsys.readouterr().out
    assert "序号超出范围" in out
    assert out.count("选择序号") == 2, "第一问应该被重问一次"
    # 第二次敲的不是序号而是文本 → 整串当补充，语义与"首行就是文本"完全一致
    assert decision.option_indexes == [] and decision.supplement == "还是改 a"


def test_ask_eof_on_first_line_is_unanswered():
    _, decision = asyncio.run(_ask([None]))

    assert decision.unanswered is True


def test_ask_eof_on_second_line_keeps_the_option_already_chosen():
    """第二问 EOF：**半份回答比没有好**——已经选中的那项照常带回去。"""
    _, decision = asyncio.run(_ask(["2", None]))

    assert decision.option_indexes == [1]
    assert decision.supplement is None


# ------------------------- 提问面板：复选（select="many"） -------------------------


def test_ask_many_takes_several_indexes():
    _, decision = asyncio.run(_ask(["1,3", ""], ASK_MANY))

    assert decision.option_indexes == [0, 2]
    assert decision.unanswered is False


@pytest.mark.parametrize("typed", ["1,3", "1，3", "1 3", " 1 , 3 "])
def test_ask_many_accepts_the_three_separators(typed):
    """分隔符容错：半角逗号 / 全角逗号 / 空格——中文输入法下最容易敲出的就这三种。"""
    _, decision = asyncio.run(_ask([typed, ""], ASK_MANY))

    assert decision.option_indexes == [0, 2]


def test_ask_many_drops_out_of_range_indexes_and_keeps_the_rest(capsys):
    """复选宽容：**丢掉越界项、保留合法的**（单选那边是重问，两种模态容错空间不同）。"""
    _, decision = asyncio.run(_ask(["1,9", ""], ASK_MANY))

    assert decision.option_indexes == [0]
    # 宽容但**不静默**：得告诉人丢了什么，否则他会以为 9 也算数了
    assert "9" in capsys.readouterr().out


def test_ask_many_dedupes_repeated_indexes():
    _, decision = asyncio.run(_ask(["1,1,2", ""], ASK_MANY))

    assert decision.option_indexes == [0, 1]


def test_ask_many_blank_means_no_selection_and_supplement_still_counts():
    """复选下"一个都不选"与单选同语义：空选择 + 补充**仍是有效回答**（全局只留一条判据）。"""
    _, decision = asyncio.run(_ask(["", "两个都别动"], ASK_MANY))

    assert decision.option_indexes == [] and decision.supplement == "两个都别动"
    assert decision.unanswered is False


def test_ask_single_reasks_when_the_user_gives_several_indexes(capsys):
    """单选遇多值：**提示并重问**——静默取第一个会把用户明确表达的第二个意图吞掉。"""
    _, decision = asyncio.run(_ask(["1,2", "2", ""], ASK))  # ASK 只有 2 项，且是单选

    out = capsys.readouterr().out
    assert "单选题" in out
    assert decision.option_indexes == [1], "重问后第二次输入的那个序号才作数"


def test_ask_single_reask_can_be_escaped_by_eof():
    """重问**必须可逃逸**：人在重问处 EOF（或干脆不答）不能把面板锁死成死循环。"""
    _, decision = asyncio.run(_ask(["9", None], ASK))

    assert decision.unanswered is True


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("²", None),  # isdigit() 会放它进来，但 int("²") 抛 ValueError——面板不能因此炸掉
        ("1²", None),
        ("１", [1]),  # 全角数字照收（isdecimal 为真且 int() 认）；返回的是**1 起始**原始序号
        ("abc", None),
        ("1个", None),
        (",", []),
    ],
)
def test_parse_option_indexes_never_crashes_and_never_misreads(typed, expected):
    """解析器的两条底线：**不抛异常**（面板不该被一个怪字符炸掉）、**不误判**（不像就放行当文本）。"""
    assert parse_option_indexes(typed) == expected


def test_format_ask_hints_multi_select_only_on_many_questions():
    """不给这行提示，人不会知道能一次输入多个序号；单选那边则不该出现这句噪音。"""
    assert "可多选" in format_ask(ASK_MANY)
    assert "可多选" not in format_ask(ASK)


# ------------------------- markdown 渲染（模型答复） -------------------------


def test_render_markdown_formats_markdown():
    """markdown 正文渲染成终端文本：标记符被吃掉、结构保留（宽度固定便于断言）。"""
    pytest.importorskip("rich")
    from rich.console import Console

    buf = StringIO()
    ok = render_markdown(
        "# 标题\n\n这是 **粗体**。\n\n- 甲\n- 乙\n",
        console=Console(file=buf, width=40),
    )

    out = buf.getvalue()
    assert ok is True  # True = 已渲染，调用方不必再原样打印
    assert "标题" in out
    assert "**" not in out  # markdown 标记被消费
    assert "•" in out  # 无序列表渲染成 bullet


def test_render_markdown_falls_back_when_rich_missing(monkeypatch):
    """rich 没装（可选依赖）→ 返回 False，由调用方退回纯文本。"""
    monkeypatch.setattr(panels, "Markdown", None)
    assert panels.render_markdown("# 标题") is False


def test_render_markdown_swallows_render_error(monkeypatch):
    """渲染抛错也只降级成纯文本——显示层的问题不能吞掉一整轮答复。"""

    def _boom(_text):
        raise RuntimeError("渲染炸了")

    monkeypatch.setattr(panels, "Markdown", _boom)
    assert panels.render_markdown("正文") is False


def test_render_markdown_blank_text_is_noop():
    """空白答复视为"无可渲染"返回 True：不打印，调用方也不会多打一行空行。"""
    assert render_markdown("   \n") is True


def test_terminal_ui_emit_uses_markdown_when_available(capsys, monkeypatch):
    """渲染可用时 emit 走 render_markdown，答复不再是原样带标记的文本。"""
    seen: dict = {}

    def _fake(text, *args, **kwargs):
        seen["text"] = text
        return True

    monkeypatch.setattr(tui_ui, "render_markdown", _fake)
    TerminalUI().emit(TurnFinished(text="# 标题\n\n**粗体**"))

    assert seen["text"] == "# 标题\n\n**粗体**"
    out = capsys.readouterr().out
    assert "#" not in out and "**" not in out  # 原样标记没漏到 stdout


def test_terminal_ui_emit_falls_back_to_plain_text(capsys, monkeypatch):
    """渲染不可用时 emit 原样打印答复——宁可朴素，也不能让人看不到答复。"""
    monkeypatch.setattr(tui_ui, "render_markdown", lambda *a, **k: False)

    TerminalUI().emit(TurnFinished(text="# 原始 **文本**"))

    assert "# 原始 **文本**" in capsys.readouterr().out
