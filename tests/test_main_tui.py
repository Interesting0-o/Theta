"""app/main.py 的 TUI 辅助函数测试：get_agent_db_path / truncate / format_tool_approval。

注意：import app.main 会触发 app.agent/__init__ → graph → model，
而 model 顶层调用 get_settings()，因此运行本文件要求 .env 存在（CLAUDE.md 前提）。
"""
from pathlib import Path
from types import SimpleNamespace

from app.main import format_tool_approval, get_agent_db_path, truncate


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
