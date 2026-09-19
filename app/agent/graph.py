import asyncio
from pathlib import Path

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from app.agent.state import AgentState
from app.agent.nodes import (
    CompactNode,
    LLMNode,
    OrchestrateNode,
    QueueNode,
    ReviewNode,
    ToolNode,
    needs_compact,
)
from app.agent.model import get_main_chat_model
from app.agent.prompt import WORKER_SYSTEM_PROMPT
from app.agent.mcp import load_mcp_tool
from app.agent.tools import (
    SessionToolset,
    orchestrate_tool,
    note_tools,
    ask_tool,
    dispatch_tool,
    memory_tool,
    skill_tools_for,
)

# 项目根：模块导入期由 __file__ 算好，避免在运行时调用阻塞式 os.getcwd()
# （langgraph dev 的 blockbuster 会拦截事件循环内的同步阻塞调用）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# 默认工作区：项目根的 tmp 子目录，作为模型的默认沙箱（不直接指向仓库本身）
DEFAULT_WORKSPACE = PROJECT_ROOT / "tmp"


def _resolve_workspace(workspace_path: str | None = None) -> str:
    """解析工作区：显式参量 workspace_path → 默认 <项目根>/tmp。

    工作区是图的一个**构造期参量**（不读 env/config）：TUI 由 run_tui 传入启动目录、
    evaluation/runner 传入各任务临时工作区；get_main_agent_graph_langgraph（langgraph dev）
    零参 → 默认 <项目根>/tmp（不指向仓库自身）。返回前 resolve() 成绝对路径，与传给 MCP
    子进程的 WORKSPACE_PATH 保持一致，也作为给 LLM 显示的工作区根目录。
    """
    if workspace_path:
        return str(Path(workspace_path).expanduser().resolve())
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


async def get_main_agent_graph(
    workspace_path: str | None = None,
    session_id: str | None = None,
    model: BaseChatModel | None = None,
    mailbox=None,
):
    """
    返回未编译的 StateGraph（compile 由调用方完成，以支持挂 checkpointer 的 interrupt 审批）。

    工作区是显式参量（见 _resolve_workspace）：TUI 由 run_tui 传入启动目录、
    evaluation/runner 传入各任务临时工作区；langgraph dev/Platform 经
    get_main_agent_graph_langgraph 零参调用 → 默认 <项目根>/tmp。

    session_id 同样是构造期参量：它决定 MCP **运行体的作用域**（(工作区, 会话) 一个池，
    见 app/agent/mcp.py）。TUI 传真实会话 id、evaluation 传任务名；零参入口（langgraph dev）
    为 None → 所有会话共享一组运行体（已知偏差）。worker 子图**不走本入口**——它走
    get_sub_agent_graph（签名里没有 session_id）。

    model：不传 = `get_main_chat_model()`（生产路径）。给了就用它当**未绑定**的原始模型
    （bind_tools 仍由 LLMNode 按工具集版本做，见下）——评估的录制/回放从这里注入模型替身；
    与 `get_sub_agent_graph` 的同名参量一个路子。

    mailbox：运行中转向信箱（app/platform/runtime.py::UserMailbox），**基座持有内容**、
    图侧只在 LLMNode 入口 peek 布尔决定是否提前收口（docs/TODO.md「运行中转向」）。
    None = 无转向能力——worker / evaluation / langgraph dev 全部不传，行为与从前一致。
    """
    workspace_path = _resolve_workspace(workspace_path)
    # 默认 tmp 工作区需存在：os.makedirs 属阻塞调用，放到线程执行（blockbuster 不拦）
    if workspace_path == str(DEFAULT_WORKSPACE):
        await asyncio.to_thread(DEFAULT_WORKSPACE.mkdir, parents=True, exist_ok=True)

    graph = StateGraph(AgentState)
    # 核心 MCP 工具仍要在这里"加载"一次：它把 shim 表按 (工作区, 会话) 建起来，而动态工具集
    # 每轮就是从那份缓存里取的。**绑定**不在这里做了（见下）。
    await load_mcp_tool(workspace_path, session_id)

    # 技能目录在**构图期**烤进 get_skill 的 docstring（§3.2）——装技能是低频事件，
    # "扫一次、重开会话生效"够用（同 AGENT.md 的实例内 memo）。
    skill_tools = skill_tools_for()
    static_tools = (
        orchestrate_tool + note_tools + ask_tool + dispatch_tool + memory_tool + skill_tools
    )

    # 动态工具集（§13.4）：技能加载/卸载会改工具表，三个节点共用这一份接线——技能对账
    # （state 说加载了、运行体没有 → 重建）也在它里面。
    toolset = SessionToolset(workspace_path, static_tools)

    # 模型**不在这里 bind_tools**：交给 LLMNode 按工具集版本做（只在版本变化时重绑）。
    # 仍传未绑定的原始 model —— binding 上没有再 bind_tools 的能力。
    model = model if model is not None else get_main_chat_model()

    graph.add_node(
        "llm_node",
        LLMNode(model=model, workspace_path=workspace_path, toolset=toolset, mailbox=mailbox),
    )
    graph.add_node("queue_node", QueueNode())
    # dispatch_subtasks（spawn worker）与 memory 读写（定位 memory.md）都需要工作区，
    # 由 node 按签名注入（InjectedWorkspace 对模型隐藏）；技能工具也要工作区——能力型的 server
    # 在它里面运行（cwd / WORKSPACE_PATH），虽然技能**源**在项目根、不随工作区变。
    graph.add_node(
        "orchestrate_node",
        OrchestrateNode(static_tools, workspace_path=workspace_path),  # type: ignore[arg-type]
    )
    graph.add_node("review_node", ReviewNode(toolset=toolset))
    graph.add_node("tool_node", ToolNode(toolset=toolset))  # type: ignore
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


