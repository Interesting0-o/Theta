"""子任务 worker MCP server（多 agent 候选 B 的 isolated 首步，见 docs/MULTI_AGENT.md）。

暴露单个工具 `run_subtask(task)`：调用方（主 agent 的 dispatch_subtasks 编排工具，见
app/agent/tools.py）把一条**只读子任务**派过来，本 server 在自己进程里、以**独立消息栈**
跑一个迷你 agent 循环，返回结论正文。

本模块是 **worker 进程侧的家**（对端是主侧的 app/agent/tools.py::dispatch_subtasks）：
- worker 侧的 agent 逻辑也收在这里（原 app/agent/subtask.py 已并入）：WORKER_SYSTEM_PROMPT、
  `read_only_tools`（只读检索过滤）、`authorize_node`、`build_subtask_graph`。主 agent
  **不用**它们，只经由 spawn `python -m mcp_service.sub_agent` + `run_subtask` 跨进程调用。
- **只读、无写/命令工具 → 无 interrupt / 无审批回传**：run_subtask 是普通 request/response
  工具。写工具 + 审批回传是后续里程碑（docs §6）。
- **app.agent 相关 import 全部懒加载**（config.py 的 .env 相对 cwd 解析；app/agent/model
  顶层 get_settings）→ 模块 import 不触发 get_settings，冷启动无需 .env；真正跑子任务、
  要建 worker 图时才 import（cwd=项目根可读到 .env 的 CHAT_*）。
- 工作区取自 env `WORKSPACE_PATH`（与 file_io 约定一致；缺失/非目录 → ConfigError，
  由 @guard 归成 config_error）。
"""
import json
import os
import uuid
from pathlib import Path
from typing import Sequence

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from mcp.server.fastmcp import FastMCP

from app.exception import ConfigError, InvalidArgumentError
from app.schema.agent_schema import ToolResult
from mcp_service.utils import guard

mcp = FastMCP("SubAgent")

# worker 子任务图的递归上限（节点执行 superstep 数）；只读循环靠它兜底，防模型无限调工具
_MAX_STEPS = 100

# 项目根：spawn 本 worker 时 cwd 用项目根，使 .env 可被读到（CHAT_*）；tool.json 在此取
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TOOL_CONFIG_PATH = _PROJECT_ROOT / "app" / "agent" / "tool.json"

# 只读检索工具允许的 source：file_io 读 + git 读
_READ_SOURCES: frozenset[str] = frozenset({"mcp_service/file_io", "mcp_service/git"})


WORKER_SYSTEM_PROMPT = """\
你是 CodingAgent 的**子任务执行 agent（worker）**。你会被单独派发一条边界清晰的子任务，
用**只读**工具在当前工作区里调查后，把结论作为最终答复交回。

- 你能做的：read_file / list_dir / get_directory_tree / glob / search_content
  （工作区文件检索）与 list_repos / git_status / git_branches / git_diff / git_log /
  git_fetch（git 只读）。信息不足就先补读，不要脑补不存在的文件内容。
- 你不能做的：**不能改动任何文件、不能执行命令**——写/执行类工具对你不可用，不要尝试
  也不要在结论里假装已改动；需要落地改动时，把"要改哪里、怎么改"写清楚，由主 agent 执行。
- 所有文件类路径以注入的"当前工作区根目录"为准，相对路径都以它为基准；越界会被拦下。
- 收尾（不再需要读信息时）直接把**结论**作为最终答复返回：简明、给出关键出处
  （文件:行 或工具回执），若任务要求产出方案则给出可执行要点。默认用中文作答。\
"""


def _resolve_workspace() -> str:
    """取并校验子任务工作区（env WORKSPACE_PATH）。缺失/非目录 → ConfigError。"""
    raw = os.environ.get("WORKSPACE_PATH", "")
    if not raw or not raw.strip():
        raise ConfigError("WORKSPACE_PATH 未设置（无法定位子任务工作区）")
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise ConfigError(f"子任务工作区 {path} 不存在")
    return str(path)


def read_only_tools(tools: Sequence[BaseTool], cfg: dict | None = None) -> list[BaseTool]:
    """从工具表里筛出"只读检索"子集（按 tool.json 的 need_review / source）。

    - cfg：tool.json 解析结果 {tool_name: {need_review, source}}；默认读 app/agent/tool.json。
    - 保留：name 命中 cfg 且 `need_review:false`、source ∈ {file_io, git} 的工具；
      剔除写/删/命令/联网/web/编排（plan/notes/dispatch）等会改状态或需审批者。
    - 纯函数：不依赖 MCP 加载，便于单测注入假 cfg 验证过滤语义。
    """
    if cfg is None:
        with _TOOL_CONFIG_PATH.open("r", encoding="utf-8") as file:
            cfg = json.load(file)
    allowed = {
        name
        for name, conf in cfg.items()
        if not conf.get("need_review") and conf.get("source") in _READ_SOURCES
    }
    return [t for t in tools if getattr(t, "name", None) in allowed]


