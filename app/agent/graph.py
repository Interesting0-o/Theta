from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph, START, END
from langchain_core.messages import AIMessage, BaseMessage
from app.agent.state import AgentState
from app.agent.nodes import *
from app.agent.model import get_chat_model
from app.agent.mcp import load_mcp_tool
from app.agent.tools import core_tools


def should_continue(state: AgentState):
    last_message: BaseMessage = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "queue_node"
    return END


def should_review_or_execute(state: AgentState):
    if state.get("pending_tool_calls"):
        return "review_node"
    if state.get("approved_tool_calls"):
        return "tool_node"
    return END


async def get_graph(checkpointer=None):
    """
    返回编译后的图(含 checkpointer, 以支持 interrupt 权限请求)。
    """
    graph = StateGraph(AgentState)
    mcp_tools = await load_mcp_tool()

    all_tools = core_tools + mcp_tools
    model = get_chat_model().bind_tools(all_tools)

    graph.add_node("llm_node", LLMNode(model=model))
    graph.add_node("queue_node", queue_node)
    graph.add_node("review_node", ReviewNode())
    graph.add_node("tool_node", ToolNode(all_tools))  # type: ignore

    graph.add_edge(START, "llm_node")
    graph.add_conditional_edges(
        "llm_node",
        should_continue,
        {"queue_node": "queue_node", END: END},
    )
    graph.add_edge("queue_node", "review_node")
    graph.add_conditional_edges(
        "review_node",
        should_review_or_execute,
        {"review_node": "review_node", "tool_node": "tool_node", END: END},
    )
    graph.add_edge("tool_node", "llm_node")

    return graph.compile(checkpointer=checkpointer or InMemorySaver())
