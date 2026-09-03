"""PlanNode 行为单测：纯计划回合 / 混合回合 / 无计划 no-op。

注意：import app.agent.nodes 会触发 app/agent/__init__ → graph → model，
要求 .env 存在（CLAUDE.md 前提）。本文件不拉起真实 MCP 子进程。
"""
import asyncio

from langchain_core.messages import ToolMessage

from app.agent.nodes import PlanNode
from app.agent.tools import plan_tools


def _state(**overrides):
    base = {"pending_tool_calls": [], "current_plan": [], "messages": []}
    base.update(overrides)
    return base


def _call(name, args, call_id="call_x"):
    return {"name": name, "args": args, "id": call_id}


def test_pure_plan_turn_clears_pending_and_writes_plan():
    node = PlanNode(plan_tools)
    state = _state(
        pending_tool_calls=[
            _call("create_plan", {"steps": ["读文件", "修改", "测试"]})
        ]
    )

    out = asyncio.run(node(state))

    assert out["pending_tool_calls"] == []
    assert len(out["current_plan"]) == 3
    assert out["current_plan"][0]["status"] == "pending"
    assert len(out["messages"]) == 1
    msg = out["messages"][0]
    assert isinstance(msg, ToolMessage)
    assert msg.name == "create_plan"
    assert "已生成计划" in msg.content
    assert "读文件" in msg.content


def test_mixed_turn_consumes_plan_leaves_executable():
    node = PlanNode(plan_tools)
    exec_call = _call("write_file", {"path": "/ws/demo.txt"}, call_id="call_exec")
    state = _state(
        pending_tool_calls=[
            _call("create_plan", {"steps": ["写文件"]}),
            exec_call,
        ]
    )

    out = asyncio.run(node(state))

    assert out["pending_tool_calls"] == [exec_call]
    assert len(out["current_plan"]) == 1
    assert len(out["messages"]) == 1


def test_no_plan_call_returns_noop():
    node = PlanNode(plan_tools)
    state = _state(
        pending_tool_calls=[_call("write_file", {"path": "/ws/x"}, call_id="c1")]
    )

    out = asyncio.run(node(state))
    assert out == {}


def test_update_done_and_clear_sequential():
    node = PlanNode(plan_tools)
    state = _state(
        pending_tool_calls=[
            _call("create_plan", {"steps": ["a", "b"]}),
            _call("update_plan_step", {"step_id": "1", "status": "done"}),
            _call("clear_plan"),
        ]
    )

    out = asyncio.run(node(state))

    assert out["current_plan"] == []
    texts = "\n".join(m.content for m in out["messages"])
    assert "已生成计划（2 步）" in texts
    assert "步骤 1 已标记为 done" in texts
    assert "已清空全部计划" in texts


def test_update_unknown_step_failure_keeps_plan():
    node = PlanNode(plan_tools)
    state = _state(
        pending_tool_calls=[
            _call("create_plan", {"steps": ["a"]}),
            _call("update_plan_step", {"step_id": "99", "status": "done"}),
        ]
    )

    out = asyncio.run(node(state))

    assert len(out["current_plan"]) == 1  # 失败不改变计划
    assert out["current_plan"][0]["status"] == "pending"
    assert "不存在" in out["messages"][1].content