async def authorize_node(state: dict) -> dict:
    """免审放行本轮 tool_calls → approved_tool_calls。

    worker 只绑定只读检索工具（read_only_tools 保证 need_review:false），无需像主图那样
    经 ReviewNode 逐条 interrupt/drain——本轮模型发起的调用整批放行给 ToolNode 即可。
    （主图里 queue_node 只填 pending、靠 review_node 搬进 approved；worker 跳过 review，
    故不能直接复用 QueueNode，否则 approved 永远为空、工具永不执行。）
    """
    tool_calls = getattr(state["messages"][-1], "tool_calls", None) or []
    return {
        "pending_tool_calls": [],
        "approved_tool_calls": list(tool_calls),
        "approved_orchestrate_calls": [],
    }


def build_subtask_graph(read_tools, workspace_path: str, model=None):
    """compile 一张只读子任务图（无 checkpointer）。

    - model：默认 get_chat_model()；有 read_tools 才 bind_tools（便于测试注入假模型）。
    - 路由：llm 末条 AIMessage 有 tool_calls → authorize_node（免审放行）→ tool → llm；
      无 tool_calls → END。比主图少了 review/interrupt/编排/压缩——worker 全只读、单次调用。
    - compile 不挂 checkpointer：worker 子任务单次调用即可跑完，无需跨轮持久化。
    - app.agent 依赖懒加载：进本函数才 import（避免模块 import 期触发 get_settings）。
    """
    from app.agent.model import get_chat_model  # noqa: PLC0415
    from app.agent.nodes import LLMNode, ToolNode  # noqa: PLC0415
    from app.agent.state import AgentState  # noqa: PLC0415

    chat = model if model is not None else get_chat_model()
    if read_tools:
        chat = chat.bind_tools(list(read_tools))

    graph = StateGraph(AgentState)
    graph.add_node(
        "llm_node",
        LLMNode(model=chat, workspace_path=workspace_path, system_prompt=WORKER_SYSTEM_PROMPT),
    )
    graph.add_node("authorize_node", authorize_node)
    graph.add_node("tool_node", ToolNode(list(read_tools)))  # type: ignore[arg-type]

    def route_after_llm(state: AgentState):
        last_message = state["messages"][-1]
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            return "authorize_node"
        return END

    graph.add_edge(START, "llm_node")
    graph.add_conditional_edges(
        "llm_node", route_after_llm, {"authorize_node": "authorize_node", END: END}
    )
    graph.add_edge("authorize_node", "tool_node")
    graph.add_edge("tool_node", "llm_node")

    return graph.compile()


def _extract_conclusion(result: dict) -> str:
    """从图终态里取结论正文：最后一条非空 content 的 AIMessage；无则兜底提示。"""
    messages = result.get("messages") or []
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            text = getattr(msg, "content", None)
            if isinstance(text, str) and text.strip():
                return text.strip()
    for msg in reversed(messages):
        text = getattr(msg, "content", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return "（子任务未产出正文）"


async def run_subtask_impl(
    task: str,
    workspace: str,
    *,
    model=None,
    tools=None,
) -> ToolResult:
    """run_subtask 的核心实现；参数可注入便于测试（假 model / 假 tools，不拉真实子进程）。

    - task：子任务描述；空白 → InvalidArgumentError。
    - workspace：工作区绝对路径（须存在）。
    - tools：默认经 load_mcp_tool(workspace) 拉取后按 read_only_tools 筛成只读检索子集；
      注入时直接用（测试绕开真实 MCP 孙进程）。
    - model：默认 build_subtask_graph 内建 get_chat_model()；注入假模型则无需真实 LLM。
    """
    from app.agent.mcp import load_mcp_tool  # 懒加载：见模块 docstring

    if not isinstance(task, str) or not task.strip():
        raise InvalidArgumentError("task 不能为空")

    workspace = str(Path(workspace).expanduser().resolve())

    if tools is None:
        tools = read_only_tools(await load_mcp_tool(workspace))

    graph = build_subtask_graph(tools, workspace, model=model)

    state: dict = {
        "session_id": uuid.uuid4().hex,
        "messages": [HumanMessage(content=task)],
        "pending_tool_calls": [],
        "approved_tool_calls": [],
        "approved_orchestrate_calls": [],
        "current_plan": [],
        "notes": {},
    }
    result = await graph.ainvoke(state, config={"recursion_limit": _MAX_STEPS})
    return ToolResult(success=True, content=_extract_conclusion(result))


@mcp.tool()
@guard
async def run_subtask(task: str) -> ToolResult:
    """把一条**只读**子任务（调研 / 分析 / 读码 / 出方案）派发给独立子 agent 执行。

    子 agent 在本 server 进程里用只读工具（工作区文件检索 + git 只读）调查当前工作区，
    跑完返回一段结论正文。它**不能改动任何文件、不能执行命令**——需要落地改动时，
    由你在收到结论后自行执行。

    Args:
        task: 一条边界清晰的子任务描述（含目标与期望结论要点），如
            "调研 src/x.py 的实现，说明它如何做校验并给出 ≤5 行的中文总结"。

    Returns:
        结论正文（中文）；失败时以 [error_type] 前缀开头说明原因。
    """
    return await run_subtask_impl(task, _resolve_workspace())


if __name__ == "__main__":
    mcp.run(transport="stdio")