async def get_main_agent_graph_langgraph():
    """langgraph dev/Platform 注册入口：零参 → 默认工作区 <项目根>/tmp。

    langgraph.json 的 my_agent 指向本函数（而不是 get_main_agent_graph——后者带
    workspace_path 参量，框架按签名调用不可靠）。零参调用保证框架不把 config 当参量塞进来。

    代价之一是 MCP 运行体拿不到会话（session_id=None）：该路径下所有会话共享一组常驻运行体，
    且没有关闭时机（本路径无 finally）——已知偏差，见 docs/SKILL_DESIGN.md §12。
    """
    return await get_main_agent_graph()


def get_sub_agent_graph(read_tools=None, workspace_path: str | None = None, model=None, review_log: bool = False):
    """compile 一张 worker（子 agent）子任务图，供 mcp_service.sub_agent 等以 stdio/http 拉起。

    worker = 主 agent 的并发资料收集助手：图复用主图节点，但更小——`llm_node → queue_node →
    review_node → tool_node`（无编排/压缩）：
    - 路由：llm 末条 AIMessage 有 tool_calls → queue_node，无 → END；
      queue→review；review：pending→review_node、approved_tool_calls→tool_node、否则 END。
    - 审核判定单点复用 ReviewNode；`need_review` 的调用（如联网四件套）会 interrupt()，免审的
      放行进 approved_tool_calls。review_log 默认 False：worker 走 stdio 时 stdout 即 JSON-RPC，
      不能 print（需日志的调用方传 True）。
    - compile 挂 InMemorySaver：worker 每次被拉起只跑一条 run_subtask，interrupt 挂起 →
      Command(resume) 续跑都在同进程同 thread 内完成，内存检查点足够，不落盘。
    - model：默认 get_main_chat_model()；有 read_tools 才 bind_tools（便于测试注入假模型/假工具）。
    """
    chat = model if model is not None else get_main_chat_model()
    if read_tools:
        chat = chat.bind_tools(list(read_tools))

    graph = StateGraph(AgentState)
    graph.add_node(
        "llm_node",
        LLMNode(
            model=chat,
            workspace_path=workspace_path,
            system_prompt=WORKER_SYSTEM_PROMPT,
            inject_session_context=False,  # worker 不注入项目画像/长期记忆（只读资料收集，§4）
            attach_images=False,  # worker 的 HumanMessage 是主模型生成的 prompt，不认 @（防模型借 worker 附带本地文件）
        ),
    )
    graph.add_node("queue_node", QueueNode())
    # allow_ask=False：worker 不承载提问载荷（它没有 ask_user，也不该问用户）——拦的是模型
    # 编出这个名字的情形，见 ReviewNode.__call__ 的 ASK 分支。
    graph.add_node("review_node", ReviewNode(log=review_log, allow_ask=False))
    graph.add_node("tool_node", ToolNode(list(read_tools or [])))  # type: ignore[arg-type]

    def route_after_llm(state: AgentState):
        last_message = state["messages"][-1]
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            return "queue_node"
        return END

    def route_after_review(state: AgentState):
        if state.get("pending_tool_calls"):
            return "review_node"
        if state.get("approved_tool_calls"):
            return "tool_node"
        return END

    graph.add_edge(START, "llm_node")
    graph.add_conditional_edges(
        "llm_node", route_after_llm, {"queue_node": "queue_node", END: END}
    )
    graph.add_edge("queue_node", "review_node")
    graph.add_conditional_edges(
        "review_node",
        route_after_review,
        {"review_node": "review_node", "tool_node": "tool_node", END: END},
    )
    graph.add_edge("tool_node", "llm_node")

    return graph.compile(checkpointer=InMemorySaver())
