"""CompactNode 轮末压缩单测（假消息驱动，无真实 MCP / 无 LLM）。

用 scripted 消息序列直接 asyncio.run 驱动 CompactNode(content_budget_chars=…)，
再经 langgraph 的 add_messages reducer 合并，断言"原位替换 + 折叠区/保留区"与摘要文本。
需要导出 WORKSPACE_PATH（app.agent 包 __init__ 会 import graph→mcp 链）。
"""
import asyncio

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph.message import add_messages

from app.agent.nodes import CompactNode, needs_compact
from app.platform.tool_results import coerce_tool_result

# 触发折叠的小预算：任何有内容的历史都会超
BUDGET = 40


def _tc(name, cid):
    return {"name": name, "args": {}, "id": cid, "type": "tool_call"}


def _run(messages, notes=None, budget=BUDGET):
    """以给定预算实例化 CompactNode 后直驱，返回节点产出的 state 切片。"""
    state = {"messages": messages}
    if notes is not None:
        state["notes"] = notes
    node = CompactNode(content_budget_chars=budget)
    return asyncio.run(node(state))


def _merged(messages, node_out):
    """把节点返回的 messages 更新经 add_messages 合并，模拟真实 reducer。"""
    return add_messages(list(messages), node_out["messages"])


def _pad(n, text="x"):
    return text * n


#------------------ 用例 ------------------

def test_under_budget_is_noop():
    """预算内（用默认 60000）→ 直接 return {}（幂等、零成本）。"""
    messages = [
        HumanMessage(content="hi", id="h1"),
        AIMessage(content="你好", id="a1"),
    ]
    state = {"messages": messages}
    assert asyncio.run(CompactNode()(state)) == {}


def test_folds_older_round_and_replaces_in_place():
    """跨两轮超预算：只折更早轮的工具块；摘要 SystemMessage 原位替换；尾部仍是最终答复。"""
    messages = [
        HumanMessage(content="读 src/x.py 并报告", id="h1"),
        AIMessage(content="读取 src/x.py 布局", tool_calls=[_tc("read_file", "c1")], id="a1"),
        ToolMessage(content="x" * 200 + "\n一些文件内容", tool_call_id="c1", name="read_file", id="t1"),
        AIMessage(content="我读了 src/x.py。", id="ans1"),
        HumanMessage(content="在此基础上继续", id="h2"),
        AIMessage(content="跑一下测试", tool_calls=[_tc("run_command", "c2")], id="a2"),
        ToolMessage(content="exit=0 · 12 passed", tool_call_id="c2", name="run_command", id="t2"),
        AIMessage(content="已完成并验证通过。", id="ans2"),
    ]
    out = _run(messages)

    assert "messages" in out
    # 恰一条 SystemMessage(摘要) + 一条 RemoveMessage(t1)，无 notes 新增
    sys_msgs = [m for m in out["messages"] if isinstance(m, SystemMessage)]
    rem_msgs = [m for m in out["messages"] if isinstance(m, RemoveMessage)]
    assert len(sys_msgs) == 1 and len(rem_msgs) == 1
    assert rem_msgs[0].id == "t1"
    assert "notes" not in out

    summary = sys_msgs[0].content
    assert "工作日志: 读取 src/x.py 布局" in summary
    assert "工具调用: read_file" in summary
    assert "状态: ok" in summary

    # 原位替换：SystemMessage id == 最早被删消息 id(a1)，reducer 合并在原位置
    assert sys_msgs[0].id == "a1"
    merged = _merged(messages, out)
    # a1 处现在是 SystemMessage（原 AIMessage 已被顶替），且只此一条 id=a1
    a1_msgs = [m for m in merged if m.id == "a1"]
    assert len(a1_msgs) == 1 and isinstance(a1_msgs[0], SystemMessage)
    assert isinstance(merged[1], SystemMessage) and merged[1].id == "a1"
    assert "t1" not in {m.id for m in merged}                     # TM1 已被 RemoveMessage 删除
    assert merged[-1].content == "已完成并验证通过。"                # 尾部仍是本轮最终答复
    # 保留区不动：当前轮的 Human/工具/答复、以及旧轮的 Human 与无 tool_calls 答复都在
    assert {m.id for m in merged} == {"h1", "a1", "ans1", "h2", "a2", "t2", "ans2"}


