"""app/agent/nodes.py::format_tool_result 的测试（对应 EXCEPTION_DESIGN.md §10 L4）。

注意：import app.agent.nodes 会触发 app.agent/__init__ → graph → model，
而 model 顶层调用 get_settings()，因此运行本文件要求 .env 存在（CLAUDE.md 前提）。
"""
import json

from app.agent.nodes import format_tool_result
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


def _mcp_block(payload, block_id="lc_random"):
    """构造 langchain MCP 适配器返回的 content block（TextContent → dict）。"""
    return {"type": "text", "text": payload, "id": block_id}


def test_format_tool_result_mcp_blocks_rebuilds_toolresult():
    block = _mcp_block(json.dumps(
        {"success": True, "content": "1. 标题\n   http://x", "error_type": None},
        ensure_ascii=False,
    ))
    assert format_tool_result([block]) == "1. 标题\n   http://x"


def test_format_tool_result_mcp_blocks_error_prefix():
    block = _mcp_block(json.dumps(
        {"success": False, "content": "search_depth 必须是 basic", "error_type": "invalid_argument"},
        ensure_ascii=False,
    ))
    assert format_tool_result([block]) == "[invalid_argument] search_depth 必须是 basic"


def test_format_tool_result_mcp_blocks_plain_text():
    block = _mcp_block("纯文本结果")
    assert format_tool_result([block]) == "纯文本结果"


def test_format_tool_result_mcp_blocks_non_toolresult_json():
    block = _mcp_block('{"foo": 1}')
    assert format_tool_result([block]) == '{"foo": 1}'


def test_format_tool_result_mcp_blocks_drops_id_and_non_text():
    blocks = [
        _mcp_block("第一段", block_id="lc_1"),
        {"type": "image", "base64": "xxx"},
    ]
    assert format_tool_result(blocks) == "第一段"
