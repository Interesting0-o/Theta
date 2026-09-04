"""current_plan 跨用户轮持久化的回归测试（针对 main.py 曾每轮注入 [] 覆盖计划的 bug）。

被固化的契约（见 app/main.py 每轮输入注释 / app/agent/state.py）：
- current_plan 只由编排工具（create_plan 等）写回、存于 checkpoint 中跨轮存活；
- TUI 每轮用户输入**不携带** current_plan 键 —— 否则注入 [] 会把上一轮建的计划清空。

测试用真实 AgentState schema + InMemorySaver 搭一个最小图，模拟"用户轮"之间
LangGraph 对部分输入的合并语义，不拉起 MCP、不触发真实节点，故无需 WORKSPACE_PATH。

注意：import app.agent.state 会经 app/agent/__init__ 拉到 graph → model，
因此仍需 .env 存在（与 tests/test_plan_node.py 等同一前提）。
"""
import asyncio

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.state import AgentState

TWO_STEP_PLAN = [
    {"id": "1", "task": "读代码", "status": "pending"},
    {"id": "2", "task": "改代码", "status": "pending"},
]


def _turn_node(state):
    """读 current_plan 并回显步数；仅当收到 CREATE_PLAN 命令且还没有计划时建一个。"""
    last = state["messages"][-1]
    plan = list(state.get("current_plan") or [])
    updates = {
        "messages": [
            ToolMessage(
                name="probe",
                content=f"plan_steps={len(plan)}",
                tool_call_id="probe",
            )
        ]
    }
    if getattr(last, "content", "") == "CREATE_PLAN" and not plan:
        updates["current_plan"] = TWO_STEP_PLAN
    return updates


def _compile():
    graph = StateGraph(AgentState)
    graph.add_node("turn", _turn_node)
    graph.add_edge(START, "turn")
    graph.add_edge("turn", END)
    return graph.compile(checkpointer=InMemorySaver())


def _invoke(app, config, user_text, extra=None):
    inputs = {"session_id": "demo", "messages": [HumanMessage(content=user_text)]}
    if extra:
        inputs.update(extra)
    return asyncio.run(app.ainvoke(inputs, config))


def _last_probe(output) -> str:
    return output["messages"][-1].content


def test_fresh_thread_first_turn_without_current_plan_is_empty_then_creatable():
    """新线程首轮不携带 current_plan：读为空，且编排工具仍能正常建计划。"""
    app = _compile()
    cfg = {"configurable": {"thread_id": "t-fresh"}}
    out1 = _invoke(app, cfg, "CREATE_PLAN")
    assert _last_probe(out1) == "plan_steps=0"  # 建计划前本轮为空


def test_plan_persists_when_later_turn_omits_current_plan():
    """修复目标：上一轮建的计划，在下一轮用户输入省略 current_plan 时依然存活。"""
    app = _compile()
    cfg = {"configurable": {"thread_id": "t-persist"}}
    _invoke(app, cfg, "CREATE_PLAN")

    out2 = _invoke(app, cfg, "继续推进")  # 与 main.py 一致：只带 messages + 瞬态队列
    assert _last_probe(out2) == "plan_steps=2"


def test_injecting_empty_plan_on_later_turn_clears_it():
    """回归文档：旧 main.py 每轮注入 current_plan=[] 的语义正是"清空计划"——
    因此它必须被移除，计划才可能跨用户轮延续。"""
    app = _compile()
    cfg = {"configurable": {"thread_id": "t-clear"}}
    _invoke(app, cfg, "CREATE_PLAN")

    out2 = _invoke(app, cfg, "继续", extra={"current_plan": []})
    assert _last_probe(out2) == "plan_steps=0"
