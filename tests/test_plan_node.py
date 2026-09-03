"""OrchestrateNode 行为单测：纯计划回合 / 混合回合 / 无计划 no-op / 链式与失败路径。

直接驱动 OrchestrateNode（不拉起真实 MCP、不触发 interrupt）：
编排调用已由 ReviewNode 分流进 `approved_orchestrate_calls`，这里验证节点如何
逐个注入 state 执行编排工具、链式推进 current_plan，并把工具回执原样交回模型。

注意：import app.agent.nodes 会触发 app/agent/__init__ → graph → model，
要求 .env 存在（CLAUDE.md 前提）。本文件不拉起真实 MCP 子进程。
"""
import asyncio

from langchain_core.messages import ToolMessage

from app.agent.nodes import OrchestrateNode
from app.agent.tools import orchestrate_tool


def _state(**overrides):
    base = {
        "pending_tool_calls": [],
        "approved_tool_calls": [],
        "approved_orchestrate_calls": [],
        "current_plan": [],
        "messages": [],
    }
    base.update(overrides)
    return base


def _call(name, args, call_id="call_x"):
    return {"name": name, "args": args, "id": call_id}


def _msgs(out):
    return [m for m in out["messages"] if isinstance(m, ToolMessage)]


def test_pure_plan_turn_writes_plan_and_clears_queue():
    node = OrchestrateNode(orchestrate_tool)
    state = _state(
        approved_orchestrate_calls=[
            _call("create_plan", {"steps": ["读文件", "修改", "测试"]}, call_id="c1")
        ]
    )

    out = asyncio.run(node(state))

    assert out["approved_orchestrate_calls"] == []
    assert len(out["current_plan"]) == 3
    assert out["current_plan"][0]["status"] == "pending"

    msgs = _msgs(out)
    assert len(msgs) == 1
    assert msgs[0].name == "create_plan"
    assert msgs[0].tool_call_id == "c1"  # InjectedToolCallId 已注入
    assert "已生成计划" in msgs[0].content
    assert "读文件" in msgs[0].content


def test_mixed_turn_leaves_executable_queue_for_tool_node():
    node = OrchestrateNode(orchestrate_tool)
    state = _state(
        approved_orchestrate_calls=[
            _call("create_plan", {"steps": ["写文件"]}, call_id="c1")
        ],
        # 普通调用已在 review 分流进另一个队列，编排节点不该动它
        approved_tool_calls=[_call("write_file", {"path": "/ws/demo.txt"}, call_id="call_exec")],
    )

    out = asyncio.run(node(state))

    assert out["approved_orchestrate_calls"] == []
    assert [c["id"] for c in out["approved_tool_calls"]] == ["call_exec"]
    assert len(out["current_plan"]) == 1
    assert len(_msgs(out)) == 1


def test_no_orchestrate_call_returns_noop():
    node = OrchestrateNode(orchestrate_tool)
    out = asyncio.run(node(_state()))
    assert out == {}


def test_sequential_calls_chain_current_plan():
    """同回合 create → update(done) → clear 顺序生效，消息全部回显。"""
    node = OrchestrateNode(orchestrate_tool)
    state = _state(
        approved_orchestrate_calls=[
            _call("create_plan", {"steps": ["a", "b"]}, call_id="c1"),
            _call("update_plan_step", {"step_id": "1", "status": "done"}, call_id="c2"),
            _call("clear_plan", {}, call_id="c3"),
        ]
    )

    out = asyncio.run(node(state))

    assert out["current_plan"] == []
    texts = "\n".join(m.content for m in _msgs(out))
    assert "已生成计划（2 步）" in texts
    assert "步骤 1 已标记为 done" in texts
    assert "已清空全部计划" in texts


def test_update_unknown_step_failure_keeps_plan():
    node = OrchestrateNode(orchestrate_tool)
    state = _state(
        approved_orchestrate_calls=[
            _call("create_plan", {"steps": ["a"]}, call_id="c1"),
            _call("update_plan_step", {"step_id": "99", "status": "done"}, call_id="c2"),
        ]
    )

    out = asyncio.run(node(state))

    assert len(out["current_plan"]) == 1  # 失败不改变计划
    assert out["current_plan"][0]["status"] == "pending"
    msgs = _msgs(out)
    assert len(msgs) == 2
    assert "步骤 99 不存在" in msgs[1].content


def test_unregistered_tool_errors_without_mutating_plan():
    node = OrchestrateNode(orchestrate_tool)
    state = _state(
        approved_orchestrate_calls=[_call("ghost_tool", {}, call_id="c1")]
    )

    out = asyncio.run(node(state))

    assert out["current_plan"] == []
    msgs = _msgs(out)
    assert len(msgs) == 1
    assert "工具不存在或未注册" in msgs[0].content
