"""agent 侧工具（模型可见的都在这）：

- `core_tools`：真实副作用/查询工具（web_search），走 review → ToolNode。
- `orchestrate_tool`：编排工具（create_plan / update_plan_step / clear_plan）。
  函数体是真实的：`state` 用 `InjectedState` 标注、`tool_call_id` 用
  `InjectedToolCallId` 标注——两者都从 bind_tools 的 schema 里剔除，模型看不到，
  由 OrchestrateNode 在拿到 tool_calls 后把当前 state 与模型生成的参数一起注入再调用。
  工具返回值 = "要写回 state 的部分切片"（如 `current_plan`，可附带一条 ToolMessage
  作为给模型的正文），由 OrchestrateNode 合并写回。计划纯状态转换见 app/agent/utils.py。
"""
from typing import Annotated, List

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import InjectedState, InjectedToolCallId
from langchain_tavily import TavilySearch

from app.config import get_settings
from app.agent.state import AgentState
from app.agent.utils import apply_step_status, empty_plan, plan_snapshot, steps_to_plan
from app.schema.agent_schema import PlanStatus

settings = get_settings()

web_search = TavilySearch(
    tavily_api_key=settings.TAVILY_API_KEY
)


@tool
def create_plan(
    steps: Annotated[list[str], "按顺序纳入计划的任务列表"],
    state: Annotated[AgentState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> dict:
    """按顺序任务列表创建计划。"""
    plan = steps_to_plan(steps)
    return {
        "current_plan": plan,
        "messages": [
            ToolMessage(
                name="create_plan",
                tool_call_id=tool_call_id,
                content=f"已生成计划（{len(plan)} 步）\n{plan_snapshot(plan)}",
            )
        ],
    }


@tool
def update_plan_step(
    step_id: Annotated[str, '要更新状态的步骤 id（创建时分配的 "1","2",...）'],
    status: PlanStatus,
    state: Annotated[AgentState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> dict:
    """把指定步骤标记为某状态（pending / in_progress / done）。"""
    # 只读当前计划，构造新列表写回（不改动传入的 state）
    plan = list(state.get("current_plan", []))
    new_plan, ok, msg = apply_step_status(plan, step_id, status)
    if not ok:
        return {
            "messages": [
                ToolMessage(
                    name="update_plan_step",
                    tool_call_id=tool_call_id,
                    content=msg,
                )
            ]
        }
    return {
        "current_plan": new_plan,
        "messages": [
            ToolMessage(
                name="update_plan_step",
                tool_call_id=tool_call_id,
                content=f"{msg}\n{plan_snapshot(new_plan)}",
            )
        ],
    }


@tool
def clear_plan(
    state: Annotated[AgentState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> dict:
    """清除当前计划。"""
    return {
        "current_plan": empty_plan(),
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
