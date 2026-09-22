"""agent 侧工具（模型可见的都在这）。

本模块的**编排工具**按 docs/ARCHITECTURE.md §4 的**四问**判定：改动的是 **agent 自身的运行
语义**（plan / notes / ask_user / memory / skill），不是外部世界。**它们改变的方式各不相同、
返回值形状也不统一**——不要拿返回类型给"编排工具"下定义。**副作用有无也不一**：
`write_memory`（写盘）、`get_skill` / `drop_skill`（起/关技能运行体）**有**；计划三件套、
`read_note`、`ask_user`、`read_memory` 无。

（`dispatch_subtasks` **不在本模块**：2026-09-21 起它是 `mcp_service/dispatch.py` 那个 server 的
工具——它派生 worker = 独立进程与独立 Agent runtime，命中四问的第四问。）

- `orchestrate_tool`：计划三件套（create_plan / update_plan_step / clear_plan），
  由编排节点（orchestrate_node）消费。下面几条**只描述这一组**：

  * 只操作会话内的 `current_plan`（跨轮持久、放在 AgentState 里），**不产生真实副作用**；
  * `state`（读当前计划）与 `tool_call_id`（消息回执）用 `InjectedState` /
    `InjectedToolCallId` 标注，会从模型可见 schema 里剔除——模型只看到真正的业务参数
    （如 `steps` / `step_id` / `status`）；
  * 返回值 = "要写回 state 的切片"：本组给 `current_plan` + `messages`。**形状规则以执行器
    为准**（nodes.py::OrchestrateNode，那里是本协议的权威——messages 必给、current_plan 可选且
    链式、其余切片要在 `_EXTRA_SLICE_KEYS` 里登记）；本模块只按它写，不另立一份说法；
  * 计划的核心逻辑（规划、推进状态、清空、快照回显）由本模块内实现，
    不放在 utils.py——工具自成一个整体，docstring 即给模型的使用手册。

计划的跨轮协议（模型须知道的约定，见各工具 docstring）：
- 会话内始终维护一份 current_plan，步骤形如 {id: "1", task, status}；
- id 在 create_plan 时按 "1","2",… 分配，后续用 id 引用；
- status ∈ pending / in_progress / done；
- 工具响应会把最新计划快照回显，供模型跨轮跟踪；没有独立的"查看计划"工具。
"""
import asyncio
import logging
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date
from importlib.machinery import PathFinder
from pathlib import Path
from typing import Annotated, List, Literal, get_args

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolArg, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from app.agent import gates
from app.platform import mcp
from app.resource import memory, skills
from app.agent.state import AgentState
from app.config import skill_env
from app.exception import ConfigError
from app.schema.agent_schema import PlanStatus, PlanStep, SkillMeta

# 项目根：spawn worker 子进程时 cwd 用项目根使 .env 可读（worker 内懒加载 LLMNode.main_chat_model）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

logger = logging.getLogger(__name__)


# 计划步骤允许的状态取值——**从 PlanStatus 派生**，不另抄一份（同源才能保证校验与类型不漂移）
PLAN_STATUSES: tuple[str, ...] = get_args(PlanStatus)

# ------------------------- worker（子 agent）可用工具子集 -------------------------
# worker 定位 = 主 agent 的并发资料收集助手，其可用工具按 tool.json 过滤：
#   工作区只读检索（file_io 读，need_review:false，免审批）
#   ∪ 联网检索四件套（web_search 系，need_review:true → 触发主侧审批）。
# 终端（连只读 process_*）、plan/notes 一概不进 worker；dispatch_subtasks 同理——它现在是
# 普通 MCP 工具（source: mcp_service/dispatch），两条 source 规则都不命中、也没有 worker_allow，
# 天然被剔除（这条防线专防**递归派发**，别把它加进 _WORKSPACE_SOURCES）。
_WORKSPACE_SOURCES: frozenset[str] = frozenset({"mcp_service/file_io"})
_WEB_SOURCES: frozenset[str] = frozenset({"mcp_service/web_search"})
# tool.json 里显式标了 worker_allow 的工具：**逐条授权**，不按 source 批量放行。
# 为什么需要它：现有的两条规则都是"source 命中 ∧ need_review:false"，而 run_command 是
# need_review:true（它必须能触发审批），加不进任何一条。终端工具下放的语义也不是"这个 source
# 对 worker 开放"，而是"这几条命令工具交给 worker 用"——所以按名字授权，且权威仍在 tool.json。


def worker_tools(tools, cfg: dict | None = None):
    """从工具表筛出 worker 可用子集（工作区只读检索 + 联网检索）。

    - cfg：tool.json 解析结果 {tool_name: {need_review, source}}；默认取 `gates.tool_config()`
      （解析与缓存的单点，与 ReviewNode 读的是同一份）。
    - 保留三类：① `need_review:false` 且 source∈{file_io}——免审的只读检索；
      ② source∈{web_search}——联网四件套；③ tool.json 标了 `worker_allow` 的工具——终端下放。
    - **①② 免审批、③ 走审批**：worker 的终止/命令类工具会经主侧统一 broker 请人批准
      （跨进程回传，见 mcp_service/sub_agent.py）。其中 run_command 还带
      `free_when: readonly_shell`——只读 git 子命令**免审**，所以"派 worker 去看仓库状态"
      不会产生任何审批；写命令才惊动人。
    - 剔除：写/删/plan/notes/memory 等（或改状态、或需更高权限），以及 dispatch_subtasks
      （会再派生子任务 → 递归）；memory 被剔除 = worker 不读写长期记忆，保持调查隔离。
    - 纯函数：不依赖 MCP 加载，便于单测注入假 cfg 验证过滤语义。
    """
    if cfg is None:
        cfg = gates.tool_config()
    allowed: set[str] = set()
    for name, conf in cfg.items():
        source = conf.get("source")
        if conf.get("worker_allow"):
            allowed.add(name)
        elif source in _WORKSPACE_SOURCES and not conf.get("need_review"):
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

