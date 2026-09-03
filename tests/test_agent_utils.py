"""app/agent/utils.py::format_tool_result 的测试（对应 EXCEPTION_DESIGN.md §10 L4）。

注意：import app.agent.utils 会触发 app.agent/__init__ → graph → model，
而 model 顶层调用 get_settings()，因此运行本文件要求 .env 存在（CLAUDE.md 前提）。
"""
from types import SimpleNamespace

from app.agent.utils import format_tool_approval, format_tool_result, truncate
from app.schema.agent_schema import ToolResult


def test_format_tool_result_prefixes_error_type():
    result = ToolResult(
        success=False,
        error_type="workspace_violation",
        content="路径 /etc 不在工作区/tmp 内",
    )
    assert format_tool_result(result) == "[workspace_violation] 路径 /etc 不在工作区/tmp 内"


def test_format_tool_result_no_error_type_returns_content():
    result = ToolResult(success=True, content="文件已写入: /ws/demo.txt")
    assert format_tool_result(result) == "文件已写入: /ws/demo.txt"


def test_format_tool_result_internal_error_prefix():
    result = ToolResult(
        success=False,
        error_type="internal_error",
        content="工具内部错误（非输入问题），无需重试。",
    )
    assert format_tool_result(result).startswith("[internal_error]")


def test_format_tool_result_passthrough_string():
    assert format_tool_result("纯文本结果") == "纯文本结果"


def test_format_tool_result_dict_content():
    assert format_tool_result({"content": "来自字典"}) == "来自字典"


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
