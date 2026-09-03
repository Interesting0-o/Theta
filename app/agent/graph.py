from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph, START, END
from langchain_core.messages import AIMessage, BaseMessage
from app.agent.state import AgentState
from app.agent.nodes import *
from app.agent.model import get_chat_model
from app.agent.mcp import load_mcp_tool
from app.agent.tools import core_tools, orchestrate_tool


def should_continue(state: AgentState):
    last_message: BaseMessage = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "queue_node"
    return END


def should_review_or_execute(state: AgentState):
    # review_node 逐条 drain pending；排空后按"编排优先、普通回落"路由到执行器
    if state.get("pending_tool_calls"):
        return "review_node"
    if state.get("approved_orchestrate_calls"):
        return "orchestrate_node"
    if state.get("approved_tool_calls"):
        return "tool_node"
    return END


def should_continue_after_orchestrate(state: AgentState):
    # 同一回合编排+普通调用并存时：编排先跑，这里把剩余普通调用交给 tool_node
    return "tool_node" if state.get("approved_tool_calls") else "llm_node"


async def get_graph(workspace_path:str):
    """
    返回编译后的图(含 checkpointer, 以支持 interrupt 权限请求)。
    """
    graph = StateGraph(AgentState)
    mcp_tools = await load_mcp_tool(workspace_path)

    # 模型能看到全部工具（含计划工具）；但只有"可执行工具"会进 review→tool_node
    all_tools = core_tools + orchestrate_tool + mcp_tools
    executable_tools = core_tools + mcp_tools
    model = get_chat_model().bind_tools(all_tools)

    graph.add_node("llm_node", LLMNode(model=model))
    graph.add_node("queue_node", queue_node)
    graph.add_node("orchestrate_node", OrchestrateNode(orchestrate_tool))
    graph.add_node("review_node", ReviewNode())
    graph.add_node("tool_node", ToolNode(executable_tools))  # type: ignore

    graph.add_edge(START, "llm_node")
    graph.add_conditional_edges(
        "llm_node",
        should_continue,
        {"queue_node": "queue_node", END: END},
    )
    # queue → review：编排与普通调用统一走审批，由 review 按 source 分流
    graph.add_edge("queue_node", "review_node")
    graph.add_conditional_edges(
        "review_node",
        should_review_or_execute,
        {
            "review_node": "review_node",
            "orchestrate_node": "orchestrate_node",
            "tool_node": "tool_node",
            END: END,
        },
    )
    # orchestrate 先跑；有剩余普通调用则回落 tool_node，否则回模型
    graph.add_conditional_edges(
        "orchestrate_node",
        should_continue_after_orchestrate,
        {"tool_node": "tool_node", "llm_node": "llm_node"},
    )
    graph.add_edge("tool_node", "llm_node")

    return graph