# ------------------------- 向用户提问（ask，source="ask"）-------------------------
# 人机闸门的第二种载荷（docs/MULTI_AGENT.md §6）：模型在需求不明确时把选项摆给用户、等回答。
#
# **interrupt 不在本函数里**，而在 `ReviewNode` 的闸门分支（app/agent/nodes.py）。为什么：
# LangGraph 的 resume 会**整节点重跑**（实测见该处注释），interrupt 之前的外部副作用会被真切
# 第二次；`OrchestrateNode` 同批次里可能有 write_memory，所以它不能承载 interrupt。
# 分工是：**ReviewNode 提问并把人的回答写进 state 的 `ask_answers`**，本工具随后被
# OrchestrateNode 按常规执行、只负责把两段回答渲染成回执——闸门的产出是 state，执行器消费 state。
@tool
def ask_user(
    why: str,
    question: str,
    state: Annotated[AgentState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    options: list[str] | None = None,
    select: Literal["one", "many"] = "one",
) -> dict:
    """在动手之前，把"你判断不了、只能由用户定"的问题摆给用户选，并等他的回答。

    回答是**两段**：用户选中哪些选项（可留空），以及一句自由补充（可留空）。两段都收得到——
    **"没选任何选项、自己写了一段方案"同样是有效回答**，别当成没回答。

    什么时候该问（先自问一句：这个答案我是不是自己就能查到？）
    - 目标 / 范围 / 取舍不明确，且**猜错代价高**（白干一轮，或改动难以回退）；
    - 用户给的信息互相矛盾，走哪条路会显著改变后续做法；
    - 该由用户拍板的偏好（命名、风格、要不要兼容旧行为）——这类你猜就是错的。

    什么时候别问
    - 答案能靠工具查出来（读文件、检索、跑命令）——**先自己查**，别拿提问代替调研；
    - 用户其实已经说清楚了，你只是想确认一下——重复提问最消耗耐心；
    - 同一个问题你已经问过（本轮或上轮）——用户没答就按最佳判断继续，并在收尾时说明你的假设。

    怎么问
    - `why` 必填：一句话说清**为什么问**（卡在哪、答了会改变什么）。用户靠它决定要不要认真答；
    - `options`：2–5 项为宜（最多 5 项；留空 = 纯开放式提问）。每项写清**差别**而不是只给名字
      （如"浅克隆：只取最新一次提交，快，但看不了历史"）。把**你推荐的那项排第一**并写明理由；
      用户还可以在选项之外补充自己的方案，所以别把选项做成"封闭三选一"；
    - `select`：这题是**单选**（默认）还是**复选**。选项之间**互相独立、可以同时要**时用 `"many"`
      （"这几项里哪些要保留""还要补哪些"）——用户一次勾多项，你也不必自己造"两者都要"这种
      组合选项。**互斥的取舍**（走哪条路、用哪个方案、要不要兼容旧行为）保持默认单选：
      单选比多选省事，能单选就别多选。

    Args:
        why: 为什么问——卡在什么地方、这个回答会改变什么。
        question: 要用户回答的问题本身，一句话说清。
        options: 备选方案清单（0–5 项），每项写清与其他项的差别，推荐项排第一。
        select: "one"（默认，单选）/ "many"（复选）。选项可叠加时用 many。
        state: （系统自动注入，无需传入）取回用户在闸门处给出的两段回答。
        tool_call_id: （系统自动注入，无需传入）本次调用 id，用来对上回答。

    Returns:
        一条 `[user_answer]` 回执：用户选了什么、补充了什么；无人作答时如实说明。
    """
    answer = (state.get("ask_answers") or {}).get(tool_call_id) or {}
    raw_indexes = answer.get("option_indexes")
    indexes = raw_indexes if isinstance(raw_indexes, list) else []
    supplement = (answer.get("supplement") or "").strip()
    # ⚠️ 选项归一（去首尾空白 / 丢空项）**必须与闸门的 `gates.ask_request` 同口径**，且与
    # 面板编号（`app/tui/ui.py::_ask` 拿的是闸门归一后那份清单）一致：序号是这三处各自算出来的，
    # 口径一旦分叉，用户选的号会在其中一边越界、被静默当成"没选"。
    # 序号合法性本身由闸门在写回前把关（它拿的就是同一份清单），此处按"没选中任何给定选项"降级即可。
    opts = [str(item).strip() for item in (options or []) if str(item).strip()]
    picked = [(index + 1, opts[index]) for index in indexes if 0 <= index < len(opts)]

    if picked:
        # 序号展示是 **1 起始**（面板口径），内部一律 0 起始
        chosen = "、".join(f"{number}. {text}" for number, text in picked)
        if select == "many":
            # **多选即使只勾 1 项也写明总数**：否则模型会怀疑"是不是丢了几项"
            content = f"[user_answer] 用户选择了（多选，共 {len(picked)} 项）：{chosen}"
        else:
            content = f"[user_answer] 用户选择了：{chosen}"
        if supplement:
            content += f"\n用户补充：{supplement}"
    elif supplement:
        content = f"[user_answer] 用户未选任何给定选项，直接补充说明：{supplement}"
    else:
        content = (
            "[user_answer] 用户未作答（直接回车或输入已结束）。不要重复提问同一个问题；"
            "按你的最佳判断继续，并在收尾时说明你据此做的假设。"
        )
    return _tool_receipt("ask_user", tool_call_id, content)


ask_tool: List[BaseTool] = [
    ask_user,
]

class InjectedWorkspace(InjectedToolArg):
    """标记"需要工作区"的工具参数为运行时注入（对模型隐藏）。

    由 OrchestrateNode 按签名代填（nodes.py::OrchestrateNode._invoke）：memory 系列工具用它定位
    `resource/<ws_key>/memory/memory.md`（模型看不到这个参数，也就无法指定记忆文件路径），
    技能加载/卸载用它决定能力型 server 在哪个工作区里跑。

    **它是"住在主进程"的后果，不是判据**（2026-09-21 拍板）：按 docs/ARCHITECTURE.md §4 的四问，
    凡"改变 agent 自身运行语义"的工具都住在主进程；而住主进程就拿不到启动 env 里的工作区，只能
    靠注入。反例是 `file_io`——它同样需要工作区，却是普通工具，因为工作区就是它的启动配置。
    `dispatch_subtasks` 2026-09-21 搬去 `mcp_service/dispatch.py`（它派生独立进程 = 独立 Agent
    runtime，命中四问的第四问），工作区随改成从启动 env 取，离开了这条注入路径。
    """


# ------------------------- 长期记忆（source="memory"）-------------------------
# 记忆 = 跨会话、按工作区共享的稳定结论，落 resource/<ws_key>/memory/memory.md——磁盘上的
# 一等真值文件（不是向量索引那种游离副本，见 docs/LONG_TERM_MEMORY.md §1）。文件路径由注入的
# workspace 经 app.resource.memory_root 算出、**对模型隐藏**：模型只给 type/content/key。
#
# 与 read_note 的分工：notes 是会话内折叠归档（短命、ref=rN，随 checkpoint 存亡），memory 跨
# 会话长存（ref=mN）；两者并存、语义分开。**留在编排工具**（2026-09-21 拍板）：按四问它命中
# 第三问（涉及"持久化 / workspace"等资源）→ 方向是资源单点，不是 MCP；附带好处是读写都在主
# 进程，避开"注入在主进程读、写入在子进程写"这对跨进程碰同一批文件的组合。
#
# 两个工具都写成 **async** 且把落盘交给 asyncio.to_thread：读写会碰 os.stat / os.mkdir
# （Path.exists / mkdir / resolve 底下），langgraph dev 的 blockbuster 会在事件循环里拦截这类
# 阻塞调用——与 mcp.py::load_mcp_tool、graph.py 默认工作区 mkdir 的处理一致。


def _tool_receipt(tool_name: str, tool_call_id: str, content: str) -> dict:
    """"只回话、不改 state"的编排工具的返回切片。

    记忆读写与技能加载/卸载共用这个形状——它们都只产一条给模型看的 ToolMessage，
    不写任何 state 切片（对比计划三件套，它们要回 `current_plan`）。
    """
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
    - 随时能从工作区重取的 dump（代码内容、目录结构、文件清单）。

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
        return _tool_receipt(
            "write_memory", tool_call_id, "记忆内容为空，未写入。content 要写成一句有结论的事实。"
        )
    if not kind:
        return _tool_receipt(
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
            return _tool_receipt(
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
    return _tool_receipt(
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
        return _tool_receipt("read_memory", tool_call_id, "当前工作区还没有长期记忆。")

    entries = memory.parse_entries(text)
    target = (key or "").strip()
    if target:
        entry = next((e for e in entries if e.key == target), None)
        if entry is None:
            existing = "、".join(e.key for e in entries) or "（暂无）"
            return _tool_receipt(
                "read_memory", tool_call_id, f"记忆 {target} 不存在。现有编号：{existing}"
            )
        return _tool_receipt(
            "read_memory",
            tool_call_id,
            f"### [{entry.key}] {entry.type} · {entry.date}\n\n{entry.content}",
        )

    if not entries:
        return _tool_receipt(
            "read_memory", tool_call_id, "当前工作区还没有长期记忆条目（只有文件头说明）。"
        )
    visible = memory.strip_comments(text).strip()
    return _tool_receipt(
        "read_memory", tool_call_id, f"当前该工作区共 {len(entries)} 条长期记忆：\n\n{visible}"
    )


memory_tool: List[BaseTool] = [
    write_memory,
    read_memory,
]

# ------------------------- 技能（skill） -------------------------
# 按需加载的"领域包"（docs/SKILL_DESIGN.md）：**目录**（name + description）在构图期由
# skill_tools_for 烤进 get_skill 的 docstring，**正文**由 get_skill 只写进 state 的
# loaded_skills 名字清单、再由 LLMNode 每轮从盘上读进系统提示。
#
# ⚠️ **回执里绝不带正文**（§3.5 的铁律）：工具结果会进 message history、此后每轮随历史重发；
# 若正文既当 ToolResult 又注入系统提示，就是两份拷贝——而淘汰只能抹掉注入那份，"淘汰"就成了谎。
# 只存名字让这条**结构性成立**：正文只有系统提示一个家，卸载 = 删一个名字。
#
# get_skill 要读盘 / 起进程，故与记忆工具同样写成 async + asyncio.to_thread（避开 langgraph dev
# 的 blockbuster）；drop_skill 要 await 关运行体，也必须是 async（编排节点原生支持 async）。
#
# 两个工具**都注入 workspace**：能力型技能要按工作区造连接（server 的 cwd = 工作区、env 里带
# WORKSPACE_PATH），与 memory 那组同款（`InjectedWorkspace` 对模型隐藏）。
# 技能**源**仍在项目根（不随工作区变），变的只是"它被拉起来时待在哪个工作区里"。
#
# 能力型的完整链路（§13）：get_skill 读正文 → 校验（目录名/会话/env/工具登记）→ 起 server →
# 把工具加进本会话（mcp.register_server）→ 工具随下一次模型调用出现；drop_skill 反向移除。
# **加载失败一律拒绝加载**（不写 loaded_skills）：语义一致——"已加载"意味着正文与工具都到位。


def _skill_server_name(meta_or_name) -> str:
    """技能运行体在 mcp 注册表里的 server 名：`skills/<目录名>`（= tool.json 的 source）。"""
    dir_name = getattr(meta_or_name, "dir_name", meta_or_name)
    return f"{skills.SKILLS_PACKAGE}/{dir_name}"


# 加载时体检的超时（秒）。它是**附加信息**，不该让一次 get_skill 卡在网络上：超时按
# "没做成"报关，不阻断加载（理由见 _skill_preflight_line）。
_PREFLIGHT_TIMEOUT = 8


def _probe_skill_credential(url: str, token: str) -> tuple[bool, str]:
    """打一次只读端点验凭证，返回 (是否可用, 一行说明)。

    只看 HTTP 层：能拿到响应 = 可用；401/403 = 被拒；网络失败 = 连不上。**不解析正文**——
    "这个凭证行不行"是加载时该回答的全部；账号、配额那些是技能工具自己的事。
    """
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "User-Agent": "theta-agent"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_PREFLIGHT_TIMEOUT) as response:
            return True, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"被拒（HTTP {exc.code}：令牌无效/过期，或权限不足）"
    except urllib.error.URLError as exc:
        return False, f"连不上（{exc.reason}）"
    except OSError as exc:
        # urlopen 只在 connect/send 阶段把 OSError 包成 URLError；**读阶段**出的超时
        # （TimeoutError）、连接被重置（ConnectionResetError）等是裸 OSError，不接住就会
        # 逃出 get_skill——而那时 register_server 已经跑过，等于破坏了"体检失败不阻断加载"
        # 的契约（见 _skill_preflight_line）。URLError 是 OSError 子类，故这条必须排在它后面。
        return False, f"连不上（{exc}）"


async def _skill_preflight_line(meta: SkillMeta) -> str | None:
    """按技能声明的 `preflight` 打一次体检，返回**一行结论**；没声明 → None（回执里不出现）。

    为什么在 host 侧、而不是做成技能工具：凭证行不行是**加载那一刻就该知道**的配置事实。
    做成工具等于指望模型想到去调它（它并不知道该调），而配置问题被推到"第一次真调用才炸"
    正是要避免的那种发现方式；做在 host 侧还省下一条模型可见的工具表条目（2026-09-13 改：
    `github_auth_status` 这个工具就是被这条替掉的）。

    **失败不阻断加载**：技能的正文/纪律在拿不到远端时仍然有用（照它的规范写 PR 描述不需要
    网络），所以结论只如实报关，让模型自己决定还动不动手；真调工具时另有 upstream_error 兜底。
    """
    spec = await asyncio.to_thread(skills.read_preflight, meta)
    if spec is None:
        return None
    # 这里不再兜 skill_env 的 ConfigError / 空值：调用点只在 _register_skill_runtime 成功
    # 之后才走到本函数，而它已用**同一个 meta** 调过 skill_env（失败就提前回"凭证没配好，
    # 已拒绝加载"），且 read_preflight 保证 bearer_env 属于 read_skill_env(meta)——空值会
    # 在那一步抛 ConfigError。所以 env 里该键必有非空值。
    env = skill_env(await asyncio.to_thread(skills.read_skill_env, meta))
    token = env[spec.bearer_env]

    ok, detail = await asyncio.to_thread(_probe_skill_credential, spec.url, token)
    if ok:
        return f"凭证体检：{spec.bearer_env} 可用（{detail}）。"
    return (
        f"⚠️ 凭证体检失败：{spec.bearer_env} {detail}——本技能的工具调用会同样失败。"
        f"请把这条告知用户（多半是 .env 里的它过期或权限不够）；改好之后 drop_skill 再 "
        f"get_skill 会重新体检。"
    )


def _missing_requirements(meta: SkillMeta, workspace: str) -> list[str]:
    """技能声明的依赖里，当前环境**import 不到**的那些（按顶层包名）。

    为什么可信：技能依赖装在主 venv（§5.1 已决不做隔离），技能 server 又是用 `sys.executable`
    起的——**与 host 同一个解释器**，所以这里的结论就是子进程 import 时的结论。搜索路径照着子进程
    的拼（`-m` 的 `sys.path[0]` 是工作区，再加 `PYTHONPATH=项目根`，然后才是 host 自己的 sys.path），
    免得"工作区里恰好有一份同名包"这种边角被误判。

    `find_spec` **只定位、不执行模块代码**：零副作用、不起进程——这正是它比"起一次 import 探针"
    强的地方（后者会把技能的模块级代码多跑一遍）。`requirements` 里写 `a.b` 时只看顶层 `a`。
    """
    declared = skills.read_requirements(meta)
    if not declared:
        return []
    search = [workspace, str(_PROJECT_ROOT), *sys.path]
    missing: list[str] = []
    for name in declared:
        try:
            found = PathFinder().find_spec(name.split(".")[0], search) is not None
        except (ImportError, ValueError, AttributeError):
            found = False
        if not found:
            missing.append(name)
    return missing


# 启动失败时"重跑一次 import 抓原因"的超时（秒）与回执里 traceback 的截断长度。
# 这条只在**已经失败**之后跑，所以不给成功路径添任何开销。
_STARTUP_PROBE_TIMEOUT = 15
_STARTUP_PROBE_CHARS = 1200


def _flatten_exception(exc: BaseException) -> str:
    """把一个异常（可能是 ExceptionGroup）压成一行可读的类型 + 消息。

    MCP 适配器把"子进程没起来"报成 `ExceptionGroup(...) → McpError: Connection closed`——
    原样回给模型等于什么都没说。展平一层（最多取前 3 个）比那串判词有用。
    """
    subs = list(getattr(exc, "exceptions", ()) or ())[:3]
    if not subs:
        return f"{type(exc).__name__}: {exc}"
    inner = "、".join(f"{type(sub).__name__}: {sub}" for sub in subs)
    return f"{type(exc).__name__}（{inner}）"


def _probe_skill_startup(meta: SkillMeta, workspace: str, env: dict[str, str]) -> str | None:
    """重跑一次"能不能 import 这个 server"，把**子进程的 traceback** 带回来；问不出来 → None。

    为什么需要它：能力型技能起不来时（缺依赖、语法错、server 自己抛 ConfigError…），MCP 适配器
    只把 `ExceptionGroup → McpError: Connection closed` 交给 host——**真正的原因只打在子进程的
    stderr 上**，模型拿到的是个黑箱，而"把配置问题告知用户"恰恰要求说得出是什么问题。

    做法：用**与真启动完全同一套** command/cwd/env（直接取 `stdio_connection` 的产物，避免两处
    漂移）跑 `python -c "import <module>"`，收回 stderr 尾部的 traceback。所以它只在失败路径上
    多起一个进程——成功路径一次都不跑；而且 import 成功（returncode 0）时返回 None，因为那种
    "起不来"另有原因（协议/超时），不该拿一段无关的 traceback 误导模型。
    """
    connection = mcp.stdio_connection(skills.server_module(meta), workspace, env)
    command = [connection["command"], *connection["args"]]
    import_cmd = [
        command[0],  # 同一个解释器
        "-c",
        f"import {skills.server_module(meta)}",
    ]
    try:
        done = subprocess.run(
            import_cmd,
            cwd=connection["cwd"],
            env=connection["env"],
            capture_output=True,
            text=True,
            timeout=_STARTUP_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("技能 %s 的启动探针自身失败：%s", meta.name, exc)
        return None
    if done.returncode == 0:
        return None
    tail = (done.stderr or "").strip()[-_STARTUP_PROBE_CHARS:]
    return tail or None


async def _register_skill_runtime(
    meta: SkillMeta, workspace: str, session: str | None
) -> str | None:
    """把能力型技能的 server 加进本会话；成功 → None，失败 → 可行动的错误正文。

    顺序：**先把所有校验做完 → 再一次加入 → 逐条校验工具名 → 任一不满足就回滚**。
    """
    server = _skill_server_name(meta)

    if not session:
        return (
            f"技能 {meta.name} 是能力型（带工具），但当前会话没有会话标识"
            f"（state.session_id 为空），无法隔离它的运行体。知识型技能不受影响。"
        )
    if meta.name != meta.dir_name:
        return (
            f"技能 {meta.name} 带 server.py，但它的目录名 {meta.dir_name!r} 与技能名不一致——"
            f"能力型要求两者相同（启动模块与工具登记都按目录名走）。"
            f"请把 SKILL.md 的 name 改成目录名，或改目录名；改完**重开会话**再试"
            f"（技能的目录条目录入在会话启动时完成）。"
        )

    missing = await asyncio.to_thread(_missing_requirements, meta, workspace)
    if missing:
        return (
            f"技能 {meta.name} 声明的依赖当前环境里 import 不到：{missing}。"
            f"请把它们加进项目根的 pyproject.toml 后执行 uv sync（技能依赖装在主 venv，不做隔离），"
            f"然后**重新 get_skill 即可**，不必重启进程。"
            f"**这是技能配置问题、不是你的调用方式问题**——请把这条回执告知用户，不要自己绕道。"
        )

    try:
        env = skill_env(await asyncio.to_thread(skills.read_skill_env, meta))
    except ConfigError as exc:
        return (
            f"技能 {meta.name} 需要的凭证没配好，已拒绝加载：{exc}。"
            f"请在 .env 里补上（键还要落在 app/config.py::SKILL_ENV_WHITELIST 里），"
            f"改完**需要重启进程**（配置在启动时读入并缓存），再重新 get_skill。"
        )

    connection = mcp.stdio_connection(skills.server_module(meta), workspace, env)
    existing_names = mcp.session_tool_names(workspace, session) | set(_ORCHESTRATE_TOOL_NAMES)

    try:
        names = await mcp.register_server(workspace, session, server, connection)
    except Exception as exc:  # noqa: BLE001 —— 起不来就是加载失败，交给回执说清楚
        # 重跑一次 import 把子进程的真 traceback 拿回来（失败路径才付这点代价）：
        # 否则模型手里只有 ExceptionGroup → McpError，说不出"为什么起不来"
        traceback_tail = await asyncio.to_thread(
            _probe_skill_startup, meta, workspace, env
        )
        detail = traceback_tail or _flatten_exception(exc)
        return (
            f"技能 {meta.name} 的 server 起不来或没列出工具，已拒绝加载"
            f"（**装好依赖或改好代码之后，直接重新 get_skill 即可，不必重启进程**）。\n"
            f"**这是技能自身的问题（缺依赖 / 代码报错 / 配置不对），不是你的调用方式问题**——"
            f"请把下面这段告知用户，不要自己绕道：\n"
            f"```\n{detail}\n```"
        )

    problem = _check_skill_tools(meta, server, names, existing_names)
    if problem is not None:
        await mcp.unregister_server(workspace, session, server)
        return problem
    return None


def _check_skill_tools(
    meta: SkillMeta, server: str, names: list[str], existing_names: set[str]
) -> str | None:
    """校验技能带来的工具名（声明 / 重名 / 自重复）；有问题 → 可行动的错误正文。

    审批策略的声明处（2026-09-19 起）是**技能目录自己的 skill.json**（`tools` 键）——约束跟
    能力走，加技能不再需要动中心表。fail-closed 强度不变：server 暴露的**每一个**工具都必须
    逐条声明 `need_review`，缺一个就拒载（否则"未声明 = 免审"会静默放行一次没人看过的动作）。
    另外两向也拦：声明形状不对（need_review 非布尔）与声明了 server 没暴露的工具（拼写错误
    的静默形态——声明者以为有政策兜着，实际那工具根本不存在）。
    读盘不缓存：改完 skill.json **重新 get_skill 即可**，不必重启进程。
    """
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        return f"技能 {meta.name} 暴露了重名工具 {duplicated}，已拒绝加载。"

    clash = sorted(set(names) & existing_names)
    if clash:
        return (
            f"技能 {meta.name} 的工具与已有工具重名：{clash}，已拒绝加载。"
            f"请改名后重新 get_skill（同名会让模型与审批策略都无法区分两者）。"
        )

    declared = skills.read_tool_policies(meta)
    undeclared = [n for n in names if n not in declared]
    if undeclared:
        return (
            f"技能 {meta.name} 的工具没有在它自己的 skill.json 里声明审批策略：{undeclared}。"
            f'请在技能目录的 skill.json 加 "tools" 段，为每个工具声明 {{"need_review": true/false}}，'
            f"改完**重新 get_skill 即可**（技能配置每次加载时读取，不必重启进程）。"
            f"**这是技能配置问题、不是你的调用方式问题**——请把这条回执告知用户，"
            f"不要自己绕道（例如改用 read_file 去读技能目录）。"
        )

    bad_shape = sorted(
        n
        for n, conf in declared.items()
        if not isinstance(conf, dict) or not isinstance(conf.get("need_review"), bool)
    )
    if bad_shape:
        return (
            f"技能 {meta.name} 的 skill.json 里 {bad_shape} 的 need_review 声明形状不对"
            f"（必须是 {{\"need_review\": true/false}}），已拒绝加载。"
            f"改好**重新 get_skill 即可**。"
            f"**这是技能配置问题、不是你的调用方式问题**——请把这条回执告知用户。"
        )

    unexposed = sorted(n for n in declared if n not in set(names))
    if unexposed:
        return (
            f"技能 {meta.name} 的 skill.json 声明了 server 没有暴露的工具：{unexposed}，"
            f"已拒绝加载（多半是拼写错误——声明了政策却不存在那个工具，等于政策落空）。"
            f"改好**重新 get_skill 即可**。"
            f"**这是技能配置问题、不是你的调用方式问题**——请把这条回执告知用户。"
        )
    return None


@tool
async def get_skill(
    name: str,
    state: Annotated[AgentState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    workspace: Annotated[str, InjectedWorkspace],
) -> dict:
    """加载一个技能：把它的正文注入系统提示，能力型还会把它的工具加进本会话。

    何时用：任务落进某个已安装技能覆盖的领域（如 GitHub 平台操作），而你需要那个领域的
    做法、约束或能力清单时——**先看本工具说明末尾的"可用技能"目录**，判断相关再加载。
    标 [带工具] 的技能加载后会多出一批工具（下一次模型调用就能看到）；技能一旦加载就在本会话
    常驻（每轮都在系统提示里），所以**不相关的别加**；不需要了用 drop_skill 卸下、把预算还回来。

    Args:
        name: 技能名，取自本工具说明里的"可用技能"目录（如 "github"）。
        state: （系统自动注入，无需传入）当前 AgentState，读 loaded_skills。
        tool_call_id: （系统自动注入，无需传入）本次调用 id。
        workspace: （系统自动注入，无需传入）当前工作区，能力型技能的 server 在它里面运行。

    Returns:
        成功：回执（正文已进系统提示；能力型另注明工具数）。名字已在清单里、但它的运行体不在
        （对账重建失败被退避）时，这里会**重新拉起**并如实说明——那是本会话唯一的重试入口。
        失败：技能名不存在（附可用清单）/ 已在加载中且运行体齐备 / 正文预算已满（附已加载
        清单，要求先卸一个）/ 能力型加载失败（缺凭证、目录名与技能名不一致、工具未登记或
        重名、server 起不来）——**失败即拒绝加载**，正文与工具都不会生效。
    """
    wanted = (name or "").strip()
    loaded = list(state.get("loaded_skills") or [])

    metas = await asyncio.to_thread(skills.scan_skills)
    meta = metas.get(wanted)
    if meta is None:
        names = "、".join(sorted(metas)) or "（无）"
        return _tool_receipt(
            "get_skill", tool_call_id, f"技能 {wanted!r} 不存在。可用技能：{names}"
        )

    if wanted in loaded:
        # 例外：名字在 state 里、运行体却不在（对账重建失败被退避、或对账本身抛了异常）。
        # 这时"已加载"是**假的**——正文在、工具没有，模型照 SKILL.md 去调工具只会撞
        # unknown_tool；而那条回执恰恰让模型来这里。所以这里给它一条**真的重试路径**，
        # 否则失败退避（见 `_register_failed`）会把"每轮自动重试"换成"整会话静默死路"。
        if meta.capability and _skill_server_name(meta) not in mcp.registered_servers(
            workspace, state.get("session_id")
        ):
            problem = await _register_skill_runtime(meta, workspace, state.get("session_id"))
            if problem is not None:
                return _tool_receipt("get_skill", tool_call_id, problem)
            return _tool_receipt(
                "get_skill",
                tool_call_id,
                f"技能 {wanted} 已在本会话加载中，但它此前缺失的工具运行体不在——"
                f"已重新拉起，下次模型调用即可调用。",
            )
        return _tool_receipt(
            "get_skill", tool_call_id, f"技能 {wanted} 已在本会话加载中，无需重复加载。"
        )

    body = await asyncio.to_thread(skills.body_of, meta)
    if body is None:
        return _tool_receipt(
            "get_skill", tool_call_id, f"技能 {wanted} 的正文读取失败（SKILL.md 读不到），未加载。"
        )

    # 预算满 → **拒绝**，并要求模型先卸一个（§3.5）。不静默淘汰：知识型技能没有可观测的
    # "使用事件"（正文被动注入、模型每轮都看得到），"最久未用"根本无从定义。
    used = await asyncio.to_thread(skills.bodies_size, loaded)
    if used + len(body) > skills.SKILL_BODY_BUDGET:
        current = "、".join(loaded) or "（无）"
        return _tool_receipt(
            "get_skill",
            tool_call_id,
            f"技能正文预算已满（上限 {skills.SKILL_BODY_BUDGET} 字符，当前已用 {used}），"
            f"无法加载 {wanted}。当前已加载：{current}。"
            f"请先用 drop_skill 卸下一个不再需要的技能，再重试。",
        )

    if meta.capability:
        # 能力型：先把它拉起来再加名字——**失败即拒绝加载**（不写 loaded_skills），
        # 否则模型会以为工具可用，照着 SKILL.md 里的工具名去调、全部撞 unknown_tool。
        problem = await _register_skill_runtime(meta, workspace, state.get("session_id"))
        if problem is not None:
            return _tool_receipt("get_skill", tool_call_id, problem)

    loaded.append(wanted)
    content = f"已加载技能 {wanted}，其正文已进系统提示（下一次模型调用即可见）。"
    if meta.capability:
        content += "该技能带的工具也已加入本会话，下次模型调用即可调用。"
        # 加载期体检（技能在 skill.json 里声明了 preflight 才有）：结论随回执给模型。
        # 只在这条"真的加载了"的路径上打——幂等那条（已加载，无需重复）零成本，不探测。
        note = await _skill_preflight_line(meta)
        if note:
            content += f"\n{note}"
    content += f"\n当前已加载：{'、'.join(loaded)}"
    return {
        "loaded_skills": loaded,
        "messages": [
            ToolMessage(name="get_skill", tool_call_id=tool_call_id, content=content)
        ],
    }


@tool
async def drop_skill(
    name: str,
    state: Annotated[AgentState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    workspace: Annotated[str, InjectedWorkspace],
) -> dict:
    """卸下一个已加载的技能：正文移出系统提示，（能力型）它的工具也从本会话移除。

    何时用：某领域的活干完了；或者 get_skill 报"预算已满"，你需要腾出位置加载别的技能。
    卸下后该技能的正文不再进系统提示，它的纪律与做法也就不再对你有约束力——需要时重新加载。

    Args:
        name: 要卸下的技能名（当前已加载清单里的名字）。
        state: （系统自动注入，无需传入）当前 AgentState，读 loaded_skills。
        tool_call_id: （系统自动注入，无需传入）本次调用 id。
        workspace: （系统自动注入，无需传入）当前工作区，用于关掉该技能在本会话的运行体。

    Returns:
        回执 + 卸下后的已加载清单；名字本就不在清单里则说明情况。
    """
    wanted = (name or "").strip()
    loaded = list(state.get("loaded_skills") or [])

    if wanted not in loaded:
        current = "、".join(loaded) or "（无）"
        return _tool_receipt(
            "drop_skill",
            tool_call_id,
            f"技能 {wanted!r} 不在已加载清单里，无需卸下。当前已加载：{current}",
        )

    session = state.get("session_id")
    server = _skill_server_name(wanted)
    released = False
    if server in mcp.registered_servers(workspace, session):
        # 能力型：把运行体一起关掉（§3.3 第二坑——拆 server 必须同时注销它的工具）。
        # 名字不在注册表里（知识型，或运行体本来就没起来）就什么都不用做。
        await mcp.unregister_server(workspace, session, server)
        released = True

    loaded.remove(wanted)
    current = "、".join(loaded) or "（无）"
    content = f"已卸下技能 {wanted}，其正文已从系统提示移除。"
    if released:
        content += "它的工具也已从本会话移除。"
    content += f"当前已加载：{current}"
    return {
        "loaded_skills": loaded,
        "messages": [
            ToolMessage(name="drop_skill", tool_call_id=tool_call_id, content=content)
        ],
    }


skill_tool: List[BaseTool] = [
    get_skill,
    drop_skill,
]

# 编排工具（不走 tool.json 审批、也不在 MCP 工具表里）的名字集合——技能工具**不许与它们重名**：
# 同名会让模型的两份 schema 指向同一个名字，而 tool.json 放不下两条同键登记。
_ORCHESTRATE_TOOL_NAMES: frozenset[str] = frozenset(
    t.name
    for t in (orchestrate_tool + note_tools + ask_tool + memory_tool + skill_tool)
)


class SessionToolset:
    """"某会话当前可见的工具集"的接线点：LLMNode / ToolNode / ReviewNode 三家共用一份。

    为什么落在 tools.py 而不是 mcp.py：它除了取工具，还要做**技能对账**——而"哪些名字是技能、
    是不是能力型"是这边的知识（mcp.py 只认识 server，不认识技能这个概念）。

    与 §13.2 原稿的差别：不另造"注册表对象"，运行时状态（谁注册了哪个 server、版本几）仍按
    (工作区, 会话) 落在 mcp.py——与 §12 已落地的运行体注册表同源，避免两套平行状态各走各的。
    """

    def __init__(self, workspace: str, static_tools: list[BaseTool] | None = None) -> None:
        self.workspace = workspace
        # 静态工具（编排 / 笔记 / 派发 / 记忆 / 技能三件套）：它们不进 MCP 注册表，但**模型必须
        # 一直看得到**——动态绑定若只给 MCP 工具，连 get_skill 自己都会从模型眼前消失。
        self._static_tools = list(static_tools or [])
        # 已经查实"不该有运行体"的技能名（知识型）。**对账快路径靠它**：`registered` 只含能力型，
        # 而 `loaded` 含两型名字，光比两者**永远不会相等**（知识型永远只在 loaded 一侧），于是
        # 每轮都要扫盘才能重新确认"差集里的都是知识型"。记下这些名字就回到纯内存比较。
        # 能力型与否由**盘上有没有 server.py** 决定（与会话无关），所以这份备忘不需要分会话；
        # 它按实例存活（一个图/会话一个），技能源类型变更属"重开会话生效"级别的事件，无需失效钩子。
        self._knowledge_only: set[str] = set()
        # 已经查实"**运行体重建不起来**"的 (会话, 技能名)（server 起不来 / 工具没在 tool.json
        # 登记 / 源已损坏）。**失败必须有退避**，否则这个名字会一直留在 `missing` 里：每轮
        # （LLMNode + ToolNode 各一次）重新校验 + 重列 schema——而重列要真起一个临时会话
        # （回滚路径已经把 schema 缓存丢了），于是一个坏技能会让该会话**每轮多起两个子进程**，
        # 且日志每轮响两遍。记下来即回到纯内存比较。
        #
        # ⚠️ **必须连会话一起记**，不能像 `_knowledge_only` 那样只按名字：失败原因里有两条是
        # 会话相关的——"当前会话没有会话标识"与"与**本会话**已有工具重名"——而同一个实例会服务
        # 多个会话（langgraph dev 一图多 thread，见 LLMNode._model_for 的注释）。只按名字记的话，
        # A 会话的失败会让 B 会话整轮不再尝试重建，B 只表现为"正文在、工具没有"。
        # （`_knowledge_only` 只按名字是安全的：能力型与否由盘上有没有 server.py 决定，与会话无关。）
        # 失效靠重开会话（新图 = 新实例）；模型若 drop_skill 后再 get_skill 也会**真的重试**
        # ——`get_skill` 不查这份备忘，且失败时给的是可行动回执，比这里静默跳过有用得多。
        self._register_failed: set[tuple[str | None, str]] = set()

    async def tools_and_version(self, state: AgentState) -> tuple[list[BaseTool], int]:
        """给 LLMNode：当前工具表（静态 + MCP）+ 版本（版本变了才需要重新 `bind_tools`）。"""
        session = state.get("session_id")
        try:
            await self._reconcile(state, session)
        except Exception as exc:  # noqa: BLE001 —— 对账失败不许炸整轮（§13.3）
            logger.warning("技能对账失败，本轮沿用现状：%s", exc)
        return (
            [*self._static_tools, *mcp.session_tools(self.workspace, session)],
            mcp.tools_version(self.workspace, session),
        )

    async def tools_map(self, state: AgentState) -> dict[str, BaseTool]:
        """给 ToolNode：按名查表。**每轮重解析**——技能可能在本回合的编排阶段刚被卸载。

        只含 MCP 工具：编排工具走 OrchestrateNode，不经这里执行（若把静态工具也塞进来，
        一个错误分流的编排调用会在这里被当成普通工具执行、撞上缺失的注入参数）。
        """
        session = state.get("session_id")
        try:
            await self._reconcile(state, session)
        except Exception as exc:  # noqa: BLE001
            logger.warning("技能对账失败，本轮沿用现状：%s", exc)
        return {tool.name: tool for tool in mcp.session_tools(self.workspace, session)}

    def known_names(self, state: AgentState) -> set[str]:
        """给 ReviewNode：当前工具表里的名字集合（含编排工具）。同步、纯内存。"""
        session = state.get("session_id")
        # `_static_tools` 生产恒为构图期传入的那张表（graph.py），恒非空——不设空表兜底：
        # 真出现空表是接线错了，兜底只会把 bug 藏起来（`known_names` 少名字 → 编排调用被判成
        # "未知工具"，症状比这里直接暴露更难查）。
        static = {tool.name for tool in self._static_tools}
        return mcp.session_tool_names(self.workspace, session) | static

    async def _reconcile(self, state: AgentState, session: str | None) -> None:
        """让"运行体"与 state 的 `loaded_skills` 对齐。

        **快乐路径 = 一次内存集合比较**（两个方向都一致就直接返回，不读盘、不起进程）——它每轮
        都会被调。只在真不一致时才动手：

        - 运行体在、state 里没有 → 注销。防的是"工具在、纪律不在"：注册成功但 state 没落
          （回执写回前回合被取消），模型手里就会有工具、却没读过那个领域的约束；
        - state 里有、运行体没有（进程重启、切会话转回来）→ 只重建**能力型**的那个。

        ⚠️ 这里有两个容易踩的点：

        - 只有"只加不减"是不够的：两个方向都要对。**反向对账之所以安全，前提是运行体按
          会话分片**（本设计如此）——若键退化成全局，双向对账会变成两个会话互相拆台；
        - `registered` 只含能力型，`loaded` 含两型名字，**两边直接比较永远不会相等**。所以
          "该建"的那一侧要减去两份备忘录——`_knowledge_only`（查实过的知识型）与
          `_register_failed`（查实过建不起来的）——否则它们会让快路径失效：前者每轮重扫一遍
          技能源的全部 SKILL.md，后者更贵，每轮重列 schema（真起临时会话）。
        """
        loaded = {n.strip() for n in (state.get("loaded_skills") or []) if n and n.strip()}
        prefix = f"{skills.SKILLS_PACKAGE}/"
        registered = {
            server.removeprefix(prefix)
            for server in mcp.registered_servers(self.workspace, session)
            if server.startswith(prefix)
        }
        # 备忘按会话过滤：本会话查实过建不起来的才跳过，别的会话的失败与这里无关
        failed_here = {name for sess, name in self._register_failed if sess == session}
        missing = loaded - registered - self._knowledge_only - failed_here
        if not missing and not registered - loaded:
            return

        for extra in sorted(registered - loaded):
            await mcp.unregister_server(self.workspace, session, f"{prefix}{extra}")

        for name in sorted(missing):
            meta = await asyncio.to_thread(skills.get_meta, name)
            if meta is None:
                continue  # 源被删：正文侧由 skills_block 的告警提示模型卸下
            if not meta.capability:
                # 查实是知识型 → 记下来，往后这个技能不再触发任何读盘
                self._knowledge_only.add(name)
                continue
            problem = await _register_skill_runtime(meta, self.workspace, session)
            if problem is not None:
                # 记下失败就不再逐轮重试（见 `_register_failed` 上的说明）。正文侧由 skills_block
                # 正常注入——模型若真去调那个不存在的工具，会拿到 unknown_tool 回执并被引向
                # get_skill 重载，那时它会拿到这条失败的**可行动原因**，比这里静默跳过有用。
                self._register_failed.add((session, name))
                logger.warning(
                    "技能 %s 的运行体重建失败（本会话不再重试，重开会话可重来）：%s", name, problem
                )


def skill_tools_for() -> list[BaseTool]:
    """构图期把技能目录烤进 get_skill 的 docstring（§3.2），返回本图要用的技能工具。

    目录是**唯一的发现通道**：它随工具 schema 每轮都在模型眼前，模型据此判断该不该加载。
    没有它，模型不知道存在任何技能，`get_skill` 就永远不可达。**正文不进目录**——目录每轮
    付费，塞正文等于每轮全量付费，正是渐进披露要避免的（§3.6 的两个预算）。

    用 `model_copy` 复制而非原地改 description：模块级的 `get_skill` 是共享对象，直接改会
    污染同进程里的其它图（评估与测试各建各的图）。
    """
    catalog = skills.catalog_block()
    if not catalog:
        return list(skill_tool)
    return [
        get_skill.model_copy(update={"description": f"{get_skill.description}\n\n{catalog}"}),
        drop_skill,
    ]
