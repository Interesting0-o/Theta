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
import json
import sys
from datetime import date
from pathlib import Path
from typing import Annotated, List

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolArg, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from app.agent import memory
from app.agent.state import AgentState
from app.agent.utils import format_tool_result
from app.schema.agent_schema import PlanStatus, PlanStep

# 项目根：spawn worker 子进程时 cwd 用项目根使 .env 可读（worker 内懒加载 get_main_chat_model）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent




# 计划步骤允许的状态取值（与 PlanStatus Literal 保持一致）
PLAN_STATUSES: tuple[str, ...] = ("pending", "in_progress", "done")

# ------------------------- worker（子 agent）可用工具子集 -------------------------
# worker 定位 = 主 agent 的并发资料收集助手，其可用工具按 tool.json 过滤：
#   工作区只读检索（file_io 读 + git 读，need_review:false，免审批）
#   ∪ 联网检索四件套（web_search 系，need_review:true → 触发主侧审批）。
# 终端（连只读 process_*）、git 写、plan/notes/dispatch 一概不进 worker。
_WORKSPACE_SOURCES: frozenset[str] = frozenset({"mcp_service/file_io", "mcp_service/git"})
_WEB_SOURCES: frozenset[str] = frozenset({"mcp_service/web_search"})
_WORKER_TOOL_CONFIG = Path(__file__).with_name("tool.json")


