"""agent 侧工具（模型可见的都在这）。

- `core_tools`：真实副作用/查询工具（web_search），走 review → ToolNode。
- `orchestrate_tool`：编排工具（create_plan / update_plan_step / clear_plan），
  由编排节点（orchestrate_node）消费。与普通工具的关键区别：

  * 只操作会话内的 `current_plan`（跨轮持久、放在 AgentState 里），不产生真实副作用；
  * `state`（读当前计划）与 `tool_call_id`（消息回执）用 `InjectedState` /
    `InjectedToolCallId` 标注，会从模型可见 schema 里剔除——模型只看到真正的业务参数
    （如 `steps` / `step_id` / `status`）；
  * 返回值 = "要写回 state 的切片" `{current_plan: [...], messages: [ToolMessage]}`，
    编排节点负责把切片合并写回、把消息交给模型；
  * 计划的核心逻辑（规划、推进状态、清空、快照回显）由本模块内实现，
    不放在 utils.py——工具自成一个整体，docstring 即给模型的使用手册。

计划的跨轮协议（模型须知道的约定，见各工具 docstring）：
- 会话内始终维护一份 current_plan，步骤形如 {id: "1", task, status}；
- id 在 create_plan 时按 "1","2",… 分配，后续用 id 引用；
- status ∈ pending / in_progress / done；
- 工具响应会把最新计划快照回显，供模型跨轮跟踪；没有独立的"查看计划"工具。
"""
from typing import Annotated, List

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langchain_tavily import TavilySearch

from app.config import get_settings
from app.agent.state import AgentState
from app.schema.agent_schema import PlanStatus, PlanStep

settings = get_settings()

web_search = TavilySearch(
    tavily_api_key=settings.TAVILY_API_KEY
)

# 计划步骤允许的状态取值（与 PlanStatus Literal 保持一致）
PLAN_STATUSES: tuple[str, ...] = ("pending", "in_progress", "done")


def _snapshot(plan: list[PlanStep]) -> str:
    """把计划渲染成清单文本（done 打勾），作为给模型的回显正文。"""
    lines = []
    for step in plan:
        mark = "x" if step.get("status") == "done" else " "
        lines.append(f"- [{mark}] {step.get('id')}. {step.get('task')}")
    return "\n".join(lines)


@tool
def create_plan(
    steps: list[str],
    state: Annotated[AgentState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> dict:
    """创建一份新计划，并覆盖会话内已有的旧计划。

    会话内始终维护"当前计划 current_plan"：按执行顺序排列的步骤列表，每步为
    {id, task, status}，跨多轮对话持久保存。创建/推进/清空都通过编排工具完成，
    本工具的响应会把最新计划清单回显给你。

    何时该用：接到需要多步完成的任务（如"先读代码 → 再修改 → 再测试"）时，
    先 create_plan 把任务拆成有序步骤，再按步骤依次调用其它工具执行。
    何时别用：计划已存在、只想推进某步进度——那是 update_plan_step 的职责，
    不要每轮都重新建计划（重建会丢弃已记录的进度）。

    Args:
        steps: 按执行顺序排列的任务字符串列表；空白项会被忽略。
            生成的步骤 id 从 "1" 开始递增（"1","2","3",…），
            之后用 update_plan_step 更新进度时以这些 id 引用。
        state: （系统自动注入，无需传入）当前 AgentState，读取/替换 current_plan。
        tool_call_id: （系统自动注入，无需传入）本次调用 id，用于把回执关联到对话。

    Returns:
        更新后的完整 current_plan，并在消息里附上计划清单快照。
    """
    # 规划：过滤空白项，按序分配 "1","2",…，初始状态一律 pending
    plan: list[PlanStep] = []
    for task in steps:
        if not isinstance(task, str) or not task.strip():
            continue
        plan.append({"id": str(len(plan) + 1), "task": task, "status": "pending"})

    return {
        "current_plan": plan,
        "messages": [
            ToolMessage(
                name="create_plan",
                tool_call_id=tool_call_id,
                content=f"已生成计划（{len(plan)} 步）\n{_snapshot(plan)}",
            )
        ],
    }


@tool
def update_plan_step(
    step_id: str,
    status: PlanStatus,
    state: Annotated[AgentState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> dict:
    """把计划中指定步骤标记为目标状态，用于推进任务进度。

    计划建好后，跨轮推进进度的唯一入口：开始执行某步 → 置为 in_progress；
    该步完成 → 置为 done。只会更新你要的那一步，其余步骤不受影响。

    仅能操作最近一次 create_plan 回显给你的步骤 id；若 id 不存在
    （计划被清空/重建，或编号笔误），本工具返回错误且不改动计划。

    Args:
        step_id: 要更新的步骤 id，即 create_plan 清单里的编号（"1","2",…）。
        status: 目标状态，允许 pending（未开始）/ in_progress（进行中）/ done（已完成）。
        state: （系统自动注入，无需传入）当前 AgentState，读取 current_plan。
        tool_call_id: （系统自动注入，无需传入）本次调用 id。

    Returns:
        成功：current_plan + 状态回执消息；失败：仅返回错误消息，计划保持不变。
    """
    current = list(state.get("current_plan", []))

    if status not in PLAN_STATUSES:
        return {
            "messages": [
                ToolMessage(
                    name="update_plan_step",
                    tool_call_id=tool_call_id,
                    content=f"非法状态 {status!r}（可用：{' / '.join(PLAN_STATUSES)}）",
                )
            ]
        }

    idx = next((i for i, step in enumerate(current) if step.get("id") == step_id), None)
    if idx is None:
        return {
            "messages": [
                ToolMessage(
                    name="update_plan_step",
                    tool_call_id=tool_call_id,
                    content=f"步骤 {step_id} 不存在",
                )
            ]
        }

    # 浅拷贝出新列表再改，保证不改动原 state 里的列表（LangGraph state 应不可变更新）
    plan:List[PlanStep] = [dict(step) for step in current]
    plan[idx] = {**plan[idx], "status": status}

    return {
        "current_plan": plan,
        "messages": [
            ToolMessage(
                name="update_plan_step",
                tool_call_id=tool_call_id,
                content=f"步骤 {step_id} 已标记为 {status}\n{_snapshot(plan)}",
            )
        ],
    }


@tool
def clear_plan(
    state: Annotated[AgentState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> dict:
    """清空当前计划。

    当任务已整体完成、或计划已不再适用（需求变更需要重新规划）时调用。
    清空后所有旧步骤 id 全部失效；需要新计划时请重新 create_plan。

    Args:
        state: （系统自动注入，无需传入）当前 AgentState，读取 current_plan。
        tool_call_id: （系统自动注入，无需传入）本次调用 id。

    Returns:
        置空后的 current_plan + 清空回执消息。
    """
    return {
        "current_plan": [],
        "messages": [
            ToolMessage(
                name="clear_plan",
                tool_call_id=tool_call_id,
                content="已清空全部计划",
            )
        ],
    }


core_tools: List[BaseTool] = [
    web_search
]

orchestrate_tool: List[BaseTool] = [
    create_plan,
    update_plan_step,
    clear_plan,
]
