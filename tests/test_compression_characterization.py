"""压缩引擎 characterization 测试（`docs/NODES_REFACTOR_PLAN.md` Stage 0）。

目的：在抽 `app/agent/compression/` 之前，把**当前行为**钉住，让后续结构迁移与算法
修复产生可见 diff。这里刻意断言现状（包括已知缺口），**不是目标态**：

- Stage 0 先以 `【现状】` 断言了三个已知缺口（低水位不停、预算可突破、指针失灵）；
  **Stage 1（2026-11-04）已修算法，这三条断言随之翻转为目标行为**。
- 标 `【不变量】` 的用例断言必须一直成立的安全边界（失败正文保留、未消费块不压等），
  重构前后都不许变。

覆盖 Stage 0 六项：低水位停止、`max_batch_chars` 上界、read 去重指针链、混合块失败
矩阵、steering 提前收口、SQLite 重启幂等；另加 Stage 1 的“失败 plan 调用不改边界”。

需要导出 WORKSPACE_PATH（app.agent 包 __init__ 会 import graph→mcp 链）；SQLite 用例
还会用到 Windows 侧 tmp 目录。
"""
import asyncio

import aiosqlite
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.agent.state import AgentState
from app.agent.nodes import CompactNode, CompressNode


def _tc(name, cid, **args):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def _batch(ai_id, items):
    """items: (call_id, name, args, content) → AIMessage + 对应 ToolMessages。"""
    calls = [_tc(name, cid, **args) for cid, name, args, _ in items]
    messages = [AIMessage(content="", tool_calls=calls, id=ai_id)]
    for cid, name, _args, content in items:
        messages.append(
            ToolMessage(content=content, tool_call_id=cid, name=name, id=f"t{cid}")
        )
    return messages


def _run(messages, **kwargs):
    return asyncio.run(CompressNode(**kwargs)({"messages": messages}))


def _run_compact(messages, notes=None):
    state = {"messages": messages}
    if notes is not None:
        state["notes"] = notes
    return asyncio.run(CompactNode()(state))


def _merged(messages, out):
    return add_messages(list(messages), out["messages"])


def _by_id(messages, message_id):
    return next(m for m in messages if m.id == message_id)


#------------------ 1. 低水位停止（Stage 1 已修） ------------------


def test_batch_mask_stops_at_low_watermark():
    """批级路径逐批压，回到 `low_watermark_chars` 就停手。"""
    messages = [HumanMessage(content="长任务", id="h1")]
    for index in range(3):
        messages += _batch(
            f"a{index + 1}",
            [(f"c{index + 1}", "read_file", {"path": f"{index}.py"}, "A" * 15000)],
        )
    messages.append(AIMessage(content="完成", id="ans"))

    out = _run(
        messages,
        high_watermark_chars=30000,
        low_watermark_chars=20000,
        keep_blocks=0,
        mask_min_chars=100,
        max_batch_chars=30000,
        min_batch_chars=100,
    )
    merged = _merged(messages, out)

    # 第一批 30000 压完后只剩 15000 < 低水位 20000，第三块保持原文。
    assert "已压缩" in _by_id(merged, "tc1").content
    assert "已压缩" in _by_id(merged, "tc2").content
    assert _by_id(merged, "tc3").content == "A" * 15000


#------------------ 2. max_batch_chars 硬上界（Stage 1 已修） ------------------


def test_semantic_cut_respects_max_batch_chars():
    """语义切点不得把首个超预算块带进本批；超限块独立成批。"""
    messages = [HumanMessage(content="部署", id="h1")]
    messages += _batch("a1", [("c1", "read_file", {"path": "a.py"}, "A" * 4000)])
    messages += _batch("a2", [("c2", "read_file", {"path": "b.py"}, "B" * 4000)])
    # 第三块是里程碑（start_process），也是预算循环停下的“首个超限块”
    messages += _batch(
        "a3", [("c3", "start_process", {"command": "server"}, "started p1\n" + "C" * 4000)]
    )
    messages.append(AIMessage(content="完成", id="ans"))

    out = _run(
        messages,
        high_watermark_chars=5000,
        low_watermark_chars=1000,
        keep_blocks=0,
        mask_min_chars=100,
        max_batch_chars=10000,
        min_batch_chars=100,
    )
    merged = _merged(messages, out)

    pointer = _by_id(merged, "tc1").content
    assert "path=a.py" in pointer
    assert "path=b.py" in pointer
    assert "started p1" not in pointer          # 12000 > 10000，超限块不得进第一批
    assert "started p1" in _by_id(merged, "tc3").content


#------------------ 3. read 去重指针链（Stage 1 已修） ------------------


