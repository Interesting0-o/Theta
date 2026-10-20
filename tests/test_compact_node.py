"""CompactNode 轮末压缩单测（假消息驱动，无真实 MCP / 无 LLM）。

用 scripted 消息序列直接 asyncio.run 驱动 CompactNode()，再经 langgraph 的 add_messages
reducer 合并，断言"原位替换 + 折叠区/保留区"与摘要文本。轮末压缩自 2026-10-20 起无条件
启用（无字符预算门），因此测试只需构造"可折块"，不再用预算参数控制触发。
需要导出 WORKSPACE_PATH（app.agent 包 __init__ 会 import graph→mcp 链）。
"""
import asyncio

import pytest

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

def _tc(name, cid):
    return {"name": name, "args": {}, "id": cid, "type": "tool_call"}


def _run(messages, notes=None):
    """直驱 CompactNode，返回节点产出的 state 切片。"""
    state = {"messages": messages}
    if notes is not None:
        state["notes"] = notes
    node = CompactNode()
    return asyncio.run(node(state))


def _merged(messages, node_out):
    """把节点返回的 messages 更新经 add_messages 合并，模拟真实 reducer。"""
    return add_messages(list(messages), node_out["messages"])


def _by_id(messages, message_id):
    return next(m for m in messages if m.id == message_id)


def _pad(n, text="x"):
    return text * n


#------------------ 用例 ------------------

def test_folds_without_budget_gate():
    """轮末压缩无条件启用：历史再小，只要有可折块就折（没有 60000 预算门）。"""
    messages = [
        HumanMessage(content="hi", id="h1"),
        AIMessage(content="读 x", tool_calls=[_tc("read_file", "c1")], id="a1"),
        ToolMessage(content="x", tool_call_id="c1", name="read_file", id="t1"),
        AIMessage(content="读完了", id="ans1"),
        HumanMessage(content="继续", id="h2"),
        AIMessage(content="好", id="ans2"),
    ]
    out = _run(messages)
    assert any(isinstance(m, SystemMessage) for m in out["messages"])


def test_unconsumed_block_is_kept():
    """旧轮以 ToolMessage 收尾（块后没有 AIMessage）→ 未被消费，不折。"""
    messages = [
        HumanMessage(content="读 x", id="h1"),
        AIMessage(content="读 x", tool_calls=[_tc("read_file", "c1")], id="a1"),
        ToolMessage(content="x", tool_call_id="c1", name="read_file", id="t1"),
        HumanMessage(content="新问题", id="h2"),
        AIMessage(content="好", id="ans2"),
    ]
    assert _run(messages) == {}


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
        AIMessage(content="已被拒，先不删", id="ans1"),
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
        AIMessage(content="明白，先停下", id="ans1"),
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
        AIMessage(content="明白，先停下", id="ans1"),
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
        AIMessage(content="已获取结果", id="ans1"),
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
        AIMessage(content="已深挖", id="ans1"),
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


def test_needs_compact_budget_boundary():
    """轮内压缩预留的预算判定：字符数必须严格超过 budget 才需压缩（= budget 不触发）。"""
    exactly = [HumanMessage(content="x" * 40, id="h1")]
    over = [HumanMessage(content="x" * 41, id="h1")]
    under = [HumanMessage(content="x" * 39, id="h1")]
    assert needs_compact({"messages": over}, budget=40) is True
    assert needs_compact({"messages": exactly}, budget=40) is False
    assert needs_compact({"messages": under}, budget=40) is False
    assert needs_compact({"messages": []}, budget=40) is False


#------------------ 编排工具：不折（避免误伤） ------------------

ORCHESTRATE_TOOL_NAMES = [
    "create_plan",
    "update_plan_step",
    "clear_plan",
    "read_note",
    "ask_user",
    "write_memory",
    "read_memory",
    "get_skill",
    "drop_skill",
]


def _orchestration_block(name):
    return [
        HumanMessage(content=_pad(50), id="h1"),
        AIMessage(content="执行编排", tool_calls=[_tc(name, "c1")], id="a1"),
        ToolMessage(content="编排回执正文", tool_call_id="c1", name=name, id="t1"),
        AIMessage(content="处理完了", id="ans1"),
        HumanMessage(content="继续", id="h2"),
        AIMessage(content="好", id="ans2"),
    ]


def test_orchestrate_tool_set_comes_from_tool_json():
    """编排集合由 tool.json 的 source 派生，不是手写名单。"""
    node = CompactNode()
    assert set(ORCHESTRATE_TOOL_NAMES) <= node.orchestrate_tools
    assert "run_command" not in node.orchestrate_tools
    assert "read_file" not in node.orchestrate_tools
    assert "dispatch_subtasks" not in node.orchestrate_tools


@pytest.mark.parametrize("name", ORCHESTRATE_TOOL_NAMES)
def test_orchestration_only_block_is_never_folded(name):
    """整块都是编排工具：计划/回答/记忆/技能回执一律原样保留。"""
    assert _run(_orchestration_block(name)) == {}


def test_ask_user_answer_survives_turn_end():
    """提问回答是单轮 state，折掉就永久丢失——必须保留。"""
    messages = [
        HumanMessage(content="选方案", id="h1"),
        AIMessage(content="需要你拍板", tool_calls=[_tc("ask_user", "c1")], id="a1"),
        ToolMessage(
            content="[user_answer] 用户选择了：1. 方案 A\n用户补充：别动旧兼容层",
            tool_call_id="c1",
            name="ask_user",
            id="t1",
        ),
        AIMessage(content="按方案 A 做", id="ans1"),
        HumanMessage(content="继续", id="h2"),
        AIMessage(content="好", id="ans2"),
    ]
    assert _run(messages) == {}


