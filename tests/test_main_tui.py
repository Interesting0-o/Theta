"""app/tui 的 TUI 辅助函数测试：truncate / format_tool_approval / resolve_tui_workspace。

辅助随 TUI 重构从 app/main.py 迁入 app/tui：truncate/format_tool_approval 在 approval.py，
resolve_tui_workspace 在 driver.py。checkpoint 落点路径已单点到 app/resource.py（见
tests/test_resource.py）。app.tui 模块 import 期不触发 app.agent → 本文件无需 .env。
"""
from types import SimpleNamespace

from app.tui.approval import format_tool_approval, truncate
from app.tui.driver import resolve_tui_workspace


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
