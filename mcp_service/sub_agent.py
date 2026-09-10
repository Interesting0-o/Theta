"""子任务 worker MCP server（stdio；worker 图在 app/agent.graph::get_sub_agent_graph）。

暴露单个工具 `run_subtask(task)`：调用方（主 agent 的 dispatch_subtasks 编排工具，见
app/agent/tools.py）把一条**子任务**（并发资料收集）派过来，本 server 在自己进程里、以独立
消息栈跑一个 worker 子 agent，返回结论正文。

本模块是 **worker 进程的 server / run 壳**——业务（子图、人设、工具子集）归 app.agent：
- 子图 = `app/agent/graph.py::get_sub_agent_graph`（复用 Queue/Review/Tool/LLM 节点 +
  InMemorySaver compile，need_review 调用 interrupt，免审放行进 approved_tool_calls）；
- 人设 = `app/agent/prompt.py::WORKER_SYSTEM_PROMPT`；
- 可用工具子集 = `app/agent/tools.py::worker_tools`（工作区只读检索 ∪ 联网四件套）。
本模块只在 run_subtask 真正执行时才懒加载它们 → 模块 import 不碰 app.agent、无需 .env。
- 本模块自留：FastMCP(stdio) 入口 + run 编排（run_subtask_impl：建 state / 跑图 / 提结论）+
  审批回传 driver（interrupt → POST 主侧 ApprovalInboxServer → 长轮询 → Command(resume)）。
- worker = 主 agent 的并发资料收集助手：只读工作区 + 可联网；联网触发主 agent 人工审批；
  **无工作区写/命令**（其下放是后续里程碑，docs §6）。
- 工作区取自 env WORKSPACE_PATH（缺失/非目录 → ConfigError，由 @guard 归成 config_error）。
"""
import os
import time
import uuid
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command
from mcp.server.fastmcp import FastMCP

from app.exception import ConfigError, InvalidArgumentError
from app.schema.agent_schema import ToolResult
from app.schema.approval_schema import DEFAULT_INBOX_HOST, DEFAULT_INBOX_PORT
from mcp_service.utils import guard

mcp = FastMCP("SubAgent")

# worker 子任务图的递归上限（superstep 数）；只读/审批循环靠它兜底
_MAX_STEPS = 100

# 主侧统一审批收件箱（ApprovalInboxServer）默认地址：与主侧**共用同一份常量**
# （app/schema/approval_schema.py），两边各写一个字面量迟早会漂。env AGENT_INBOX_URL 可覆盖，
# 且主侧启动收件箱后会把**实际**地址写进该 env、由 spawn 时转发进来（子进程 env 是替换制、不继承）。
_INBOX_DEFAULT_URL = f"http://{DEFAULT_INBOX_HOST}:{DEFAULT_INBOX_PORT}"
# 单条审批决定等待的总上限（秒）；server 单次 block 最多等 120s，driver 外层循环重试到该上限
_APPROVAL_TIMEOUT_S = 300.0


def _resolve_workspace() -> str:
    """取并校验子任务工作区（env WORKSPACE_PATH）。缺失/非目录 → ConfigError。"""
    raw = os.environ.get("WORKSPACE_PATH", "")
    if not raw or not raw.strip():
        raise ConfigError("WORKSPACE_PATH 未设置（无法定位子任务工作区）")
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise ConfigError(f"子任务工作区 {path} 不存在")
    return str(path)


def _worker_id() -> str:
    """生成一条 run_subtask 的 worker 标记（展示用，主侧渲染成 worker:<id>）。"""
    return f"worker-{uuid.uuid4().hex[:8]}"


def _inbox_base_url() -> str:
    """主侧审批收件箱（ApprovalInboxServer）基地址；每次现取以便运行时覆盖。

    空值按"未设置"处理（与本仓 `.env` 的惯例一致）——否则空串会被当成一个地址用。
    """
    return os.environ.get("AGENT_INBOX_URL") or _INBOX_DEFAULT_URL


# ---------------------------------------------------------------------------
# 审批回传：interrupt 值 → HTTP POST 主侧 → 长轮询等决定
# ---------------------------------------------------------------------------


def _value_to_approval(value) -> tuple[str, dict, str]:
    """一条 graph interrupt value（ReviewNode payload）→ (tool_name, tool_args, description)。

    ReviewNode payload 键：type/tool_name/tool_args/current_step/tool_call_id。description 取
    tool_args.description（终端类工具的人话解释），缺省回退成"worker 需审批的调用: <tool>"。
    """
    if not isinstance(value, dict):
        return ("?", {}, f"worker 需审批的调用：{value!r}")
    tool_name = value.get("tool_name") or "?"
    tool_args = value.get("tool_args") or {}
    description = tool_args.get("description") if isinstance(tool_args, dict) else None
    if not description:
        description = f"worker 需审批的调用: {tool_name}"
    return tool_name, dict(tool_args) if isinstance(tool_args, dict) else {}, str(description)