def test_mixed_block_folds_only_non_orchestration_calls():
    """混合块：编排调用与回执留下，普通调用折叠，AIMessage 的 tool_calls 原位缩减。"""
    messages = [
        HumanMessage(content="推进并验证", id="h1"),
        AIMessage(
            content="先更新计划再跑测试",
            tool_calls=[
                _tc("update_plan_step", "c1"),
                _tc("run_command", "c2"),
            ],
            id="a1",
        ),
        ToolMessage(
            content="步骤 1 已标记为 done\n- [x] 1. 改代码",
            tool_call_id="c1",
            name="update_plan_step",
            id="t1",
        ),
        ToolMessage(content="exit=0 · 12 passed", tool_call_id="c2", name="run_command", id="t2"),
        AIMessage(content="已完成并验证。", id="ans1"),
        HumanMessage(content="继续", id="h2"),
        AIMessage(content="好", id="ans2"),
    ]
    out = _run(messages)

    sys_msgs = [m for m in out["messages"] if isinstance(m, SystemMessage)]
    assert len(sys_msgs) == 1
    assert sys_msgs[0].id == "t2"                    # 摘要原位替换被折的普通结果
    assert "工具调用: run_command" in sys_msgs[0].content
    assert "update_plan_step" not in sys_msgs[0].content

    # 保留编排回执：它不在 removed 里；t2 的位置被摘要 SystemMessage 原位顶替
    merged = _merged(messages, out)
    assert "步骤 1 已标记为 done" in _by_id(merged, "t1").content
    assert isinstance(_by_id(merged, "t2"), SystemMessage)

    # AIMessage 原位改写：只留编排调用
    a1 = _by_id(merged, "a1")
    assert isinstance(a1, AIMessage)
    assert [tc["id"] for tc in a1.tool_calls] == ["c1"]
    assert a1.content == "先更新计划再跑测试"


def test_mixed_block_archives_only_folded_web_result():
    """混合块里的 web 调用仍归档 notes，编排回执不归档、摘要行也不提它。"""
    messages = [
        HumanMessage(content="规划并调研", id="h1"),
        AIMessage(
            content="建计划 + 查资料",
            tool_calls=[_tc("create_plan", "c1"), _tc("web_search", "c2")],
            id="a1",
        ),
        ToolMessage(
            content="已生成计划（1 步）\n- [ ] 1. 调研",
            tool_call_id="c1",
            name="create_plan",
            id="t1",
        ),
        ToolMessage(
            content="# 光伏市场\n2026 年装机…" + _pad(300),
            tool_call_id="c2",
            name="web_search",
            id="t2",
        ),
        AIMessage(content="资料已拿到", id="ans1"),
        HumanMessage(content="继续", id="h2"),
        AIMessage(content="好", id="ans2"),
    ]
    out = _run(messages)

    assert "notes#r1" in out["notes"]
    assert "光伏" in out["notes"]["notes#r1"]["content"]
    summary = next(m.content for m in out["messages"] if isinstance(m, SystemMessage))
    assert "工具调用: web_search" in summary
    assert "create_plan" not in summary

    merged = _merged(messages, out)
    assert _by_id(merged, "t1").content.startswith("已生成计划")
    assert [tc["id"] for tc in _by_id(merged, "a1").tool_calls] == ["c1"]


def test_orchestration_block_does_not_block_other_blocks():
    """编排块保留，不影响同一折叠区里另一个普通块被折。"""
    messages = [
        HumanMessage(content="建计划", id="h1"),
        AIMessage(content="建计划", tool_calls=[_tc("create_plan", "c1")], id="a1"),
        ToolMessage(content="已生成计划（1 步）", tool_call_id="c1", name="create_plan", id="t1"),
        AIMessage(content="读文件", tool_calls=[_tc("read_file", "c2")], id="a2"),
        ToolMessage(content="x" * 400, tool_call_id="c2", name="read_file", id="t2"),
        AIMessage(content="读完了", id="ans1"),
        HumanMessage(content="继续", id="h2"),
        AIMessage(content="好", id="ans2"),
    ]
    out = _run(messages)

    sys_msgs = [m for m in out["messages"] if isinstance(m, SystemMessage)]
    assert len(sys_msgs) == 1 and sys_msgs[0].id == "a2"
    merged = _merged(messages, out)
    assert "已生成计划" in _by_id(merged, "t1").content      # 编排块原样保留
    assert "t2" not in {m.id for m in merged}               # 普通块被折
    assert "create_plan" not in sys_msgs[0].content


def test_mixed_block_with_ordinary_before_orchestration_is_kept():
    """普通回执排在编排回执之前：不折整块。

    混合块会把 AIMessage 改写成只留编排调用；若摘要插在编排 ToolMessage 之前，
    就会出现 AIMessage(tool_calls) → SystemMessage → ToolMessage 的非法邻接。
    生产路径 OrchestrateNode 先跑、ToolNode 后跑，不会出现该顺序；这里是防御性用例。
    """
    messages = [
        HumanMessage(content="先跑再更新计划", id="h1"),
        AIMessage(
            content="顺序反了",
            tool_calls=[_tc("run_command", "c1"), _tc("update_plan_step", "c2")],
            id="a1",
        ),
        ToolMessage(content="exit=0", tool_call_id="c1", name="run_command", id="t1"),
        ToolMessage(content="步骤 1 已标记为 done", tool_call_id="c2", name="update_plan_step", id="t2"),
        AIMessage(content="完成", id="ans1"),
        HumanMessage(content="继续", id="h2"),
        AIMessage(content="好", id="ans2"),
    ]
    assert _run(messages) == {}
