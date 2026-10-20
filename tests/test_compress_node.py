"""CompressNode（轮内终端字段压缩）单测：假消息驱动，无真实 MCP / 无 LLM。

覆盖：
- 区域：只动最后一条 HumanMessage 之后；
- 只压缩终端工具的 `ToolMessage.content`；
- 中间截断：保留头尾 + 省略标记；阈值以下不动；
- 不增删消息，保留 id / tool_call_id / name / additional_kwargs；
- 幂等；非终端 / 旧轮消息不动。

需要导出 WORKSPACE_PATH（app.agent 包 __init__ 会 import graph→mcp 链）。
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.message import add_messages

from app.agent.nodes import CompressNode

THRESHOLD = 20  # 头尾各 5


def _tc(name, cid):
    return {"name": name, "args": {}, "id": cid, "type": "tool_call"}


def _run(messages, threshold=THRESHOLD):
    return asyncio.run(
        CompressNode(threshold_chars=threshold)({"messages": messages})
    )


def _merged(messages, out):
    return add_messages(list(messages), out["messages"])


def _by_id(messages, message_id):
    return next(m for m in messages if m.id == message_id)


#------------------ 区域边界 ------------------

def test_no_human_is_noop():
    """没有 HumanMessage → 没有"当前轮"，不动。"""
    messages = [
        ToolMessage(content="x" * 100, tool_call_id="c1", name="run_command", id="t1"),
    ]
    assert _run(messages) == {}


def test_only_current_turn_is_compressed():
    """last_human 之前（旧轮）的终端消息不动，只压当前轮。"""
    messages = [
        HumanMessage(content="旧", id="h1"),
        ToolMessage(content="O" * 100, tool_call_id="c1", name="run_command", id="t1"),
        HumanMessage(content="新", id="h2"),
        ToolMessage(content="N" * 100, tool_call_id="c2", name="run_command", id="t2"),
    ]
    out = _run(messages)
    assert len(out["messages"]) == 1
    assert out["messages"][0].id == "t2"

    merged = _merged(messages, out)
    assert _by_id(merged, "t1").content == "O" * 100
    assert "已截断" in _by_id(merged, "t2").content


#------------------ 阈值 ------------------

def test_at_or_under_threshold_is_noop():
    """长度等于阈值也不截断（严格大于才动手）。"""
    messages = [
        HumanMessage(content="跑", id="h1"),
        ToolMessage(content="x" * THRESHOLD, tool_call_id="c1", name="run_command", id="t1"),
    ]
    assert _run(messages) == {}


def test_truncates_middle_keeping_head_and_tail():
    """超阈值：保留头 5 / 尾 5，中间加省略标记，消息条数不变。"""
    content = "H" * 30 + "M" * 100 + "T" * 30  # 160 字符
    messages = [
        HumanMessage(content="跑", id="h1"),
        ToolMessage(content=content, tool_call_id="c1", name="run_command", id="t1"),
    ]
    out = _run(messages)
    assert len(out["messages"]) == 1
    new = out["messages"][0]

    assert new.id == "t1"
    assert new.tool_call_id == "c1"
    assert new.name == "run_command"
    assert new.content.startswith("H" * 5)
    assert new.content.endswith("T" * 5)
    assert "已截断 150 字符" in new.content
    assert len(new.content) < len(content)

    merged = _merged(messages, out)
    assert len(merged) == 2                    # 不增删消息
    assert merged[1].id == "t1"


def test_threshold_must_leave_room_for_head_and_tail():
    with pytest.raises(ValueError):
        CompressNode(threshold_chars=3)
    node = CompressNode()
    assert node.head_chars == node.threshold_chars // 4
    assert node.tail_chars == node.threshold_chars // 4


#------------------ 工具范围 ------------------

def test_non_terminal_tools_are_untouched():
    """file_io / 联网工具的返回再长也不压。"""
    messages = [
        HumanMessage(content="查", id="h1"),
        ToolMessage(content="x" * 100, tool_call_id="c1", name="read_file", id="t1"),
        ToolMessage(content="y" * 100, tool_call_id="c2", name="web_search", id="t2"),
    ]
    assert _run(messages) == {}


def test_mixed_block_only_terminal_message_truncated():
    """同一条 AIMessage 下的混合调用：只压终端那条 ToolMessage。"""
    messages = [
        HumanMessage(content="跑", id="h1"),
        AIMessage(
            content="混合",
            tool_calls=[_tc("run_command", "c1"), _tc("read_file", "c2")],
            id="a1",
        ),
        ToolMessage(content="R" * 100, tool_call_id="c1", name="run_command", id="t1"),
        ToolMessage(content="F" * 100, tool_call_id="c2", name="read_file", id="t2"),
    ]
    out = _run(messages)
    assert len(out["messages"]) == 1
    assert out["messages"][0].id == "t1"

    merged = _merged(messages, out)
    assert "已截断" in _by_id(merged, "t1").content
    assert _by_id(merged, "t2").content == "F" * 100


def test_all_terminal_tools_are_covered():
    """六个终端工具都在压缩范围内（阈值以下本来就不动，这里用超长触发）。"""
    names = [
        "run_command",
        "start_process",
        "process_wait",
        "process_read",
        "process_kill",
        "process_list",
    ]
    messages = [HumanMessage(content="跑", id="h1")]
    for index, name in enumerate(names):
        messages.append(
            ToolMessage(
                content="x" * 100,
                tool_call_id=f"c{index}",
                name=name,
                id=f"t{index}",
            )
        )
    out = _run(messages)
    assert {m.id for m in out["messages"]} == {f"t{i}" for i in range(len(names))}


#------------------ 保真 / 幂等 ------------------

def test_preserves_metadata_and_stamps():
    """只改 content：id / tool_call_id / name / additional_kwargs 全部保留。"""
    messages = [
        HumanMessage(content="跑", id="h1"),
        ToolMessage(
            content="x" * 100,
            tool_call_id="c1",
            name="run_command",
            id="t1",
            additional_kwargs={"error_type": "command_failed"},
        ),
    ]
    out = _run(messages)
    new = out["messages"][0]
    assert new.id == "t1"
    assert new.tool_call_id == "c1"
    assert new.name == "run_command"
    assert new.additional_kwargs == {"error_type": "command_failed"}
    assert "已截断" in new.content


def test_idempotent():
    """已带截断标记的消息不会再次截断（即使标记让它又超过小阈值）。"""
    messages = [
        HumanMessage(content="跑", id="h1"),
        ToolMessage(content="x" * 100, tool_call_id="c1", name="run_command", id="t1"),
    ]
    first = _run(messages)
    merged = _merged(messages, first)
    assert _run(merged) == {}
