"""CompressNode（轮内字段压缩）单测：假消息驱动，无真实 MCP / 无 LLM。

覆盖：
- 区域：只动最后一条 HumanMessage 之后；
- 终端工具 `ToolMessage.content` 中间截断：保留头尾 + 省略标记；阈值以下不动；
- read_file 重复读取：同路径以最后一次成功读取为准，旧结果指针化；
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
READ_MIN = 10  # 测试里 read_file 去重的最小正文


def _tc(name, cid, **args):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def _run(messages, threshold=THRESHOLD, read_min=READ_MIN):
    return asyncio.run(
        CompressNode(
            threshold_chars=threshold, read_dedup_min_chars=read_min
        )({"messages": messages})
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


#------------------ read_file 重复读取：最后一次为准 ------------------

def _read_block(*reads):
    """reads: (call_id, path, args, content) → H + A(tool_calls) + ToolMessages。

    args 例：{"start_line": 10, "end_line": 20}；不给 = 全文读取。
    """
    messages = [HumanMessage(content="读文件", id="h1")]
    calls = []
    tools = []
    for cid, path, args, content in reads:
        calls.append(_tc("read_file", cid, path=path, **args))
        tools.append(
            ToolMessage(
                content=content,
                tool_call_id=cid,
                name="read_file",
                id=f"t{cid[1:]}",
            )
        )
    messages.append(AIMessage(content="", tool_calls=calls, id="a1"))
    messages.extend(tools)
    return messages


def _ids(out):
    return [m.id for m in out["messages"]]


def test_duplicate_read_file_uses_latest_result():
    """同路径两次读取：旧的变指针，最新的保留正文（不再要求正文相同）。"""
    messages = _read_block(
        ("c1", "a.py", {}, "OLD\n" + "A" * 100),
        ("c2", "a.py", {}, "NEW\n" + "B" * 100),
    )
    out = _run(messages)

    assert _ids(out) == ["t1"]
    merged = _merged(messages, out)
    assert len(merged) == 4
    assert _by_id(merged, "t2").content.startswith("NEW")
    pointer = _by_id(merged, "t1").content
    assert "重复读取" in pointer
    assert "tool_call_id=c2" in pointer
    assert "a.py" in pointer
    assert "最后一次成功读取为准" in pointer


def test_duplicate_read_file_preserves_metadata():
    """只改 content：旧消息的 id / tool_call_id / name 原样保留。"""
    messages = _read_block(
        ("c1", "a.py", {}, "A" * 100),
        ("c2", "a.py", {}, "B" * 100),
    )
    merged = _merged(messages, _run(messages))
    old = _by_id(merged, "t1")
    assert old.tool_call_id == "c1"
    assert old.name == "read_file"
    assert old.id == "t1"


def test_same_content_different_path_is_kept():
    """不同文件：即使正文碰巧相同，也各自保留。"""
    content = "F" * 100
    messages = _read_block(
        ("c1", "a.py", {}, content),
        ("c2", "b.py", {}, content),
    )
    assert _run(messages) == {}


def test_read_file_path_is_normalized_for_comparison():
    """`./a.py` 与 `a.py` 视为同一文件。"""
    messages = _read_block(
        ("c1", "./a.py", {}, "A" * 100),
        ("c2", "a.py", {}, "B" * 100),
    )
    assert _ids(_run(messages)) == ["t1"]


def test_read_file_below_threshold_is_noop():
    """旧正文太短：指针不比正文省多少，不处理。"""
    messages = _read_block(
        ("c1", "a.py", {}, "A" * 5),
        ("c2", "a.py", {}, "B" * 5),
    )
    assert _run(messages, read_min=10) == {}


def test_pointer_only_uses_current_turn():
    """旧轮的 read_file 不参与；当前轮只有一条时无从替换。"""
    content = "F" * 100
    messages = [
        HumanMessage(content="旧", id="h0"),
        AIMessage(content="", tool_calls=[_tc("read_file", "c0", path="a.py")], id="a0"),
        ToolMessage(content=content, tool_call_id="c0", name="read_file", id="t0"),
        HumanMessage(content="新", id="h1"),
        AIMessage(content="", tool_calls=[_tc("read_file", "c1", path="a.py")], id="a1"),
        ToolMessage(content=content, tool_call_id="c1", name="read_file", id="t1"),
    ]
    assert _run(messages) == {}


def test_three_duplicates_all_point_to_last():
    """三条同文件：前两条都指向最后一条，最后一条保留正文。"""
    messages = _read_block(
        ("c1", "a.py", {}, "A" * 100),
        ("c2", "a.py", {}, "B" * 100),
        ("c3", "a.py", {}, "C" * 100),
    )
    out = _run(messages)
    assert {m.id for m in out["messages"]} == {"t1", "t2"}
    merged = _merged(messages, out)
    assert _by_id(merged, "t3").content == "C" * 100
    assert "tool_call_id=c3" in _by_id(merged, "t1").content
    assert "tool_call_id=c3" in _by_id(merged, "t2").content


def test_read_file_dedup_is_idempotent():
    messages = _read_block(
        ("c1", "a.py", {}, "A" * 100),
        ("c2", "a.py", {}, "B" * 100),
    )
    first = _run(messages)
    merged = _merged(messages, first)
    assert _run(merged) == {}


def test_failed_reads_are_neither_replaced_nor_targets():
    """失败回执不参与替换，也不能成为“最新内容”的目标。"""
    messages = _read_block(
        ("c1", "a.py", {}, "A" * 100),
        ("c2", "a.py", {}, "B" * 100),
        ("c3", "a.py", {}, "E" * 100),
    )
    for message in messages:
        if isinstance(message, ToolMessage) and message.tool_call_id == "c3":
            message.additional_kwargs = {"error_type": "io_error"}

    out = _run(messages)
    assert _ids(out) == ["t1"]           # t3 失败，不当目标；t1 指向 t2
    merged = _merged(messages, out)
    assert _by_id(merged, "t2").content == "B" * 100
    assert _by_id(merged, "t3").content == "E" * 100


def test_all_failed_reads_are_noop():
    messages = _read_block(
        ("c1", "a.py", {}, "A" * 100),
        ("c2", "a.py", {}, "B" * 100),
    )
    for message in messages:
        if isinstance(message, ToolMessage):
            message.additional_kwargs = {"error_type": "io_error"}
    assert _run(messages) == {}


def test_latest_short_read_still_replaces_long_old_read():
    """最后读到的是短内容（如文件被截短）：旧的长正文仍然过期，照样指针化。"""
    messages = _read_block(
        ("c1", "a.py", {}, "A" * 100),
        ("c2", "a.py", {}, "tiny"),
    )
    out = _run(messages)
    assert _ids(out) == ["t1"]
    merged = _merged(messages, out)
    assert _by_id(merged, "t2").content == "tiny"
    assert "tool_call_id=c2" in _by_id(merged, "t1").content


def test_latest_empty_read_still_wins():
    """文件被清空：最后一次成功读取是空串，也应作为最新结果。"""
    messages = _read_block(
        ("c1", "a.py", {}, "A" * 100),
        ("c2", "a.py", {}, ""),
    )
    out = _run(messages)
    assert _ids(out) == ["t1"]
    merged = _merged(messages, out)
    assert _by_id(merged, "t2").content == ""
    assert "0 字符" in _by_id(merged, "t1").content


#------------------ 行区间覆盖检查 ------------------

def test_same_range_dedupes():
    messages = _read_block(
        ("c1", "a.py", {"start_line": 10, "end_line": 20}, "A" * 100),
        ("c2", "a.py", {"start_line": 10, "end_line": 20}, "B" * 100),
    )
    assert _ids(_run(messages)) == ["t1"]


def test_latest_narrower_range_does_not_replace():
    """旧读 10-30，最新只读 10-20：不能把 21-30 当成已省略。"""
    messages = _read_block(
        ("c1", "a.py", {"start_line": 10, "end_line": 30}, "A" * 100),
        ("c2", "a.py", {"start_line": 10, "end_line": 20}, "B" * 100),
    )
    assert _run(messages) == {}


def test_latest_wider_range_replaces():
    """最新读 10-30 覆盖旧读 20-30。"""
    messages = _read_block(
        ("c1", "a.py", {"start_line": 20, "end_line": 30}, "A" * 100),
        ("c2", "a.py", {"start_line": 10, "end_line": 30}, "B" * 100),
    )
    assert _ids(_run(messages)) == ["t1"]


def test_range_does_not_replace_full_read():
    """旧的是全文请求，最新只读一个区间：不能替换。"""
    messages = _read_block(
        ("c1", "a.py", {}, "A" * 100),
        ("c2", "a.py", {"start_line": 10, "end_line": 20}, "B" * 100),
    )
    assert _run(messages) == {}


def test_full_read_does_not_replace_range_read():
    """最新是全文请求，旧的是区间：保守保留（大文件“全文”只是预览）。"""
    messages = _read_block(
        ("c1", "a.py", {"start_line": 500, "end_line": 600}, "A" * 100),
        ("c2", "a.py", {}, "B" * 100),
    )
    assert _run(messages) == {}


def test_start_only_later_start_does_not_replace():
    """只给 start_line：最新起点更晚，覆盖不到旧读取的前半段。"""
    messages = _read_block(
        ("c1", "a.py", {"start_line": 10}, "A" * 100),
        ("c2", "a.py", {"start_line": 20}, "B" * 100),
    )
    assert _run(messages) == {}


def test_start_only_earlier_start_replaces():
    """只给 start_line：最新起点更早，覆盖旧读取。"""
    messages = _read_block(
        ("c1", "a.py", {"start_line": 20}, "A" * 100),
        ("c2", "a.py", {"start_line": 10}, "B" * 100),
    )
    assert _ids(_run(messages)) == ["t1"]


#------------------ 参数 / 边界 ------------------

def test_read_file_without_call_args_is_skipped():
    """找不到对应 tool_call（拿不到 path）时不动。"""
    content = "F" * 100
    messages = [
        HumanMessage(content="读", id="h1"),
        ToolMessage(content=content, tool_call_id="c1", name="read_file", id="t1"),
        ToolMessage(content=content, tool_call_id="c2", name="read_file", id="t2"),
    ]
    assert _run(messages) == {}


def test_read_dedup_min_chars_must_be_positive():
    with pytest.raises(ValueError):
        CompressNode(read_dedup_min_chars=0)
