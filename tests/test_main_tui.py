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

from app.schema.ui_schema import TurnFinished
from app.tui import panels
from app.tui import ui as tui_ui
from app.tui.panels import format_tool_approval, render_markdown, truncate
from app.tui.runner import resolve_tui_workspace
from app.tui.ui import TerminalUI


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
        assert await ui.decide({"tool_name": "write_file"}) is False  # 无人确认 → 按未批准

    asyncio.run(asyncio.wait_for(go(), timeout=2))


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
