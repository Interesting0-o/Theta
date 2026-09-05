import asyncio
import os
from pathlib import Path

from langgraph.graph import StateGraph, START, END
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from app.agent.state import AgentState
from app.agent.nodes import *
from app.agent.model import get_chat_model
from app.agent.mcp import load_mcp_tool
from app.agent.tools import  orchestrate_tool, note_tools

# 项目根：模块导入期由 __file__ 算好，避免在运行时调用阻塞式 os.getcwd()
# （langgraph dev 的 blockbuster 会拦截事件循环内的同步阻塞调用）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# 默认工作区：项目根的 tmp 子目录，作为模型的默认沙箱（不直接指向仓库本身）
DEFAULT_WORKSPACE = PROJECT_ROOT / "tmp"


def _resolve_workspace(config: RunnableConfig | None = None) -> str:
    """解析工作区路径：config.configurable.workspace_path → 环境变量 WORKSPACE_PATH → 默认 tmp。

    LangGraph 平台/CLI 会把工厂函数的唯一参数当 Config 传入（见 langgraph_api
    _classify_factory），因此工作区不从函数参数取，而按上面优先级解析；都不提供时
    退回 <项目根>/tmp（__file__ 推导、非阻塞），便于 `uv run langgraph dev` 直接可用
    （可用 env/config 覆盖）。返回前 resolve() 成绝对路径，与 mcp_service/file_io 里
    import 时对 WORKSPACE_PATH 的 resolve 保持一致，也作为给 LLM 显示的工作区根目录。
    """
    cfg = (config or {}).get("configurable") or {}
    workspace = cfg.get("workspace_path") or os.environ.get("WORKSPACE_PATH")
    if workspace:
        return str(Path(workspace).expanduser().resolve())
    return str(DEFAULT_WORKSPACE)


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


async def get_graph(config: RunnableConfig | None = None):
    """
    返回未编译的 StateGraph（compile 由调用方完成，以支持挂 checkpointer 的 interrupt 审批）。

    LangGraph dev/Platform 会以 Config 为参调用本工厂（0 参时给默认 config）；
    TUI(app.main)则直接无参调用。工作区见 _resolve_workspace。
    """
    workspace_path = _resolve_workspace(config)
    # 默认 tmp 工作区需存在：os.makedirs 属阻塞调用，放到线程执行（blockbuster 不拦）
    if workspace_path == str(DEFAULT_WORKSPACE):
        await asyncio.to_thread(DEFAULT_WORKSPACE.mkdir, parents=True, exist_ok=True)

    graph = StateGraph(AgentState)
    mcp_tools = await load_mcp_tool(workspace_path)

    # 模型能看到全部工具（含计划工具）；但只有"可执行工具"会进 review→tool_node
    all_tools =  orchestrate_tool + note_tools + mcp_tools
    model = get_chat_model().bind_tools(all_tools)

    graph.add_node("llm_node", LLMNode(model=model, workspace_path=workspace_path))
    graph.add_node("queue_node", QueueNode())
    graph.add_node("orchestrate_node", OrchestrateNode(orchestrate_tool + note_tools))#type:ignore
    graph.add_node("review_node", ReviewNode())
    graph.add_node("tool_node", ToolNode(mcp_tools))  # type: ignore
    compact_node = CompactNode()
    graph.add_node("compact_node", compact_node)

    # llm 收尾路由：还有 tool_calls → 审批；已给最终答复 → 历史超预算才进 compact_node，
    # 否则直接 END（省掉欠费轮次的节点执行与 checkpoint）。预算口径与 CompactNode 一致。
    def route_after_llm(state: AgentState):
        last_message = state["messages"][-1]
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            return "queue_node"
        return "compact_node" if needs_compact(state, compact_node.content_budget_chars) else END

    graph.add_edge(START, "llm_node")
    graph.add_conditional_edges(
        "llm_node",
        route_after_llm,
        {"queue_node": "queue_node", "compact_node": "compact_node", END: END},
    )
    graph.add_edge("compact_node", END)
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