def test_skips_plain_aimessage():
    """无 tool_calls 的 AIMessage（答复）不会被折叠。"""
    messages = [
        HumanMessage(content="甲", id="h1"),
        AIMessage(content="旧轮答复，不带工具调用。", id="ans_old"),
        HumanMessage(content="乙" + _pad(200), id="h2"),
        AIMessage(content="本轮答复。", id="ans_new"),
    ]
    out = _run(messages)
    assert out == {}  # 无可折工具块 → 即使超预算也不动


def test_incomplete_block_is_kept():
    """块内 tool_call 无兑现 ToolMessage（悬空）→ 整块保留，不产生孤儿。"""
    messages = [
        HumanMessage(content=_pad(300), id="h1"),
        AIMessage(content="尝试删目录", tool_calls=[_tc("delete_dir", "c9")], id="a9"),
        HumanMessage(content="追问", id="h2"),
        AIMessage(content="答复", id="ans2"),
    ]
    out = _run(messages)
    assert out == {}


def test_denied_status_rendered():
    """生产端结构化戳 decision=denied → 摘要状态 denied（未执行）。"""
    messages = [
        HumanMessage(content=_pad(100), id="h1"),
        AIMessage(content="删除临时目录 foo", tool_calls=[_tc("delete_dir", "c1")], id="a1"),
        ToolMessage(
            content="[approval_denied] 工具调用被人工拒绝，未执行。", tool_call_id="c1",
            name="delete_dir", id="t1", additional_kwargs={"decision": "denied"},
        ),
        HumanMessage(content="换别的方案", id="h2"),
        AIMessage(content="好的。", id="ans2"),
    ]
    out = _run(messages)
    merged = _merged(messages, out)
    sys_msg = next(m for m in out["messages"] if isinstance(m, SystemMessage))
    assert "状态: denied（未执行）" in sys_msg.content
    assert "工作日志: 删除临时目录 foo" in sys_msg.content
    assert merged[-1].content == "好的。"


def test_failed_status_rendered_from_content_prefix():
    """无结构化戳、仅 content 前缀的历史消息 → 回退判定 failed。"""
    messages = [
        HumanMessage(content=_pad(100), id="h1"),
        AIMessage(content="写文件到越界路径", tool_calls=[_tc("write_file", "c1")], id="a1"),
        ToolMessage(
            content="[workspace_violation] 路径越界：../../etc/passwd", tool_call_id="c1",
            name="write_file", id="t1",
        ),
        HumanMessage(content="嗯", id="h2"),
        AIMessage(content="明白。", id="ans2"),
    ]
    out = _run(messages)
    sys_msg = next(m for m in out["messages"] if isinstance(m, SystemMessage))
    assert "状态: failed（workspace_violation）" in sys_msg.content


def test_stamped_error_type_reads_struct():
    """生产端结构化戳(error_type)优先于 content，无需解析前缀。"""
    messages = [
        HumanMessage(content=_pad(100), id="h1"),
        AIMessage(content="读越界文件", tool_calls=[_tc("read_file", "c1")], id="a1"),
        ToolMessage(content="一些无前缀的正文", tool_call_id="c1", name="read_file", id="t1",
                    additional_kwargs={"error_type": "workspace_violation"}),
        HumanMessage(content="嗯", id="h2"),
        AIMessage(content="明白。", id="ans2"),
    ]
    out = _run(messages)
    sys_msg = next(m for m in out["messages"] if isinstance(m, SystemMessage))
    assert "状态: failed（workspace_violation）" in sys_msg.content


def test_web_search_result_archives_to_notes():
    """联网四工具的成功正文 → 进 notes，摘要行尾带 详情见 notes#rN。"""
    messages = [
        HumanMessage(content=_pad(50), id="h1"),
        AIMessage(content="调研光伏市场 2026", tool_calls=[_tc("web_search", "c1")], id="a1"),
        ToolMessage(content="# 光伏市场\n2026 年全球光伏装机…" + _pad(300), tool_call_id="c1",
                    name="web_search", id="t1"),
        HumanMessage(content="继续", id="h2"),
        AIMessage(content="已汇总。", id="ans2"),
    ]
    out = _run(messages)
    assert "notes" in out and "notes#r1" in out["notes"]
    note = out["notes"]["notes#r1"]
    assert note["source_tool"] == "web_search"   # kind 字段已于 2026-09-22 删除（悬空：只写不读）
    assert "光伏" in note["content"]

    sys_msg = next(m for m in out["messages"] if isinstance(m, SystemMessage))
    assert "详情见 notes#r1" in sys_msg.content
    assert "状态: ok" in sys_msg.content