def worker_tools(tools, cfg: dict | None = None):
    """从工具表筛出 worker 可用子集（工作区只读检索 + 联网检索）。

    - cfg：tool.json 解析结果 {tool_name: {need_review, source}}；默认读 app/agent/tool.json。
    - 保留：name 命中 cfg 且（`need_review:false` 且 source∈{file_io,git}）——免审的只读检索；
      或 source∈{web_search}——联网四件套（need_review:true，会触发主侧审批）。
    - 剔除：写/删/命令/git 写/plan/notes/dispatch/memory 等（或改状态、或需更高权限、
      或会再派生子任务；memory 被剔除 = worker 不读写长期记忆，保持只读调查隔离）。
    - 纯函数：不依赖 MCP 加载，便于单测注入假 cfg 验证过滤语义。
    """
    if cfg is None:
        with _WORKER_TOOL_CONFIG.open("r", encoding="utf-8") as file:
            cfg = json.load(file)
    allowed: set[str] = set()
    for name, conf in cfg.items():
        source = conf.get("source")
        if source in _WORKSPACE_SOURCES and not conf.get("need_review"):
            allowed.add(name)
        elif source in _WEB_SOURCES:
            allowed.add(name)
    return [t for t in tools if getattr(t, "name", None) in allowed]


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
    """标记"需要工作区"的工具参数为运行时注入（对模型隐藏）。

    由 OrchestrateNode 按签名代填（nodes.py::OrchestrateNode._invoke）：dispatch 往该工作区
    spawn worker，memory 系列工具用它定位 `resource/<ws_key>/memory/memory.md`。模型看不到
    这个参数，也就无法指定记忆文件路径。
    """


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
    """并发跑一批资料收集子任务；每个子任务一个独立 worker（默认 spawn sub_agent 进程）。

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
    """把多个**相互独立**的查证/调研问题，并行派给一批一次性 worker 子 agent 快速收集资料，
    拿回每个的结论正文。

    何时用：当前任务需要"先并行搜集一堆互不相关的资料/事实"时——例如分别调研工作区里几个
    模块各自怎么实现、分别查几份 API 文档/报错资料、分别读几块代码的职责。把每个独立问题写成
    一条 sub_task 一次派出，由系统对每条起一个独立 worker 进程**并发**执行，比你自己逐条串行
    读/搜快得多。派发本身免审批。

    worker 的能力边界（重要）：
    - worker 能：只读当前工作区（文件检索 + git 只读）+ **联网检索**（web_search 等；其联网
      调用会以"子任务审批"形式出现在人工审批、可能等待）。
    - worker 不能：改任何文件、执行任何命令、做最终决策——它返回"结论正文 + 出处"，**只是给
      你做判断的素材**；它上下文独立，看不到其它 worker 的结果。
    - 因此真正"干活"（改动、验证、给用户答复）仍是你自己：收到结论先汇总/交叉核对，需要落地
      改动时由你用写/命令工具执行，不要指望 worker 替你改。

    何时别用：子问题之间有依赖（下游要吃上游产物、需按先后做）——那不该并发，留给你自己按
    计划推进；或单问单答、拆无可拆——直接自己做即可，不要为派发而派发。

    用法提示：每条 sub_task 写成一条**自包含**的调研问题（含想要拿到的结论要点与出处要求），
    让 worker 查完即可回报、不必依赖别人；一次别派太多（建议 ≤5 条），太长就分批，避免并行
    结果过长、审批轰炸。

    Args:
        sub_tasks: 相互独立的调研/资料收集问题列表（每条约一个调查目标，含期望结论要点）。
            每条各由一个独立 worker 执行；互不共享上下文。

    Returns:
        汇总文本：逐个子任务的结果（✓/✗ + 结论或失败原因）。
    """
    results = await run_subtask_batch(sub_tasks, workspace)
    # 汇总正文很短，直接内联（不再抽独立渲染函数）
    lines = [f"已并发派出 {len(results)} 个资料收集 worker，结果："]
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

# ------------------------- 长期记忆（source="memory"）-------------------------
# 记忆 = 跨会话、按工作区共享的稳定结论，落 resource/<ws_key>/memory/memory.md——磁盘上的
# 一等真值文件（不是向量索引那种游离副本，见 docs/LONG_TERM_MEMORY.md §1）。文件路径由注入的
# workspace 经 app.resource.memory_root 算出、**对模型隐藏**：模型只给 type/content/key。
#
# 与 read_note 的分工：notes 是会话内折叠归档（短命、ref=rN，随 checkpoint 存亡），memory 跨
# 会话长存（ref=mN）；两者并存、语义分开。与 dispatch 同走 OrchestrateNode，因为都需要注入
# 工作区（对模型隐藏的 InjectedWorkspace）。
#
# 两个工具都写成 **async** 且把落盘交给 asyncio.to_thread：读写会碰 os.stat / os.mkdir
# （Path.exists / mkdir / resolve 底下），langgraph dev 的 blockbuster 会在事件循环里拦截这类
# 阻塞调用——与 mcp.py::load_mcp_tool、graph.py 默认工作区 mkdir 的处理一致。


def _memory_receipt(tool_name: str, tool_call_id: str, content: str) -> dict:
    """记忆工具的返回切片：只回一条 ToolMessage（这两个工具不碰 state）。"""
    return {
        "messages": [
            ToolMessage(name=tool_name, tool_call_id=tool_call_id, content=content)
        ]
    }


@tool
async def write_memory(
    type: str,
    content: str,
    workspace: Annotated[str, InjectedWorkspace],
    tool_call_id: Annotated[str, InjectedToolCallId],
    key: str = "",
) -> dict:
    """把一条**跨会话仍然有用**的稳定事实写进本工作区的长期记忆。

    长期记忆按工作区保存、跨会话长存（下次再做这个项目时还读得到）——所以这里写的是"下次再来
    做这个项目时仍然成立"的东西，而不是本次任务的流水账。

    何时该写（一段有结论的工作收尾时）：
    - 用户明确的偏好 / 约束（如"输出与注释用中文""不要动 venv"）；
    - 敲定的架构决策（如"子 agent 传输定案 stdio，HTTP 等 DAG 再说"）；
    - 工作区的既有约定或特殊之处（如"测试必须用 python -m pytest 并导出 WORKSPACE_PATH"）。
    何时别写：
    - 临时过程与操作流水（"我读了 X 文件"）；猜测、未定方案、还没验证的结论；
    - 随时能从工作区 / git 重取的 dump（代码内容、目录结构、文件清单）。

    写法：content 一句话成条、带结论（必要时附出处或日期）；type 取
    user-preference（用户偏好 / 约束）/ decision（决策）/ convention（约定）/
    project-fact（项目事实）。

    Args:
        type: 记忆类别，建议取 user-preference / decision / convention / project-fact。
        content: 一句话结论；空内容会被拒绝（不写空记忆）。
        key: 省略 = 追加一条新记忆（编号由系统分配，如 m3）；
            给了已有编号（如 "m2"）= 覆写那一条的类别与正文（编号与首次记入日期不变），
            用于修正写错 / 过时的记忆——**不要靠追加"更正：…"来叠**。
        workspace: （系统自动注入，无需传入）本会话工作区。
        tool_call_id: （系统自动注入，无需传入）本次调用 id。

    Returns:
        回执：写入 / 覆写的条目 + 当前总条数。要看全部记忆用 read_memory。
    """
    kind = (type or "").strip()
    body = (content or "").strip()
    if not body:
        return _memory_receipt(
            "write_memory", tool_call_id, "记忆内容为空，未写入。content 要写成一句有结论的事实。"
        )
    if not kind:
        return _memory_receipt(
            "write_memory",
            tool_call_id,
            "记忆类别为空，未写入。type 建议取 user-preference / decision / convention / project-fact。",
        )

    target = (key or "").strip()
    if target:
        entry = await asyncio.to_thread(memory.overwrite_entry, workspace, target, kind, body)
        if entry is None:
            keys = await asyncio.to_thread(memory.entry_keys, workspace)
            existing = "、".join(keys) or "（暂无）"
            return _memory_receipt(
                "write_memory",
                tool_call_id,
                f"记忆 {target} 不存在，未做任何改动。现有编号：{existing}；要新增请省略 key。",
            )
        action = f"已覆写 [{entry.key}]（首次记入日期 {entry.date} 不变）"
    else:
        entry = await asyncio.to_thread(
            memory.append_entry, workspace, kind, body, date.today().isoformat()
        )
        action = f"已写入 [{entry.key}]"

    total = len(await asyncio.to_thread(memory.entry_keys, workspace))
    return _memory_receipt(
        "write_memory",
        tool_call_id,
        f"{action} {entry.type} · {entry.date}\n{entry.content}\n\n"
        f"当前该工作区共 {total} 条长期记忆（要看全部用 read_memory）。",
    )


@tool
async def read_memory(
    workspace: Annotated[str, InjectedWorkspace],
    tool_call_id: Annotated[str, InjectedToolCallId],
    key: str = "",
) -> dict:
    """读取本工作区的长期记忆（跨会话保存的偏好 / 决策 / 约定 / 项目事实）。

    何时用：需要确认"用户或这个项目之前定过什么"时——动手前核对既有约定，或想找某条记忆的
    编号以便用 write_memory 覆写它。想一眼看全当前记了些什么也用它。

    Args:
        key: 省略 = 返回全部记忆；给编号（如 "m3"）= 只返回那一条。
        workspace: （系统自动注入，无需传入）本会话工作区。
        tool_call_id: （系统自动注入，无需传入）本次调用 id。

    Returns:
        命中：记忆正文（全文或单条）；无记忆 / 编号不存在：说明情况并列出现有编号。
    """
    text = await asyncio.to_thread(memory.read_text, workspace)
    if not text.strip():
        return _memory_receipt("read_memory", tool_call_id, "当前工作区还没有长期记忆。")

    entries = memory.parse_entries(text)
    target = (key or "").strip()
    if target:
        entry = next((e for e in entries if e.key == target), None)
        if entry is None:
            existing = "、".join(e.key for e in entries) or "（暂无）"
            return _memory_receipt(
                "read_memory", tool_call_id, f"记忆 {target} 不存在。现有编号：{existing}"
            )
        return _memory_receipt(
            "read_memory",
            tool_call_id,
            f"### [{entry.key}] {entry.type} · {entry.date}\n\n{entry.content}",
        )

    if not entries:
        return _memory_receipt(
            "read_memory", tool_call_id, "当前工作区还没有长期记忆条目（只有文件头说明）。"
        )
    visible = memory.strip_comments(text).strip()
    return _memory_receipt(
        "read_memory", tool_call_id, f"当前该工作区共 {len(entries)} 条长期记忆：\n\n{visible}"
    )


memory_tool: List[BaseTool] = [
    write_memory,
    read_memory,
]
