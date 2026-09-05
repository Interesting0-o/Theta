"""review 分流与图路由单测（不拉起真实 MCP、不触发真实 interrupt）。

分流类用例用 tool.json 里 need_review:false 的工具（create_plan / read_file），
所以 ReviewNode 不会 interrupt，可安全地 asyncio.run 驱动；
审批闸门类用例 monkeypatch interrupt，验证需审批工具（如 web_search）的挂起与决策。
"""
import asyncio

from langchain_core.messages import ToolMessage

from app.agent.nodes import ReviewNode
from app.agent.graph import should_review_or_execute, should_continue_after_orchestrate


def _call(name, call_id="call_x"):
    return {"name": name, "args": {}, "id": call_id}


def _state(pending):
    return {
        "pending_tool_calls": pending,
        "approved_tool_calls": [],
        "approved_orchestrate_calls": [],
    }


def _run(node, state):
    return asyncio.run(node(state))


def test_orchestration_call_goes_to_orchestrate_queue():
    """source=='plan' 的编排调用应落入 approved_orchestrate_calls。"""
    out = _run(ReviewNode(), _state([_call("create_plan")]))
    assert out["pending_tool_calls"] == []
    assert [c["name"] for c in out["approved_orchestrate_calls"]] == ["create_plan"]
    assert out["approved_tool_calls"] == []


def test_ordinary_call_goes_to_tool_queue():
    """免审的普通调用应落入 approved_tool_calls。"""
    out = _run(ReviewNode(), _state([_call("read_file")]))
    assert out["pending_tool_calls"] == []
    assert out["approved_orchestrate_calls"] == []
    assert [c["name"] for c in out["approved_tool_calls"]] == ["read_file"]


def test_mixed_turn_splits_into_separate_queues():
    """编排+普通并存时，review 逐条 drain 后分别落入两个队列。"""
    node = ReviewNode()
    out1 = _run(node, _state([_call("create_plan", "c1"), _call("read_file", "c2")]))
    # 第一次只消费 create_plan，read_file 仍在 pending
    assert [c["id"] for c in out1["pending_tool_calls"]] == ["c2"]
    assert [c["name"] for c in out1["approved_orchestrate_calls"]] == ["create_plan"]
    assert out1["approved_tool_calls"] == []

    out2 = _run(node, out1)
    assert out2["pending_tool_calls"] == []
    assert [c["name"] for c in out2["approved_orchestrate_calls"]] == ["create_plan"]
    assert [c["name"] for c in out2["approved_tool_calls"]] == ["read_file"]


def test_need_review_tool_interrupts_and_approval_lets_it_pass(monkeypatch):
    """web_search 等需审批工具必须挂起；批准后才进入放行队列。"""
    interrupts = []
    monkeypatch.setattr(
        "app.agent.nodes.interrupt",
        lambda payload: interrupts.append(payload) or {"approved": True},
    )
    out = _run(ReviewNode(), _state([_call("web_search")]))
    assert [p["tool_name"] for p in interrupts] == ["web_search"]
    assert out["pending_tool_calls"] == []
    assert [c["name"] for c in out["approved_tool_calls"]] == ["web_search"]


def test_need_review_tool_rejection_injects_denial_message(monkeypatch):
    """被拒的需审批调用不放行，但须回灌一条 ToolMessage 告知模型（兑现悬空 tool_call）。"""
    monkeypatch.setattr(
        "app.agent.nodes.interrupt",
        lambda payload: {"approved": False},
    )
    out = _run(ReviewNode(), _state([_call("web_search", call_id="call_w1")]))
    assert out["pending_tool_calls"] == []
    assert out["approved_tool_calls"] == []
    assert out["approved_orchestrate_calls"] == []
    # 拒绝必须回模型一条显式信号：以原 tool_call_id 兑现，内容标明被拒
    msgs = out.get("messages", [])
    assert len(msgs) == 1
    msg = msgs[0]
    assert isinstance(msg, ToolMessage)
    assert msg.tool_call_id == "call_w1"
    assert msg.name == "web_search"
    assert "approval_denied" in msg.content


def test_need_review_tool_approval_injects_no_message(monkeypatch):
    """批准路径不应注入多余 ToolMessage（只有拒绝才需要反馈）。"""
    monkeypatch.setattr(
        "app.agent.nodes.interrupt",
        lambda payload: {"approved": True},
    )
    out = _run(ReviewNode(), _state([_call("web_search")]))
    assert out.get("messages") is None


def test_denied_call_only_gets_denial_message_in_mixed_turn(monkeypatch):
    """同一 AIMessage 的多个调用：被拒的那个收到拒绝消息，免审放行的那个不注入。"""
    monkeypatch.setattr(
        "app.agent.nodes.interrupt",
        lambda payload: {"approved": False},  # web_search 被拒；read_file 免审不会走到这里
    )
    node = ReviewNode()
    out1 = _run(node, _state([_call("web_search", call_id="c1"), _call("read_file", call_id="c2")]))
    # 第一次：c1 被拒 → 注入 c1 的拒绝消息，c2 留在 pending
    assert [m.tool_call_id for m in out1.get("messages", [])] == ["c1"]
    assert [c["id"] for c in out1["pending_tool_calls"]] == ["c2"]

    out2 = _run(node, out1)
    # 第二次：c2 免审放行入 approved_tool_calls，不再注入消息
    assert out2.get("messages") is None
    assert [c["id"] for c in out2["approved_tool_calls"]] == ["c2"]


def test_should_review_or_execute_prefers_orchestrate():
    state = {
        "pending_tool_calls": [],
        "approved_orchestrate_calls": [_call("create_plan")],
        "approved_tool_calls": [_call("read_file")],
    }
    assert should_review_or_execute(state) == "orchestrate_node"


def test_should_review_or_execute_drains_pending_first():
    state = {
        "pending_tool_calls": [_call("create_plan")],
        "approved_orchestrate_calls": [],
        "approved_tool_calls": [_call("read_file")],
    }
    assert should_review_or_execute(state) == "review_node"


def test_after_orchestrate_falls_back_to_tool_or_llm():
    with_tool = {
        "pending_tool_calls": [],
        "approved_orchestrate_calls": [_call("create_plan")],
        "approved_tool_calls": [_call("read_file")],
    }
    assert should_continue_after_orchestrate(with_tool) == "tool_node"

    only_orchestrate = {
        "pending_tool_calls": [],
        "approved_orchestrate_calls": [_call("create_plan")],
        "approved_tool_calls": [],
    }
    assert should_continue_after_orchestrate(only_orchestrate) == "llm_node"