def test_existing_notes_ref_continues_numbering():
    """已有 notes 时新归档编号接着最大号往后排。"""
    messages = [
        HumanMessage(content=_pad(50), id="h1"),
        AIMessage(content="深挖技术", tool_calls=[_tc("deep_research", "c1")], id="a1"),
        ToolMessage(content="某研究报告正文" + _pad(200), tool_call_id="c1",
                    name="deep_research", id="t1"),
        HumanMessage(content="嗯", id="h2"),
        AIMessage(content="好了。", id="ans2"),
    ]
    out = _run(messages, notes={"notes#r3": {"title": "旧", "content": "旧", "created": 1, "size": 1}})
    assert "notes#r4" in out["notes"]
    assert out["notes"]["notes#r4"]["source_tool"] == "deep_research"


def test_single_round_never_folds():
    """整个会话只有一条 HumanMessage → 折叠区为空，即使超预算也 no-op。"""
    messages = [
        HumanMessage(content="一个很长的单轮问题" + _pad(300), id="h1"),
        AIMessage(content="读文件", tool_calls=[_tc("read_file", "c1")], id="a1"),
        ToolMessage(content="x" * 400, tool_call_id="c1", name="read_file", id="t1"),
        AIMessage(content="读完了。", id="ans1"),
    ]
    out = _run(messages)
    assert out == {}


def test_coerce_tool_result_reconstructs_mcp_payload():
    """_coerce_tool_result 能从 MCP content-block 形态还原 ToolResult，否则 None。"""
    from app.schema.agent_schema import ToolResult

    payload = [{"type": "text",
                "text": '{"success": false, "content": "越界", "error_type": "workspace_violation"}'}]
    tr = coerce_tool_result(payload)
    assert tr is not None and tr.success is False and tr.error_type == "workspace_violation"
    assert coerce_tool_result([{"type": "text", "text": "普通文本"}]) is None
    assert coerce_tool_result(ToolResult(success=True, content="ok")) is not None
    assert coerce_tool_result("纯字符串") is None


def test_tm_status_prefers_stamp_over_content():
    """_tm_status 优先读 additional_kwargs 结构化戳。"""
    stamped = ToolMessage(content="成功文案", tool_call_id="c", name="run_command",
                          additional_kwargs={"error_type": "internal_error"})
    assert CompactNode._tm_status(stamped) == ("failed", "internal_error")

    denied = ToolMessage(content="随便", tool_call_id="c", name="x",
                         additional_kwargs={"decision": "denied"})
    assert CompactNode._tm_status(denied) == ("denied", "未执行")

    ok = ToolMessage(content="exit=0", tool_call_id="c", name="run_command")
    assert CompactNode._tm_status(ok) == ("ok", "")


def test_custom_budget_param_controls_trigger():
    """预算可作为构造参数传入；超它才折叠。"""
    big = [
        HumanMessage(content=_pad(100), id="h1"),
        AIMessage(content="读 x", tool_calls=[_tc("read_file", "c1")], id="a1"),
        ToolMessage(content=_pad(200), tool_call_id="c1", name="read_file", id="t1"),
        HumanMessage(content="嗯", id="h2"),
        AIMessage(content="完毕", id="ans2"),
    ]
    # 预算极大 → no-op；预算极小 → 折叠
    noop = asyncio.run(CompactNode(content_budget_chars=10 ** 6)({"messages": big}))
    assert noop == {}
    out = _run(big, budget=50)
    assert any(isinstance(m, SystemMessage) for m in out["messages"])


def test_needs_compact_budget_boundary():
    """路由的预算判定：字符数必须严格超过 budget 才需压缩（= budget 不触发）。"""
    exactly = [HumanMessage(content="x" * 40, id="h1")]
    over = [HumanMessage(content="x" * 41, id="h1")]
    under = [HumanMessage(content="x" * 39, id="h1")]
    assert needs_compact({"messages": over}, budget=40) is True
    assert needs_compact({"messages": exactly}, budget=40) is False
    assert needs_compact({"messages": under}, budget=40) is False
    assert needs_compact({"messages": []}, budget=40) is False