async def _await_main_decision(
    description: str,
    tool_name: str,
    tool_args: dict,
    *,
    worker_id: str,
    base_url: str | None = None,
    timeout: float = _APPROVAL_TIMEOUT_S,
) -> bool:
    """把一条 worker 需审批的调用发进主侧统一 broker 并长轮询等人工决定，返回是否批准。

    协议与主侧 ApprovalInboxServer（app/tui/approval_inbox.py）对齐：POST /requests 入队拿
    approval_id → 循环 GET /requests/{id}?block=1 直到 status=="decided"（server 单次最多 block
    120s 会回 {"status":"pending"}，故外层循环）。失败（非 200 / 超时）抛 RuntimeError，
    由 @guard 收成 error ToolResult——worker 不静默放行。
    """
    import httpx  # noqa: PLC0415 —— 只在真 interrupt 时才需要网络，保持模块 import 轻

    url = base_url or _inbox_base_url()
    payload: dict = {
        "worker_id": worker_id,
        "tool_name": tool_name,
        "tool_args": tool_args,
        "description": description,
    }
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(base_url=url, timeout=60.0) as client:
        resp = await client.post("/requests", json=payload)
        if resp.status_code != 200:
            raise RuntimeError(f"审批收件箱入队失败: HTTP {resp.status_code} {resp.text[:200]}")
        approval_id = resp.json()["approval_id"]
        while True:
            rr = await client.get(f"/requests/{approval_id}", params={"block": "1"})
            if rr.status_code != 200:
                raise RuntimeError(f"等待审批决定失败: HTTP {rr.status_code}")
            decision = rr.json()
            if decision.get("status") == "decided":
                return bool(decision.get("approved"))
            if time.monotonic() >= deadline:
                raise RuntimeError(f"等待审批决定超时（{timeout:.0f}s），未获得主侧决定")


async def _run_with_remote_approval(
    graph,
    state: dict,
    *,
    base_url: str | None = None,
    worker_id: str | None = None,
) -> dict:
    """跑子任务图：遇 __interrupt__ 经 HTTP 送回主侧审批、以 Command(resume) 续跑。

    与主侧 app/tui/driver.py::drive_turn 同形态，只把"本地 broker park"换成"POST 主侧收件箱 +
    长轮询取回"。worker 图由 ReviewNode 产生 interrupt（逐条 drain，一次一条 need_review 调用），
    因此正常路径单 interrupt/轮；多条走兜底合成一次审批（与 drive_turn 兜底语义一致）。
    返回终态（无 __interrupt__ 的 result），由调用方提取结论。
    """
    config: dict = {
        "configurable": {"thread_id": uuid.uuid4().hex},
        "recursion_limit": _MAX_STEPS,
    }
    wid = worker_id or _worker_id()
    inputs = state
    for _ in range(_MAX_STEPS):
        result = await graph.ainvoke(inputs, config)
        interrupts = result.get("__interrupt__")
        if not interrupts:
            return result
        values = [getattr(it, "value", it) for it in list(interrupts)]
        if len(values) == 1 and isinstance(values[0], dict):
            tool_name, tool_args, description = _value_to_approval(values[0])
        else:
            # 兜底：多条 interrupt 合成一次审批（正常路径不会到这）
            names = [
                (v.get("tool_name") if isinstance(v, dict) else "?") for v in values
            ]
            tool_name, tool_args, description = "多工具审批", {}, "一次请求审批多项 worker 工具：" + "、".join(names)
        approved = await _await_main_decision(description, tool_name, tool_args, worker_id=wid, base_url=base_url)
        inputs = Command(resume={"approved": approved})
    raise RuntimeError(f"worker 子任务超过步数上限 {_MAX_STEPS}")


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
    inbox_url: str | None = None,
) -> ToolResult:
    """run_subtask 的核心实现；参数可注入便于测试（假 model / 假 tools，不拉真实子进程）。

    - task：子任务描述；空白 → InvalidArgumentError。
    - workspace：工作区绝对路径（须存在）。
    - tools：默认经 load_mcp_tool(workspace) 拉取后按 app.agent.tools::worker_tools 筛成可用
      子集（只读检索 + 联网，后者 need_review 会触发审批）；注入时直接用。
    - model：默认 app.agent.graph::get_sub_agent_graph 内建 get_main_chat_model()；注入假模型则无需
      真实 LLM。
    - inbox_url：主侧审批收件箱基地址；默认读 env AGENT_INBOX_URL。仅 need_review 调用被
      interrupt 时才发起 HTTP（只读路径全程不需主侧）。

    子图/人设/工具子集均为懒加载（app.agent），模块 import 阶段不触发 get_settings。
    """
    from app.agent.mcp import load_mcp_tool  # noqa: PLC0415 —— 懒加载：见模块 docstring
    from app.agent.tools import worker_tools  # noqa: PLC0415
    from app.agent.graph import get_sub_agent_graph  # noqa: PLC0415

    if not isinstance(task, str) or not task.strip():
        raise InvalidArgumentError("task 不能为空")

    workspace = str(Path(workspace).expanduser().resolve())

    if tools is None:
        tools = worker_tools(await load_mcp_tool(workspace))

    graph = get_sub_agent_graph(tools, workspace, model=model)

    state: dict = {
        "session_id": uuid.uuid4().hex,
        "messages": [HumanMessage(content=task)],
        "pending_tool_calls": [],
        "approved_tool_calls": [],
        "approved_orchestrate_calls": [],
        "current_plan": [],
        "notes": {},
    }
    result = await _run_with_remote_approval(graph, state, base_url=inbox_url)
    return ToolResult(success=True, content=_extract_conclusion(result))


@mcp.tool()
@guard
async def run_subtask(task: str) -> ToolResult:
    """把一条**子任务**（工作区调研 / 联网检索 / 读码 / 出方案）派发给独立子 agent 执行。

    子 agent 用**只读**工具（工作区文件检索 + git 只读）调查当前工作区，也可**联网检索**
    （web_search 等）——联网调用会先请求主 agent 人工审批、可能等待。它**不能改动工作区任何
    文件、不能执行命令**——需要落地改动时，由你在收到结论后自行执行。

    Args:
        task: 一条边界清晰的子任务描述（含目标与期望结论要点），如
            "调研 src/x.py 的实现，说明它如何做校验并给出 ≤5 行的中文总结"。

    Returns:
        结论正文（中文）；失败时以 [error_type] 前缀开头说明原因。
    """
    return await run_subtask_impl(task, _resolve_workspace())


if __name__ == "__main__":
    mcp.run(transport="stdio")
