"""agent 侧工具（模型可见的都在这）。

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
import asyncio
import sys
from pathlib import Path
from typing import Annotated, List

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolArg, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from app.agent.state import AgentState
from app.agent.utils import format_tool_result
from app.schema.agent_schema import PlanStatus, PlanStep

# 项目根：spawn worker 子进程时 cwd 用项目根使 .env 可读（worker 内懒加载 get_chat_model）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent




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
    plan:List[PlanStep] = [dict(step) for step in current]#type:ignore
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

@tool
def read_note(
    note_id: str,
    state: Annotated[AgentState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> dict:
    """按 ref 读取会话笔记正文（此前联网检索等被折叠归档的原文）。

    何时用：折叠摘要/系统提示里出现「详情见 notes#rN」之类的指针、需要看原文时才调用。
    notes 本身只注入一行索引，正文都通过本工具按需取回；取回的正文是短命消息，看完即弃。

    Args:
        note_id: 笔记 ref，形如 "notes#r3"（来自折叠摘要行尾的「详情见」指针）。
        state: （系统自动注入，无需传入）当前 AgentState，读取 notes。
        tool_call_id: （系统自动注入，无需传入）本次调用 id。

    Returns:
        命中：笔记正文（markdown，含标题头）；未命中：提示该 ref 不存在的错误消息。
    """
    note = (state.get("notes") or {}).get(note_id)
    if not note:
        content = f"笔记不存在: {note_id}"
    else:
        title = (note.get("title") or "").strip() or note_id
        body = str(note.get("content") or "").strip()
        content = f"### {note_id} · {title}\n\n{body}" if body else f"### {note_id} · {title}\n（空笔记）"
    return {
        "messages": [
            ToolMessage(name="read_note", tool_call_id=tool_call_id, content=content)
        ]
    }


orchestrate_tool: List[BaseTool] = [
    create_plan,
    update_plan_step,
    clear_plan,
]

# 笔记读回工具：与编排工具同属"agent 侧 state 工具"，走 OrchestrateNode 执行器（source="notes"）。
note_tools: List[BaseTool] = [
    read_note,
]

# ------------------------- 派发子任务（dispatch，source="dispatch"）-------------------------
# 仿 create_plan、走 orchestrate 层（不普通 MCP 工具）。设计：不静态绑 worker，每次调用按
# 子任务"现场 spawn"独立 worker（mcp_service/sub_agent.py）跑 run_subtask，N 个经
# asyncio.gather 并发、干完即回收——不受"先启动服务 / 最多一个子 agent"限制。
# worker_runner 做成模块级可替换，便于测试注入假执行器、不真 spawn。


class InjectedWorkspace(InjectedToolArg):
    """标记 dispatch 的 workspace 参数为运行时注入（对模型隐藏）。"""


async def _spawn_subagent_worker(task: str, workspace: str) -> str:
    """默认 worker 执行器：现场 spawn 一个 sub_agent stdio 子进程跑 run_subtask。

    langchain_mcp_adapters 的 get_tools 工具是"每次调用开一个新会话"：每次 ainvoke 拉起
    一个 `python -m mcp_service.sub_agent` 子进程、调用 run_subtask、结束后回收，因此
    N 次并发 = N 个独立 worker 进程，无预启动、无常驻泄漏。cwd=项目根使 worker 进程能
    读到项目 .env 的 CHAT_*；WORKSPACE_PATH 指向本 agent 工作区（worker 在其上只读干活）。
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: PLC0415

    client = MultiServerMCPClient(
        {
            "worker": {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "mcp_service.sub_agent"],
                "cwd": str(_PROJECT_ROOT),
                "env": {
                    "WORKSPACE_PATH": str(workspace),
                    "PYTHONPATH": str(_PROJECT_ROOT),
                },
            }
        }
    )
    tools = await client.get_tools()
    run_subtask = next(t for t in tools if t.name == "run_subtask")
    result = await run_subtask.ainvoke({"task": task})
    # MCP adapters 返回 content-block 形态 → 复用工具结果归一化（utils），空正文兜底
    return format_tool_result(result) or "（worker 未返回正文）"


# 模块级可替换的 worker 执行器；None = 用默认 _spawn_subagent_worker（测试注入假执行器）
worker_runner = None


async def run_subtask_batch(
    sub_tasks: list[str],
    workspace: str,
    runner=None,
) -> list[dict]:
    """并发跑一批只读子任务；每个子任务一个独立 worker（默认 spawn sub_agent 进程）。

    返回按输入顺序排列的 [{task, ok, content}]；单 worker 失败不拖垮整批
    （记 ok=False，content 带原因），由调用方决定是否重试/换法。
    """
    run = runner or worker_runner or _spawn_subagent_worker

    async def _one(task: str) -> dict:
        try:
            content = await run(task, workspace)
            return {"task": task, "ok": True, "content": str(content)}
        except Exception as exc:  # noqa: BLE001 —— worker 进程失败等，收口为条目
            return {"task": task, "ok": False, "content": f"worker 执行失败：{exc}"}

    return list(await asyncio.gather(*(_one(t) for t in sub_tasks)))


@tool
async def dispatch_subtasks(
    sub_tasks: list[str],
    workspace: Annotated[str, InjectedWorkspace],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> dict:
    """把多个**只读**子任务并发派发给独立 worker（子 agent），拿回每个的结论。

    何时用：手里的任务能拆成几个相互独立的调查/分析/出方案子任务（各写各的边界、
    互不依赖）时，一次把清单给我，由系统对每个子任务起一个独立 worker 进程执行——
    它们只读当前工作区（文件检索 + git 只读），不能改文件、不能执行命令，需要落地
    改动时由你基于结论自行执行。完成后每个子任务给一段结论正文（带出处）。

    何时别用：子任务之间有依赖（下游要等上游产物）——那是先后顺序的事，别并发；
    或单条任务根本不需要拆——直接自己做即可，不要为派发而派发。

    Args:
        sub_tasks: 子任务描述列表（每条约一个独立调查/分析目标，含期望结论要点）。
            每个子任务各由一个独立 worker 执行；互不共享上下文。

    Returns:
        汇总文本：逐个子任务的结果（✓/✗ + 结论或失败原因）。
    """
    results = await run_subtask_batch(sub_tasks, workspace)
    # 汇总正文很短，直接内联（不再抽独立渲染函数）
    lines = [f"已并发派发 {len(results)} 个只读子任务，结果："]
    for i, r in enumerate(results, 1):
        mark = "✓" if r["ok"] else "✗"
        lines.append(f"[{i}] ({mark}) 子任务「{r['task']}」")
        lines.append(str(r["content"]))
    summary = "\n".join(lines)
    return {
        "messages": [
            ToolMessage(
                name="dispatch_subtasks",
                tool_call_id=tool_call_id,
                content=summary,
            )
        ]
    }


dispatch_tool: List[BaseTool] = [
    dispatch_subtasks,
]
