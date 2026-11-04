"""CompressNode（轮内字段压缩）单测：read_file 去重 + 当前轮移动窗口 + 状态回执变薄。

终端输出压缩 2026-10-20 移入 terminal MCP（mcp_service/terminal.py::_compress_output）；
本节点不再做终端截断，只在结果变旧后做指针化。区域边界、去重、区间覆盖、移动窗口、
状态回执、幂等等用例见下。

需要导出 WORKSPACE_PATH（app.agent 包 __init__ 会 import graph→mcp 链）。
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.message import add_messages

from app.agent.nodes import CompressNode

READ_MIN = 10  # 测试里 read_file 去重的最小正文


def _tc(name, cid, **args):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def _run(messages, read_min=READ_MIN, **kwargs):
    return asyncio.run(
        CompressNode(read_dedup_min_chars=read_min, **kwargs)({"messages": messages})
    )


def _merged(messages, out):
    return add_messages(list(messages), out["messages"])


def _by_id(messages, message_id):
    return next(m for m in messages if m.id == message_id)


#------------------ 区域边界 ------------------

def test_no_human_is_noop():
    """没有 HumanMessage → 没有“当前轮”，不动。"""
    messages = [
        ToolMessage(content="x" * 100, tool_call_id="c1", name="read_file", id="t1"),
    ]
    assert _run(messages) == {}


def test_terminal_output_is_left_to_terminal_server():
    """终端输出压缩已下沉到 terminal MCP；CompressNode 不再截断终端 ToolMessage。"""
    messages = [
        HumanMessage(content="跑", id="h1"),
        ToolMessage(
            content="x" * 20000,
            tool_call_id="c1",
            name="run_command",
            id="t1",
        ),
    ]
    assert _run(messages) == {}


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


#------------------ 当前轮移动窗口：高/低水位、保留区、状态回执 ------------------


def _batch(ai_id, items):
    """items: (call_id, name, args, content) → AIMessage + 对应 ToolMessages。"""
    calls = [_tc(name, cid, **args) for cid, name, args, _ in items]
    messages = [AIMessage(content="", tool_calls=calls, id=ai_id)]
    for cid, name, _args, content in items:
        messages.append(
            ToolMessage(content=content, tool_call_id=cid, name=name, id=f"t{cid}")
        )
    return messages


def test_over_high_watermark_masks_old_results():
    """超过高水位：旧块按批压成一条时间线；最近 K 个 block 与最新块保留。"""
    messages = [HumanMessage(content="部署", id="h1")]
    messages += _batch(
        "a1",
        [
            ("c1", "run_command", {"command": "uv sync", "description": "装依赖"},
             "exit_code: 0\n" + "A" * 20000),
            ("c2", "read_file", {"path": "a.py"}, "B" * 20000),
            ("c3", "search_content", {"path": ".", "query": "q"}, "C" * 20000),
        ],
    )
    messages += _batch("a2", [("c4", "read_file", {"path": "b.py"}, "D" * 1000)])

    out = _run(
        messages,
        high_watermark_chars=50000,
        low_watermark_chars=30000,
        keep_blocks=1,
        mask_min_chars=100,
    )
    merged = _merged(messages, out)

    # 批级时间线放在第一条被压消息上，包含同批其它调用的目标
    assert "已压缩" in _by_id(merged, "tc1").content
    assert "cmd=uv sync" in _by_id(merged, "tc1").content
    assert "exit_code: 0" in _by_id(merged, "tc1").content
    assert "path=a.py" in _by_id(merged, "tc1").content
    assert "query=q" in _by_id(merged, "tc1").content
    # 同批其余消息只留短戳
    assert "同批已压缩" in _by_id(merged, "tc2").content
    assert "同批已压缩" in _by_id(merged, "tc3").content
    assert _by_id(merged, "tc4").content == "D" * 1000    # 最近 K=1 个 block


def test_failed_results_are_not_masked():
    """失败/被拒结果保留原样，不参与移动窗口。"""
    messages = [HumanMessage(content="部署", id="h1")]
    messages += _batch(
        "a1",
        [
            ("c1", "run_command", {"command": "bad"}, "exit_code: 1\n" + "E" * 20000),
            ("c2", "read_file", {"path": "a.py"}, "B" * 20000),
        ],
    )
    messages += _batch("a2", [("c3", "read_file", {"path": "b.py"}, "D" * 1000)])
    for message in messages:
        if isinstance(message, ToolMessage) and message.tool_call_id == "c1":
            message.additional_kwargs = {"error_type": "tool_error"}

    out = _run(
        messages,
        high_watermark_chars=10000,
        low_watermark_chars=5000,
        keep_blocks=0,
        mask_min_chars=100,
    )
    merged = _merged(messages, out)
    assert _by_id(merged, "tc1").content.startswith("exit_code: 1")
    assert "已压缩" in _by_id(merged, "tc2").content


def test_latest_batch_is_not_masked():
    """最新一批工具结果还没被模型看过，不能压。"""
    messages = [HumanMessage(content="部署", id="h1")]
    messages += _batch("a1", [("c1", "read_file", {"path": "a.py"}, "A" * 20000)])
    messages += _batch("a2", [("c2", "read_file", {"path": "b.py"}, "B" * 40000)])

    out = _run(
        messages,
        high_watermark_chars=10000,
        low_watermark_chars=5000,
        keep_blocks=0,
        mask_min_chars=100,
    )
    merged = _merged(messages, out)
    assert "已压缩" in _by_id(merged, "tc1").content
    assert _by_id(merged, "tc2").content == "B" * 40000


def test_state_receipts_are_slimmed():
    """plan/memory 回执每个工具只留最近一条；ask_user 保留。"""
    messages = [HumanMessage(content="计划", id="h1")]
    messages += _batch(
        "a1",
        [
            ("c1", "update_plan_step", {"step_id": "1", "status": "in_progress"},
             "步骤 1 in_progress\n" + "A" * 500),
            ("c2", "read_memory", {}, "记忆全文\n" + "B" * 500),
            ("c3", "write_memory", {"type": "decision", "content": "x"},
             "已写入 [m1]\n" + "C" * 500),
        ],
    )
    messages += _batch(
        "a2",
        [
            ("c4", "update_plan_step", {"step_id": "2", "status": "in_progress"},
             "步骤 2 in_progress\n" + "D" * 500),
            ("c5", "read_memory", {}, "最新记忆\n" + "E" * 500),
            ("c6", "write_memory", {"type": "decision", "content": "y"},
             "已写入 [m2]\n" + "F" * 500),
            ("c7", "ask_user", {"why": "w", "question": "q"},
             "[user_answer] 用户选择了：1"),
        ],
    )
    messages += _batch("a3", [("c8", "read_file", {"path": "a.py"}, "R" * 100)])

    out = _run(
        messages,
        high_watermark_chars=1000,
        low_watermark_chars=1,
        keep_blocks=8,
        mask_min_chars=100,
    )
    merged = _merged(messages, out)

    assert "已压缩" in _by_id(merged, "tc1").content
    assert "步骤 1" in _by_id(merged, "tc1").content
    assert "已压缩" in _by_id(merged, "tc2").content
    assert "RUNTIME_STATE" in _by_id(merged, "tc2").content
    assert "已压缩" in _by_id(merged, "tc3").content
    assert "memory.md" in _by_id(merged, "tc3").content
    # 每个工具的最新一条保留
    assert "已压缩" not in _by_id(merged, "tc4").content
    assert "已压缩" not in _by_id(merged, "tc5").content
    assert "已压缩" not in _by_id(merged, "tc6").content
    # ask_user 与最新批保留
    assert _by_id(merged, "tc7").content.startswith("[user_answer]")
    assert _by_id(merged, "tc8").content == "R" * 100


def test_mask_is_idempotent():
    messages = [HumanMessage(content="部署", id="h1")]
    messages += _batch("a1", [("c1", "read_file", {"path": "a.py"}, "A" * 40000)])
    messages += _batch("a2", [("c2", "read_file", {"path": "b.py"}, "B" * 1000)])
    kwargs = dict(
        high_watermark_chars=30000,
        low_watermark_chars=10000,
        keep_blocks=0,
        mask_min_chars=100,
    )
    merged = _merged(messages, _run(messages, **kwargs))
    assert _run(merged, **kwargs) == {}


def test_mask_preserves_metadata():
    messages = [HumanMessage(content="部署", id="h1")]
    messages += _batch("a1", [("c1", "read_file", {"path": "a.py"}, "A" * 40000)])
    messages += _batch("a2", [("c2", "read_file", {"path": "b.py"}, "B" * 100)])
    out = _run(
        messages,
        high_watermark_chars=30000,
        low_watermark_chars=10000,
        keep_blocks=0,
        mask_min_chars=100,
    )
    masked = _by_id(_merged(messages, out), "tc1")
    assert masked.id == "tc1"
    assert masked.tool_call_id == "c1"
    assert masked.name == "read_file"


def test_below_high_watermark_is_noop():
    messages = [HumanMessage(content="部署", id="h1")]
    messages += _batch("a1", [("c1", "read_file", {"path": "a.py"}, "A" * 20000)])
    messages += _batch("a2", [("c2", "read_file", {"path": "b.py"}, "B" * 100)])
    assert _run(
        messages, high_watermark_chars=50000, low_watermark_chars=25000
    ) == {}


@pytest.mark.parametrize(
    "tool_name", ["read_note", "read_event_log", "dispatch_subtasks"]
)
def test_protected_receipts_are_not_masked(tool_name):
    """编排/state 回执与 worker 结论（dispatch_subtasks）在轮内也不压。"""
    messages = [HumanMessage(content="查资料", id="h1")]
    messages += _batch("a1", [("c1", tool_name, {}, "N" * 20000)])
    messages += _batch("a2", [("c2", "read_file", {"path": "a.py"}, "R" * 20000)])
    messages.append(AIMessage(content="完成", id="ans"))

    out = _run(
        messages,
        high_watermark_chars=10000,
        low_watermark_chars=5000,
        keep_blocks=0,
        mask_min_chars=100,
        max_batch_chars=8000,
        min_batch_chars=100,
    )
    merged = _merged(messages, out)
    assert _by_id(merged, "tc1").content == "N" * 20000
    assert "已压缩" in _by_id(merged, "tc2").content


def test_web_and_ask_are_not_masked():
    messages = [HumanMessage(content="部署", id="h1")]
    messages += _batch(
        "a1",
        [
            ("c1", "web_search", {"query": "q"}, "W" * 40000),
            ("c2", "ask_user", {"why": "w", "question": "q"}, "A" * 100),
        ],
    )
    messages += _batch("a2", [("c3", "read_file", {"path": "a.py"}, "R" * 100)])
    assert _run(
        messages,
        high_watermark_chars=30000,
        low_watermark_chars=10000,
        keep_blocks=0,
        mask_min_chars=100,
    ) == {}


#------------------ Phase 2：写操作 args 清理 ------------------


def test_write_args_are_cleared_for_old_blocks():
    """旧写操作的大 content 换成指针；path 与 ToolMessage 回执保留。"""
    messages = [HumanMessage(content="写文件", id="h1")]
    messages += _batch(
        "a1",
        [("c1", "write_file", {"path": "a.py", "content": "X" * 20000}, "已写入 a.py")],
    )
    messages += _batch("a2", [("c2", "read_file", {"path": "b.py"}, "R" * 200)])
    messages.append(AIMessage(content="完成", id="ans"))

    out = _run(
        messages,
        high_watermark_chars=10000,
        low_watermark_chars=5000,
        keep_blocks=1,
        mask_min_chars=100,
        max_batch_chars=30000,
        min_batch_chars=100,
    )
    merged = _merged(messages, out)

    args = _by_id(merged, "a1").tool_calls[0]["args"]
    assert args["path"] == "a.py"
    assert "参数已省略" in args["content"]
    assert "20000" in args["content"]
    assert _by_id(merged, "tc1").content == "已写入 a.py"   # 写回执本身受保护


def test_edit_file_args_are_cleared():
    """edit_file 的 old_string / new_string 都清；path 与 replace_all 保留。"""
    messages = [HumanMessage(content="编辑", id="h1")]
    messages += _batch(
        "a1",
        [("c1", "edit_file", {
            "path": "a.py",
            "old_string": "OLD" * 2000,
            "new_string": "NEW" * 2000,
            "replace_all": False,
        }, "已编辑")],
    )
    messages += _batch("a2", [("c2", "read_file", {"path": "b.py"}, "R" * 200)])
    messages.append(AIMessage(content="完成", id="ans"))

    out = _run(
        messages,
        high_watermark_chars=10000,
        low_watermark_chars=5000,
        keep_blocks=1,
        mask_min_chars=100,
        max_batch_chars=30000,
        min_batch_chars=100,
    )
    merged = _merged(messages, out)

    args = _by_id(merged, "a1").tool_calls[0]["args"]
    assert args["path"] == "a.py"
    assert args["replace_all"] is False
    assert "参数已省略" in args["old_string"]
    assert "参数已省略" in args["new_string"]


def test_recent_write_args_are_kept():
    """保留窗口内的写操作 args 不动。"""
    messages = [HumanMessage(content="写文件", id="h1")]
    messages += _batch(
        "a1",
        [("c1", "write_file", {"path": "a.py", "content": "X" * 20000}, "已写入")],
    )
    messages.append(AIMessage(content="完成", id="ans"))

    assert _run(
        messages,
        high_watermark_chars=10000,
        low_watermark_chars=5000,
        keep_blocks=1,
        mask_min_chars=100,
        max_batch_chars=30000,
        min_batch_chars=100,
    ) == {}


def test_write_args_cleanup_is_idempotent():
    messages = [HumanMessage(content="写文件", id="h1")]
    messages += _batch(
        "a1",
        [("c1", "write_file", {"path": "a.py", "content": "X" * 20000}, "已写入")],
    )
    messages += _batch("a2", [("c2", "read_file", {"path": "b.py"}, "R" * 200)])
    messages.append(AIMessage(content="完成", id="ans"))
    kwargs = dict(
        high_watermark_chars=10000,
        low_watermark_chars=5000,
        keep_blocks=1,
        mask_min_chars=100,
        max_batch_chars=30000,
        min_batch_chars=100,
    )
    merged = _merged(messages, _run(messages, **kwargs))
    assert _run(merged, **kwargs) == {}


#------------------ 批级分组：plan step、语义切点、部分压缩、保留窗口 ------------------


def test_plan_step_change_starts_new_batch():
    """plan step 变化是硬语义边界：前后不在同一个批。"""
    messages = [HumanMessage(content="部署", id="h1")]
    messages += _batch(
        "a1",
        [
            ("c1", "update_plan_step", {"step_id": "1", "status": "in_progress"},
             "step1 " + "A" * 5000),
            ("c2", "read_file", {"path": "a.py"}, "B" * 5000),
        ],
    )
    messages += _batch("a2", [("c3", "read_file", {"path": "b.py"}, "C" * 5000)])
    messages += _batch(
        "a3",
        [
            ("c4", "update_plan_step", {"step_id": "2", "status": "in_progress"},
             "step2 " + "D" * 5000),
            ("c5", "read_file", {"path": "c.py"}, "E" * 5000),
        ],
    )
    messages += _batch("a4", [("c6", "read_file", {"path": "d.py"}, "F" * 100)])
    messages.append(AIMessage(content="完成", id="ans"))

    out = _run(
        messages,
        high_watermark_chars=10000,
        low_watermark_chars=5000,
        keep_blocks=0,
        mask_min_chars=100,
        max_batch_chars=8000,
    )
    merged = _merged(messages, out)
    # c2 在 step1、c5 在 step2，各自是自己批的首条 → 都能拿到完整时间线
    assert "path=a.py" in _by_id(merged, "tc2").content
    assert "path=c.py" in _by_id(merged, "tc5").content
    # 没有跨 step 合并
    assert "path=c.py" not in _by_id(merged, "tc2").content
    assert "path=a.py" not in _by_id(merged, "tc5").content


def test_mixed_block_masks_only_successful_sibling():
    """混合块只压 eligible 的成功结果；写回执保持原文。"""
    messages = [HumanMessage(content="部署", id="h1")]
    messages += _batch(
        "a1",
        [
            ("c1", "write_file", {"path": "x.py"}, "W" * 50),
            ("c2", "search_content", {"path": ".", "query": "q"}, "S" * 20000),
        ],
    )
    messages += _batch("a2", [("c3", "read_file", {"path": "y.py"}, "R" * 100)])
    messages.append(AIMessage(content="完成", id="ans"))

    out = _run(
        messages,
        high_watermark_chars=10000,
        low_watermark_chars=5000,
        keep_blocks=0,
        mask_min_chars=100,
        max_batch_chars=8000,
    )
    merged = _merged(messages, out)
    assert _by_id(merged, "tc1").content == "W" * 50
    assert "已压缩" in _by_id(merged, "tc2").content


def test_keep_blocks_protects_last_blocks():
    """保留窗口按 block 计：最后 K 个 block 原样保留。"""
    messages = [HumanMessage(content="部署", id="h1")]
    messages += _batch("a1", [("c1", "read_file", {"path": "a.py"}, "A" * 20000)])
    messages += _batch("a2", [("c2", "read_file", {"path": "b.py"}, "B" * 20000)])
    messages += _batch("a3", [("c3", "read_file", {"path": "c.py"}, "C" * 20000)])
    messages.append(AIMessage(content="完成", id="ans"))

    out = _run(
        messages,
        high_watermark_chars=10000,
        low_watermark_chars=5000,
        keep_blocks=1,
        mask_min_chars=100,
        max_batch_chars=8000,
    )
    merged = _merged(messages, out)
    assert "已压缩" in _by_id(merged, "tc1").content
    assert "已压缩" in _by_id(merged, "tc2").content
    assert _by_id(merged, "tc3").content == "C" * 20000


def test_batch_pointer_lists_same_batch_targets():
    """同一批的多条结果：首条时间线列出全部目标，其余只留短戳。"""
    messages = [HumanMessage(content="部署", id="h1")]
    messages += _batch(
        "a1",
        [
            ("c1", "read_file", {"path": "a.py"}, "A" * 20000),
            ("c2", "read_file", {"path": "b.py"}, "B" * 20000),
        ],
    )
    messages.append(AIMessage(content="完成", id="ans"))

    out = _run(
        messages,
        high_watermark_chars=10000,
        low_watermark_chars=5000,
        keep_blocks=0,
        mask_min_chars=100,
        max_batch_chars=8000,
    )
    merged = _merged(messages, out)
    pointer = _by_id(merged, "tc1").content
    assert "path=a.py" in pointer
    assert "path=b.py" in pointer
    assert "同批已压缩" in _by_id(merged, "tc2").content


def test_protected_content_does_not_trigger_batch_mask():
    """触发看可压正文：web 结果再大也不触发批级 mask。"""
    messages = [HumanMessage(content="调研", id="h1")]
    messages += _batch("a1", [("c1", "web_search", {"query": "q"}, "W" * 40000)])
    messages += _batch("a2", [("c2", "read_file", {"path": "a.py"}, "R" * 100)])
    messages.append(AIMessage(content="完成", id="ans"))

    assert _run(
        messages,
        high_watermark_chars=30000,
        low_watermark_chars=10000,
        keep_blocks=0,
        mask_min_chars=100,
    ) == {}


def test_budget_cut_prefers_semantic_milestone():
    """预算超限时，切点优先落在里程碑（start_process）而不是硬切。"""
    messages = [HumanMessage(content="部署", id="h1")]
    messages += _batch("a1", [("c1", "read_file", {"path": "a.py"}, "A" * 4000)])
    messages += _batch(
        "a2",
        [("c2", "start_process", {"command": "server"}, "started p1\n" + "B" * 4000)],
    )
    messages += _batch("a3", [("c3", "read_file", {"path": "c.py"}, "C" * 4000)])
    messages.append(AIMessage(content="完成", id="ans"))

    out = _run(
        messages,
        high_watermark_chars=5000,
        low_watermark_chars=1000,
        keep_blocks=0,
        mask_min_chars=100,
        max_batch_chars=10000,
    )
    merged = _merged(messages, out)
    pointer = _by_id(merged, "tc1").content
    assert "start_process" in pointer       # 里程碑把 a2 留在第一批
    assert "path=c.py" not in pointer
    assert "path=c.py" in _by_id(merged, "tc3").content


def test_watermark_validation():
    with pytest.raises(ValueError):
        CompressNode(high_watermark_chars=100, low_watermark_chars=200)
    with pytest.raises(ValueError):
        CompressNode(keep_blocks=-1)
    with pytest.raises(ValueError):
        CompressNode(mask_min_chars=0)
    with pytest.raises(ValueError):
        CompressNode(min_batch_chars=0)
    with pytest.raises(ValueError):
        CompressNode(max_batch_chars=100, min_batch_chars=200)