def test_read_dedup_pointer_target_is_protected_from_mask():
    """去重指针的目标（最后一次成功读取）不参与批级 mask，指针链保持可解析。"""
    messages = [HumanMessage(content="读文件", id="h1")]
    messages += _batch("a1", [("c1", "read_file", {"path": "a.py"}, "OLD\n" + "A" * 20000)])
    messages += _batch("a2", [("c2", "read_file", {"path": "a.py"}, "NEW\n" + "B" * 20000)])
    messages.append(AIMessage(content="完成", id="ans"))

    out = _run(
        messages,
        read_dedup_min_chars=1000,
        high_watermark_chars=10000,
        low_watermark_chars=5000,
        keep_blocks=0,
        mask_min_chars=100,
        max_batch_chars=8000,
        min_batch_chars=100,
    )
    merged = _merged(messages, out)

    old = _by_id(merged, "tc1").content
    latest = _by_id(merged, "tc2").content
    assert "tool_call_id=c2" in old
    assert latest == "NEW\n" + "B" * 20000       # 目标保留正文，指针可解析


def test_failed_plan_update_does_not_start_new_batch():
    """失败的 update_plan_step 不改变 active step，分组边界不跟着变。"""
    messages = [
        HumanMessage(content="部署", id="h1"),
        AIMessage(
            content="",
            tool_calls=[_tc("update_plan_step", "c1", step_id="1", status="in_progress")],
            id="a1",
        ),
        ToolMessage(content="步骤 1 in_progress", tool_call_id="c1",
                    name="update_plan_step", id="t1"),
        AIMessage(content="", tool_calls=[_tc("read_file", "c2", path="a.py")], id="a2"),
        ToolMessage(content="A" * 6000, tool_call_id="c2", name="read_file", id="t2"),
        # 这次 step 2 的切换失败了：不能把后面的块切到新 step
        AIMessage(
            content="",
            tool_calls=[_tc("update_plan_step", "c3", step_id="2", status="in_progress")],
            id="a3",
        ),
        ToolMessage(content="更新失败", tool_call_id="c3", name="update_plan_step", id="t3",
                    additional_kwargs={"error_type": "tool_error"}),
        AIMessage(content="", tool_calls=[_tc("read_file", "c4", path="b.py")], id="a4"),
        ToolMessage(content="B" * 6000, tool_call_id="c4", name="read_file", id="t4"),
        AIMessage(content="", tool_calls=[_tc("read_file", "c5", path="c.py")], id="a5"),
        ToolMessage(content="C" * 100, tool_call_id="c5", name="read_file", id="t5"),
        AIMessage(content="完成", id="ans"),
    ]

    out = _run(
        messages,
        high_watermark_chars=5000,
        low_watermark_chars=1000,
        keep_blocks=0,
        mask_min_chars=100,
        max_batch_chars=20000,
        min_batch_chars=100,
    )
    merged = _merged(messages, out)

    pointer = _by_id(merged, "t2").content
    assert "path=a.py" in pointer
    assert "path=b.py" in pointer                # 仍在同一个批里，没有被失败切换切开


#------------------ 4. 混合块失败矩阵 ------------------


@pytest.mark.parametrize(
    "stamp",
    [
        {"error_type": "tool_error"},
        {"decision": "denied"},
    ],
    ids=["failed", "denied"],
)
def test_compress_mixed_block_masks_only_successful_sibling(stamp):
    """【不变量】轮内：混合块只压 eligible 的成功结果，失败/被拒兄弟保持原文。"""
    messages = [HumanMessage(content="跑测试", id="h1")]
    messages += _batch(
        "a1",
        [
            ("c1", "read_file", {"path": "a.py"}, "A" * 20000),
            ("c2", "run_command", {"command": "pytest"}, "E" * 20000),
        ],
    )
    for message in messages:
        if isinstance(message, ToolMessage) and message.tool_call_id == "c2":
            message.additional_kwargs = dict(stamp)
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

    assert "已压缩" in _by_id(merged, "tc1").content
    assert _by_id(merged, "tc2").content == "E" * 20000


