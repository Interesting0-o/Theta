"""app/tui 的 TUI 辅助函数测试：get_agent_db_path / truncate / format_tool_approval。

辅助随 TUI 重构从 app/main.py 迁入 app/tui：truncate/format_tool_approval 在 approval.py，
get_agent_db_path 在 driver.py。app.tui 模块 import 期不触发 app.agent → 本文件无需 .env。
"""
from pathlib import Path
from types import SimpleNamespace

from app.tui.approval import format_tool_approval, truncate
from app.tui.driver import get_agent_db_path


def test_agent_db_path_resolves_under_resource_dir():
    db_path = get_agent_db_path()

    assert db_path.is_absolute()
    assert db_path.parent.name == "resource"
    assert db_path.name == "agent.db"
    assert db_path.parent.exists()


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