@pytest.mark.parametrize(
    "stamp",
    [
        {"error_type": "tool_error"},
        {"decision": "denied"},
    ],
    ids=["failed", "denied"],
)
def test_compact_mixed_block_with_failure_after_success_is_kept_whole(stamp):
    """【不变量】轮末：保留回执排在折叠回执之后 → 整块不折（邻接约束）。

    失败/被拒调用本身永远保留；成功兄弟能否折取决于回执顺序（失败在前时可折，
    见 `test_compact_node.py::test_mixed_block_folds_success_after_failed_sibling`）。
    """
    messages = [
        HumanMessage(content="跑测试", id="h1"),
        AIMessage(
            content="先读再跑",
            tool_calls=[
                _tc("read_file", "c1", path="a.py"),
                _tc("run_command", "c2", command="pytest"),
            ],
            id="a1",
        ),
        ToolMessage(content="A" * 200, tool_call_id="c1", name="read_file", id="t1"),
        ToolMessage(content="E" * 200, tool_call_id="c2", name="run_command", id="t2"),
        AIMessage(content="有失败", id="ans1"),
        HumanMessage(content="继续", id="h2"),
        AIMessage(content="好", id="ans2"),
    ]
    for message in messages:
        if isinstance(message, ToolMessage) and message.tool_call_id == "c2":
            message.additional_kwargs = dict(stamp)

    assert _run_compact(messages) == {}


#------------------ 5. steering 提前收口：未消费块不压 ------------------


def test_unconsumed_block_is_not_masked_when_turn_ends_early():
    """【不变量】steering 让本拍提前收口时，最新工具块还没被模型消费，不能压。

    状态里的 `is_inject` 不影响 CompressNode 的判定——它只认“块后是否已有
    AIMessage”；这里把该标志一并放进 state，固化真实收口路径的输入形状。
    """
    messages = [HumanMessage(content="转向", id="h1")]
    messages += _batch("a1", [("c1", "read_file", {"path": "a.py"}, "A" * 40000)])
    # 没有后续 AIMessage：第二块是“模型还没看过”的最新结果
    messages += _batch("a2", [("c2", "read_file", {"path": "b.py"}, "B" * 40000)])

    out = asyncio.run(
        CompressNode(
            high_watermark_chars=10000,
            low_watermark_chars=5000,
            keep_blocks=0,
            mask_min_chars=100,
            max_batch_chars=8000,
            min_batch_chars=100,
        )({"messages": messages, "is_inject": True})
    )
    merged = _merged(messages, out)

    assert "已压缩" in _by_id(merged, "tc1").content
    assert _by_id(merged, "tc2").content == "B" * 40000


#------------------ 6. SQLite 重启幂等 ------------------


def _compiled_compact_graph(saver):
    graph = StateGraph(AgentState)
    graph.add_node("compact_node", CompactNode())
    graph.add_edge(START, "compact_node")
    graph.add_edge("compact_node", END)
    return graph.compile(checkpointer=saver)


async def _invoke_with_db(db_path, config, inputs):
    """一次“进程生命周期”：开库 → 跑图 → 关库。"""
    connection = await aiosqlite.connect(str(db_path))
    try:
        saver = AsyncSqliteSaver(connection)
        await saver.setup()
        app = _compiled_compact_graph(saver)
        return await app.ainvoke(inputs, config)
    finally:
        await connection.close()


def test_compact_survives_sqlite_restart_without_duplicate_summary_or_notes(tmp_path):
    """【不变量】重启后下一轮再跑 compact，不重复生成摘要、也不重复归档 notes。"""
    db_path = tmp_path / "agent.db"
    config = {"configurable": {"thread_id": "thread-restart"}}

    first_turn = [
        HumanMessage(content="查资料", id="h1"),
        AIMessage(
            content="联网调研",
            tool_calls=[_tc("web_search", "c1", query="q")],
            id="a1",
        ),
        ToolMessage(
            content="# 光伏市场\n2026 年装机…" + "W" * 500,
            tool_call_id="c1",
            name="web_search",
            id="t1",
        ),
        AIMessage(content="已拿到资料", id="ans1"),
        HumanMessage(content="继续", id="h2"),
        AIMessage(content="好", id="ans2"),
    ]
    state1 = asyncio.run(
        _invoke_with_db(
            db_path,
            config,
            {"session_id": "s1", "messages": first_turn, "notes": {}},
        )
    )
    assert [m.content for m in state1["messages"] if isinstance(m, SystemMessage)] != []
    assert set(state1["notes"]) == {"notes#r1"}

    # 进程重启：新连接复用同一个 thread，再跑一轮（新增 Human/AI）
    state2 = asyncio.run(
        _invoke_with_db(
            db_path,
            config,
            {
                "messages": [
                    HumanMessage(content="下一件事", id="h3"),
                    AIMessage(content="收到", id="ans3"),
                ]
            },
        )
    )

    summaries = [m for m in state2["messages"] if isinstance(m, SystemMessage)]
    assert len(summaries) == 1                       # 没有第二条摘要
    assert set(state2["notes"]) == {"notes#r1"}      # 没有重复归档
    assert "t1" not in {m.id for m in state2["messages"]}  # 旧回执仍处于已折叠状态
