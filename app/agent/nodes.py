import asyncio
import inspect
import json
import logging
from inspect import signature
from pathlib import Path
from typing import Callable

from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.types import interrupt
from app.agent import gates
from app.exception import ConfigError
from app.resource import event_log, images, profile
from app.resource.memory import memory_block
from app.resource.skills import get_meta, read_tool_policies, skills_block
from app.agent.state import AgentState
from app.platform.tool_results import coerce_tool_result, format_tool_result
from app.agent.prompt import (
    RUNTIME_STATE_CLOSE,
    RUNTIME_STATE_OPEN,
    RUNTIME_STATE_SEPARATOR,
    SYSTEM_PROMPT,
    handoff_pointer_block,
    workspace_context_block,
)
from app.config import THINKING_MODES, get_settings
from app.schema.agent_schema import ImageRef, PlanStep
from app.schema.approval_schema import GATE_ASK_USER, GATE_TOOL_APPROVAL
from app.agent.compression import policy as compression_policy
from app.agent.compression.compact import plan_compact
from app.agent.compression.parser import one_line
from app.agent.compression.compress import (
    MAX_BATCH_CHARS,
    MASK_MIN_CHARS,
    MIN_BATCH_CHARS,
    READ_DEDUP_MIN_CHARS,
    TURN_HIGH_WATERMARK_CHARS,
    TURN_KEEP_BLOCKS,
    TURN_LOW_WATERMARK_CHARS,
    TurnCompressor,
)

# 顶部读一次 settings 是**行为契约**，不是随手一行：它让"缺 .env"在 import 期就响亮报错，而不是
# 拖到第一次真去建模型时才炸（`app/agent/model.py` 于 2026-09-21 并入本模块，那行随之下移到这里；
# 并入时**刻意保留**该时机，见 docs/DONE.md「节点过度实现」条）。懒加载（+ 入口把 ValidationError 翻成
# ConfigError）是 docs/EXCEPTION_DESIGN.md 里另一条**尚未落地**的待办，落地时连同这一行一起改。
settings = get_settings()
logger = logging.getLogger(__name__)


#----------------------工具调用日志（审核节点逐条输出）----------------------

def _tool_call_log(tool_call: dict) -> str:
    """把一条待审工具调用渲染成一行日志（工具名 + 紧凑参数）。"""
    name = tool_call.get("name") or "?"
    args = tool_call.get("args") or {}
    try:
        brief = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except TypeError:
        brief = str(args)
    return f"→ 工具调用: {name}  {one_line(brief)}"

#-------------------大模型节点----------------------

# 思考模式：`CHAT_THINKING` 的三态（app/config.py）到此映射成 provider 参数。**换 provider 只改
# 这一个静态方法**——厂商各叫各的（智谱/GLM 是 `thinking.type`；部分平台 `enable_thinking`；OpenAI
# 自家 `reasoning_effort`；DeepSeek 干脆靠换 model 名），把参数散到别处等于把厂商知识灌进核心节点。
#
# 三件记在案的事（都实测过，见 tests/test_thinking_config.py）：
# - **不配 `clear_thinking`**：服务端默认（`true`）就是"历史轮次的思考不随上下文给模型"，正是我们
#   要的（思考不占历史）。要"跨轮保留思考"是另一件大事（要求把思考逐字回传，与不占历史冲突）。
# - **思考 token 计入 `completion_tokens`，因此也计入 `max_tokens`**：实测 `enabled` + 偏小的
#   `max_tokens` 会让思考吃光预算、正文变空（300 时就是这样）。本仓库不设 `max_tokens`，故安全；
#   哪天要设，必须把思考的额度算进去。
# - **思考内容进不了 `messages`**：`langchain-openai` 有意不提取 `reasoning_content`（转换函数是
#   白名单式的），所以我们不需要剥离代码——但这条**依赖库的行为**，由上面那个测试文件里的契约
#   用例钉住：哪天库开始提取，用例会红，届时在 LLMNode 的写回点补剥离。



class LLMNode:
    def __init__(
        self,
        model: Runnable,
        workspace_path: str | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        inject_session_context: bool = True,
        attach_images: bool = True,
        toolset=None,
        mailbox=None,
    ) -> None:
        # `model` 是**未绑定**的原始模型：给了 toolset 就由本节点按工具集版本调 bind_tools
        # （技能加载/卸载会改工具集）。没给 toolset（worker、单测）就用原样传进来的 model。
        self.model: Runnable = model
        self.workspace_path: str | None = workspace_path
        self.system_prompt: str = system_prompt
        # 会话级上下文 = 项目画像（AGENT.md）+ 长期记忆：都是"主 agent 认得这个项目/用户"所需；
        # worker（子 agent）传 False——它是临时资料收集器，两样都不注入（§4 隔离）。
        self.inject_session_context: bool = inject_session_context
        # @图片 的调用期附图通道（attach_images_to_payload）。worker 同样传 False：worker 的
        # HumanMessage 是**主模型生成的 dispatch prompt**，不认 @，杜绝"模型借 worker 附带
        # 本地图片"的旁路（能力白名单同一口径：结构性排除，不是靠提示词自觉）。
        self.attach_images: bool = attach_images
        # 项目画像实例内 memo：会话第一次启动时读入，之后复用（§4；画像慢变）
        self._profile_block: str | None = None
        # 动态工具集（app/agent/tools.py::SessionToolset）：None = 静态绑定，行为与改造前一致
        self.toolset = toolset
        # 运行中转向信箱（app/platform/runtime.py::UserMailbox）：**基座持有内容**，本节点只在
        # 入口 peek 布尔信号。None = 无转向能力（worker / 评估 / 未接线的调用方全部不变）——
        # worker 结构性没有用户，恒传 None；不设标记就永不触发。
        self.mailbox = mailbox
        # ⚠️ 当前 binding 与"未绑定 model"**必须分开存**：`RunnableBinding` 上**没有** bind_tools
        # （那只定义在 BaseChatModel 上），若把 binding 覆盖回 self.model，第二次版本变化就会
        # AttributeError: 'RunnableBinding' object has no attribute 'bind_tools'。
        self._bound: Runnable | None = None
        self._bound_key: tuple | None = None

    #-------------------模型构造（原 app/agent/model.py，2026-09-21 并入）-------------------
    # 并入理由（docs/DONE.md「节点过度实现」条）：模型是"这一拍要发给谁"，属本节点；独立成模块
    # 只多一层间接。**留作静态方法而非内联**——沿用 profile.py 并入时的先例：测试仍可直接调用它，
    # 不必构造 LLMNode 实例（构造要 workspace、toolset、mailbox）。
    # ⚠️ **`model` 参量必须保持可注入**：evaluation 的层 A 回放与一批单测靠
    # `get_main_agent_graph(model=…)` 塞替身模型——这里是"默认构造跟着本节点走"，不是让
    # `__init__` 自己造模型。

    @staticmethod
    def thinking_extra_body(mode: str) -> dict | None:
        """把 `CHAT_THINKING` 翻成 `ChatOpenAI` 的 `extra_body`；**不下发（留空）→ `None`**。

        用 `None` 而不是空 dict 表示"不下发"，是沿用库自己的约定：`_default_params` 里的
        `exclude_if_none` 会把值为 `None` 的键**整个丢掉**（`langchain_openai/chat_models/base.py:1266-1292`），
        所以 `extra_body=None` 与"根本没有这个参数"在请求体上等价——而空 dict 会以 `{}` 发出去。

        取值写错**当场抛** `ConfigError`，不静默按"没配"处理：静默的后果是"用户以为开了思考、
        实际没开"，而这种偏差在行为上几乎不可观测。
        """
        text = (mode or "").strip().lower()
        if not text:
            return None
        if text not in THINKING_MODES:  # 取值域与 CHAT_THINKING 键同处声明（app/config.py）
            raise ConfigError(
                f"CHAT_THINKING 只能是 {' / '.join(THINKING_MODES)}（或留空 = 不下发），收到的是 {mode!r}"
            )
        return {"thinking": {"type": text}}

    @staticmethod
    def main_chat_model():
        """按 settings 构造主对话模型（生产路径的默认模型）。"""
        return init_chat_model(
            model=settings.CHAT_MODEL_NAME,
            base_url=settings.CHAT_MODEL_URL,
            api_key=settings.CHAT_MODEL_API_KEY,
            model_provider="openai",
            # 思考模式：`None` = 不下发该参数（见 thinking_extra_body）。`extra_body` 是 ChatOpenAI
            # 的一等字段（透传给 SDK 的 `extra_body=`），不是"未知 kwargs"——不会走 model_kwargs
            # 那条带警告的路，字面与行为一致。
            extra_body=LLMNode.thinking_extra_body(settings.CHAT_THINKING),
        )

    @staticmethod
    def format_plan_status(step: PlanStep) -> str:
        """把计划某步渲染成易读字符串（给模型的状态回显）。只读不改 state。"""
        status_map = {
            "pending": "待处理",
            "in_progress": "进行中",
            "done": "已完成",
        }
        if not isinstance(step, dict):
            raise ValueError("step 必须是 PlanStep 类型")

        status_value = step.get("status")
        if not isinstance(status_value, str):
            raise ValueError("step['status'] 必须是字符串")

        task = step.get("task")
        task_str = task if isinstance(task, str) else "未知状态"
        status_id = step.get("id")
        status_id_str = status_id if isinstance(status_id, str) else "未知状态"

        state_label = status_map.get(status_value, "错误")
        return f"{status_id_str}. {task_str} 当前状态为 {state_label}"

    async def __call__(self, state: AgentState) -> dict | AgentState:
        """大模型处理节点。

        **稳定前缀**只拼：行为契约 `SYSTEM_PROMPT`（见 prompt.py）+ 工作区上下文
        `workspace_context_block`（含根目录与终端 cwd 差异提醒）+ 项目画像（工作区根
        `AGENT.md`，会话首启读入、实例内 memo）。

        **动态状态放尾部**（2026-10-20，prefix cache）：当前计划回显、长期记忆、交接指针、
        已加载技能正文由 `_runtime_state_block` 渲染成 `<RUNTIME_STATE>` 块，内联到当前
        user message 的末尾；有稳定 id 的 HumanMessage 会原样写回 state（`add_messages`
        按 id 原位替换），这样下一次请求看到的它是同一份内容，不会因为“这次内联、下次消失”
        把上一轮整段打出缓存。轮内重复调用按标记替换，不会重复追加。

        记忆与技能正文**每轮实时读盘**（文件即真值）；画像**只读一次**（画像慢变）。三处读盘
        都是阻塞调用，走 to_thread 以避开 langgraph dev 的 blockbuster。

        用户消息里的 @图片路径走**调用期附图通道**（attach_images_to_payload）：base64 只临时
        进请求体副本，state 里的消息永远是带 @路径 的纯文本。RUNTIME_STATE 只改文本，不改图块。

        每次模型调用后打一条 `llm_usage` INFO 日志（input/output/cache_read/cache_hit/
        cache_miss），供 Phase 1 实测 prefix cache 收益；不改协议、不写 state。
        """
        # 运行中转向（docs/TODO.md「运行中转向」）：信箱有货 = 用户在 turn 运行中发来新输入。
        # 本拍**整体跳过**——不拼系统提示、不读画像/记忆/技能（两次 to_thread 读盘也不花）、
        # 不调模型、不加任何消息——返回标记后，既有 route_after_llm 看到"末条非
        # AIMessage(tool_calls)"，沿既有 compact/END 把当前轮提前收口；基座随后把信箱里的行
        # 当**下一轮输入**新起 turn（内容全程不进图）。刻意放在函数第一条语句：收口拍的唯一
        # 成本就是这一次 peek；也刻意**跳过而非在出口拦截**——出口收口会让模型本拍刚吐的
        # tool_calls 悬空（无兑现 ToolMessage，下一轮 API 400），入口跳过天然没有这个问题。
        if self.mailbox is not None and self.mailbox.has_pending():
            return {"is_inject": True}

        system_messages = [SystemMessage(content=self.system_prompt)]
        if self.workspace_path:
            system_messages.append(
                SystemMessage(content=workspace_context_block(self.workspace_path))
            )
        # 画像慢变、实例内 memo：放稳定前缀；记忆/交接/技能是动态状态，走尾部 RUNTIME_STATE。
        if self.inject_session_context and self.workspace_path:
            profile_text = await self._read_profile()
            if profile_text:
                system_messages.append(SystemMessage(content=profile_text))

        runtime_block = await self._runtime_state_block(state)
        state_messages, updated_human = self._apply_runtime_state(
            list(state["messages"]), runtime_block
        )
        messages = [*system_messages, *state_messages]
        # @图片 的调用期附图：解析/读盘/转码在资源层（app/resource/images.py），本节点只决定
        # "这一拍要不要附图、把记账写回哪"。改的是**请求体副本**，state["messages"] 始终是纯文本
        # （@路径原样留在历史里，本身就是"看过哪张图"的日志）。新附上的图记账写回 state，下一轮
        # 据此不再重发（首次-only）。worker 已在构造期关掉这条通道。
        newly_attached: list[ImageRef] = []
        if self.attach_images and self.workspace_path:
            messages, newly_attached = await images.attach_images_to_payload(
                messages, state.get("attached_images") or [], self.workspace_path
            )
        model = await self._model_for(state)
        res = await model.ainvoke(messages)
        LLMNode._log_usage(res, state)
        # 原样写回：**思考内容不会进 history，但这不是因为我们剥了它**——`langchain-openai` 的
        # 响应转换是白名单式的，`reasoning_content` 压根没被提取进 AIMessage（实测见
        # tests/test_thinking_config.py 的契约用例）。所以这里没有剥离代码是**有意的**：那段代码
        # 今天空转。哪天库改成会提取它，那个用例会红——届时在这里（唯一写回点）补一手剥离，
        # 并顺带决定要不要给人看。
        out_messages: list = []
        # 只在 Human 有稳定 id 时写回：没有 id（直接驱动单测）写回只会被 reducer 当成新消息。
        if updated_human is not None and getattr(updated_human, "id", None):
            out_messages.append(updated_human)
        out_messages.append(res)
        out = {"messages": out_messages}
        if newly_attached:
            out["attached_images"] = [*(state.get("attached_images") or []), *newly_attached]
        return out

    async def _runtime_state_block(self, state: AgentState) -> str:
        """渲染动态运行时状态块（计划 / 记忆 / 交接指针 / 技能正文）；没有内容返回 ""。

        只拼**动态**部分：项目画像慢变，仍在稳定前缀。技能块**独立于
        `inject_session_context`**（worker 没有 get_skill，loaded_skills 天然为空），
        与改造前一致。
        """
        parts: list[str] = []
        plans = state.get("current_plan", [])
        if plans:
            parts.append(
                "# 当前任务:\n"
                + "\n".join(self.format_plan_status(plan) for plan in plans)
            )
        if self.inject_session_context and self.workspace_path:
            block = await asyncio.to_thread(memory_block, self.workspace_path)
            if block:
                parts.append(block)
            # 交接材料指针（project-handoff 技能的"接续"入口）：只 stat 存在性、不读正文，
            # 正文由模型按需 read_file。新会话靠这一行知道"有交接可接"。
            pointer = await asyncio.to_thread(handoff_pointer_block, self.workspace_path)
            if pointer:
                parts.append(pointer)
        loaded_skills = state.get("loaded_skills") or []
        if loaded_skills:
            skill_text = await asyncio.to_thread(skills_block, loaded_skills)
            if skill_text:
                parts.append(skill_text)

        if not parts:
            return ""
        return (
            RUNTIME_STATE_OPEN
            + "\n（宿主注入的运行时状态，不是用户的新需求。）\n\n"
            + "\n\n".join(parts)
            + "\n"
            + RUNTIME_STATE_CLOSE
        )

    def _apply_runtime_state(
        self, messages: list, block: str
    ) -> tuple[list, HumanMessage | None]:
        """把 RUNTIME_STATE 内联到最后一条 HumanMessage 尾部。

        返回 `(请求消息列表, 更新后的 HumanMessage | None)`。更新后的 Human 若带 id，调用方
        会把它写回 state；`add_messages` 按 id 原位替换。旧块按标记定位替换；`block` 为空时
        把旧块剥掉（计划清空 / 技能卸下）。无 Human 或 content 不是 str（多模态）时退回
        尾部 SystemMessage（备选路径），不写 state。
        """
        index = next(
            (
                i
                for i in range(len(messages) - 1, -1, -1)
                if isinstance(messages[i], HumanMessage)
            ),
            None,
        )
        if index is None:
            if block:
                return [*messages, SystemMessage(content=block)], None
            return list(messages), None

        human = messages[index]
        content = human.content
        if not isinstance(content, str):
            if block:
                return [*messages, SystemMessage(content=block)], None
            return list(messages), None

        base = LLMNode._strip_runtime_state(content)
        new_content = base
        if block:
            separator = RUNTIME_STATE_SEPARATOR if base else ""
            new_content = base + separator + block
        if new_content == content:
            return list(messages), None

        updated = human.model_copy(update={"content": new_content})
        return [*messages[:index], updated, *messages[index + 1 :]], updated

    @staticmethod
    def _strip_runtime_state(content: str) -> str:
        """剥掉旧的 RUNTIME_STATE 块（含前置分隔线），返回用户原文。"""
        start = content.rfind(RUNTIME_STATE_OPEN)
        if start < 0:
            return content
        end = content.find(RUNTIME_STATE_CLOSE, start)
        if end < 0:
            return content
        end += len(RUNTIME_STATE_CLOSE)
        # 分隔线在 open 标记之前：只有**紧邻**且精确匹配才一并剥掉，避免误伤用户原文里的 "---"。
        separator_start = start - len(RUNTIME_STATE_SEPARATOR)
        if (
            separator_start >= 0
            and content[separator_start:start] == RUNTIME_STATE_SEPARATOR
        ):
            start = separator_start
        prefix = content[:start].rstrip()
        suffix = content[end:].lstrip()
        if prefix and suffix:
            return prefix + "\n\n" + suffix
        return prefix or suffix

    @staticmethod
    def usage_fields(res) -> dict:
        """从模型响应提取缓存/用量字段；字段缺失时返回 None，不抛错。

        两个来源：LangChain 的 `usage_metadata.input_token_details.cache_read` 与
        provider 原始 `response_metadata.token_usage.prompt_cache_hit_tokens`。
        当前 provider 两者都有（实测 2026-10-20），保留两个是为了换 provider 时还能看见。
        """
        usage = getattr(res, "usage_metadata", None)
        if not isinstance(usage, dict):
            usage = {}
        details = usage.get("input_token_details")
        if not isinstance(details, dict):
            details = {}
        metadata = getattr(res, "response_metadata", None)
        token_usage = metadata.get("token_usage") if isinstance(metadata, dict) else None
        if not isinstance(token_usage, dict):
            token_usage = {}
        return {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read": details.get("cache_read"),
            "cache_hit": token_usage.get("prompt_cache_hit_tokens"),
            "cache_miss": token_usage.get("prompt_cache_miss_tokens"),
        }

    @staticmethod
    def _log_usage(res, state: AgentState) -> None:
        fields = LLMNode.usage_fields(res)
        logger.info(
            "llm_usage input=%s output=%s cache_read=%s cache_hit=%s cache_miss=%s session=%s",
            fields["input_tokens"],
            fields["output_tokens"],
            fields["cache_read"],
            fields["cache_hit"],
            fields["cache_miss"],
            state.get("session_id"),
        )

    async def _model_for(self, state: AgentState) -> Runnable:
        """取本次要用的模型：按工具集版本决定要不要重新 `bind_tools`（§3.4——不必每轮 bind）。

        缓存键含 (工作区, 会话, 版本)：会话必须进键——同一节点实例可能服务多个会话
        （langgraph dev 一图多 thread），只按版本比会把 A 会话的工具集绑给 B。
        """
        if self.toolset is None:
            # 没有 toolset：**一次都不调 bind_tools**。worker 路径与一批单测的假模型只有
            # ainvoke，无条件 bind 会把它们全打红。
            return self.model

        tools, version = await self.toolset.tools_and_version(state)
        key = (self.workspace_path, state.get("session_id"), version)
        if self._bound is None or self._bound_key != key:
            self._bound = self.model.bind_tools(tools)
            self._bound_key = key
        return self._bound

    async def _read_profile(self) -> str:
        """项目画像只读一次（会话首启读入，§4）：实例内 memo——画像默认慢变。

        读盘与截断在资源层（`app/resource/profile.py::agent_md_block`）；**留在本节点的是
        "读一次还是每轮读"这个决定**——那是"这一拍做什么"，不是资源访问（2026-09-21 剥离时
        划的线，判据见 docs/ARCHITECTURE.md §4 的 A/B/C）。

        ⚠️ 这份 memo 是**实例内**的：TUI 路径下每个会话新建一张图/一个实例，改了 AGENT.md
        重开会话即生效；但零参入口（langgraph dev）一图多 thread、同一实例跨会话复用，那条
        路径下不重开进程就不会刷新（与 `_model_for` 注释里记的是同一类偏差）。
        """
        if self._profile_block is None:
            self._profile_block = await asyncio.to_thread(
                profile.agent_md_block, self.workspace_path
            )
        return self._profile_block

#-------------------ToolMessage 的结构化戳（**生产端单点**）-------------------
# 形状：`additional_kwargs` 里 `{"error_type": <语义标记>}` 或 `{"decision": "denied"}`（首次落地
# 2026-09-16）。**生产端 = 本模块的 ToolNode / ReviewNode**（全仓只有这两个写出点，且都经
# `_tool_stamp` 构造）；**消费端 = `compression.parser.tm_status`**（读这两个键）。
#
# 为什么不把键名/取值做成常量：**它们是字典的键，用变量承接与直接写字面量没有区别**
# （2026-09-22 用户拍板）——常量该留给"要按值分派或校验"的地方（如 `GATE_*` / `ASK_SELECT_*`）。
# 真正有价值的是 `_tool_stamp` 这个**构造器**：它保证两个生产端写出同一种形状。
def _tool_stamp(*, error_type: str | None = None, decision: str | None = None) -> dict:
    """构造 ToolMessage 的结构化戳（生产端唯一构造入口）。"""
    stamp: dict = {}
    if error_type:
        stamp["error_type"] = error_type
    if decision:
        stamp["decision"] = decision
    return stamp


#-------------------工具调用节点-----------------------
class ToolNode:
    """执行已批准的普通 MCP 工具，并把工具返回渲染成 ToolMessage 回传模型。

    工具返回的归一化在 app/platform/tool_results.py（format_tool_result 产模型可见文本、
    coerce_tool_result 还原结构化 ToolResult 供打戳，ToolNode 与 dispatch 共用），
    本节点只负责 ainvoke 执行已批准工具 + 拼 ToolMessage。
    """

    def __init__(self, tools: list[Callable] | list[BaseTool] | None = None, toolset=None) -> None:
        # toolset 给了就**每轮重解析**工具表（技能可能在本回合的编排阶段刚被卸载/加载）；
        # 没给就用固定的 tools 列表——worker 路径与既有单测一字不变。
        self.toolset = toolset
        self.tools_map = {}

        for tool_obj in tools or []:
            name = getattr(tool_obj, "name", None)
            if name:
                self.tools_map[name] = tool_obj

    async def _current_map(self, state: AgentState) -> dict:
        """本次要查的工具表：给了 toolset 就现取（动态），否则用构造时的固定表。"""
        if self.toolset is None:
            return self.tools_map
        return await self.toolset.tools_map(state)

    async def _log_event(self, state, tool_call, *, status, error_type, content_chars):
        """写一行确定性 event log；工作区/会话缺失或写盘失败都不影响工具执行。"""
        workspace = getattr(self.toolset, "workspace", None)
        session = state.get("session_id")
        if not workspace or not session:
            return
        await asyncio.to_thread(
            event_log.append_event,
            workspace,
            session,
            tool=tool_call.get("name") or "",
            tool_call_id=tool_call.get("id") or "",
            args=tool_call.get("args") or {},
            status=status,
            error_type=error_type,
            approval=tool_call.get("_approval") or "approved",
            content_chars=content_chars,
        )

    async def __call__(self, state: AgentState) -> dict | AgentState:
        tool_calls = list(state.get("approved_tool_calls", []))
        tools_map = await self._current_map(state)

        results: list[ToolMessage] = []

        for tool_call in tool_calls:
            tool_name = tool_call.get("name")
            tool_args = tool_call.get("args", {})
            tool_call_id = tool_call.get("id", "unknown")

            extra: dict = {}
            if not tool_name or tool_name not in tools_map:
                result_content = (
                    f"工具不存在或未注册: {tool_name}"
                    f"（不在本会话当前可用的工具表里——可能所属技能刚被卸载、"
                    f"或它的运行体已断开）。若仍需要它，请先用 get_skill 重新加载对应技能，"
                    f"不要原样重试。"
                )
                results.append(
                    ToolMessage(
                        content=result_content,
                        tool_call_id=tool_call_id,
                        name=tool_name,
                        additional_kwargs=_tool_stamp(error_type="unknown_tool"),
                    )
                )
                await self._log_event(
                    state,
                    tool_call,
                    status="failed",
                    error_type="unknown_tool",
                    content_chars=len(result_content),
                )
                continue

            tool_obj = tools_map[tool_name]
            try:
                # MCP 工具是 async-only(只实现了 coroutine)，必须用 ainvoke
                # (同步 invoke 会抛 "StructuredTool does not support sync invocation")
                result_value = await tool_obj.ainvoke(tool_args)
                result_content = format_tool_result(result_value)
                # 结构化戳：返回里若带 ToolResult 且失败，把 error_type 挂到消息上，
                # 压缩消费端直接读字段、不必解析 content 前缀。
                tr = coerce_tool_result(result_value)
                if tr is not None and not tr.success:
                    extra = _tool_stamp(error_type=tr.error_type or "tool_error")
            except Exception as exc:
                result_content = f"工具执行失败: {exc}"
                extra = _tool_stamp(error_type="tool_error")

            msg_kwargs: dict = {
                "content": result_content,
                "tool_call_id": tool_call_id,
                "name": tool_name,
            }
            if extra:
                msg_kwargs["additional_kwargs"] = extra
            results.append(ToolMessage(**msg_kwargs))

            error_type = extra.get("error_type") if extra else None
            await self._log_event(
                state,
                tool_call,
                status="failed" if error_type else "ok",
                error_type=error_type,
                content_chars=len(result_content),
            )

        return {"messages": results, "approved_tool_calls": []}


#----------------------工具审核节点--------------------
class ReviewNode:
    """逐条过审 pending 工具调用：按 tool.json 定处置（放行 / 挂起问人 / 回执），再按 source 分流。

    本节点是**人机闸门的唯一落点**，两种载荷都在这里挂起（docs/MULTI_AGENT.md §6）：
    - `tool_approval`：审批（need_review 的调用）；
    - `ask_user`：agent 提问（source=ask 的调用，见 ASK_SOURCE）。

    ⚠️ **interrupt 只能待在"挂起之前没有副作用"的节点里**——这条是承重约束，不是风格偏好。
    LangGraph 的 resume 会**把整个节点从头重跑**（interrupt() 依次返回记录的 resume 值），
    实测钉死于 2026-09-16：一个含 `print` + `interrupt()` 的节点，resume 后 print 出现第二次。
    注意重跑丢弃的是**未提交的 state 写**，而节点里发生的**真实外部副作用不会回滚**
    （写文件、起进程都会真切两次）——所以 `ask_user` 的 interrupt **不能**放进
    `OrchestrateNode`（同批次里可能有 write_memory）。本节点满足该约束：interrupt 之前只有
    一行日志 print；工具的执行与副作用一律交给下游执行器。

    与 ToolNode/OrchestrateNode 共用同一份 tool.json（读取与缓存单点在 app/agent/gates.py）。
    """

    # 编排工具在 tool.json 的 source 取值集合：命中即分流进 approved_orchestrate_calls，
    # 交 OrchestrateNode（通用编排执行器）处理。plan=编排；notes=笔记读回；
    # ask=向用户提问（见下 ASK_SOURCE 的说明）；
    # memory=长期记忆读写（app/agent/tools.py 的 write_memory/read_memory，靠注入的 workspace
    # 定位记忆文件）；skill=技能加载/卸载（同文件 get_skill/drop_skill）；
    # events=确定性 event log 的只读查询（read_event_log，同样靠注入的 workspace 定位会话日志）。
    # 注意两处**不在**这里的：① 技能**带来**的工具——它们的 source 取 `skills/<name>`，不命中本
    # 集合 → 进普通工具队列（get_skill 是编排动作，它带来的工具不是）；② `dispatch_subtasks`
    # ——2026-09-21 起它是 `mcp_service/dispatch` 这个 server 的工具（派生 worker = 独立进程
    # 与独立 Agent runtime，四问的第四问），走 ToolNode，**不再进编排队列**。
    # 单一来源在 `gates.ORCHESTRATE_SOURCES`（2026-11-04 下移：压缩引擎也要用它，
    # 而 compression 不能反向 import 本模块）；这里保留同名类属性，便于读者按闸门查。
    ORCHESTRATE_SOURCES: frozenset[str] = gates.ORCHESTRATE_SOURCES

    # 人机闸门的第二种载荷（docs/MULTI_AGENT.md §6）：source=ask 的工具（当前只有 ask_user）
    # **除了走编排执行器，还要在本节点再挂一次 interrupt 向人提问**。分流进编排队列照旧——
    # 回答经 state 的 ask_answers 交给工具体渲染成回执，见下面 __call__ 的 ASK 分支。
    ASK_SOURCE = "ask"

    def __init__(self, log: bool = True, toolset=None, allow_ask: bool = True) -> None:
        """log=False 供 stdio MCP server 型 worker 使用（stdout 即 JSON-RPC 协议，不能 print）。

        toolset：给了才做"存在性 + 漏登记"那道防线（主图给；worker 与单测不给 → 行为与改造前一致）；
        allow_ask=False 供 worker 用：**本图不承载提问载荷**（见 __call__ 的 ASK 分支）。
        """
        self.tool_review = gates.tool_config()
        self.log = log
        self.toolset = toolset
        self.allow_ask = allow_ask

    def _known_names(self, state: AgentState) -> set[str] | None:
        """当前可用的工具名（含编排工具）；无 toolset（worker / 单测）→ None = 不判存在性。"""
        if self.toolset is None:
            return None
        return self.toolset.known_names(state)

    def _tool_cfg(self, tool_name: str | None, state: AgentState) -> dict:
        """一条工具调用的策略条目：**内置表（tool.json）优先，其次已加载技能的 skill.json 声明**。

        2026-09-19 起技能自带工具的 `need_review` 声明在技能目录自己的 skill.json（约束跟能力
        走，见 skills.read_tool_policies）；tool.json 只管内置 MCP 工具与编排工具。技能工具能
        走到本查询的前提是它已被加载——加载期 `_check_skill_tools` 已逐条校验过声明形状，这里
        信任那份声明。两级都没有 → {}（`_decide` 把"在工具表里却两头无政策"判成 review，
        fail-closed 第二道防线不变）。读盘不缓存：与加载期校验同一口径，改 skill.json 立即生效。
        """
        if not tool_name:
            return {}
        cfg = self.tool_review.get(tool_name)
        if cfg is not None:
            return cfg
        if self.toolset is None:
            return {}
        for skill_name in state.get("loaded_skills") or []:
            meta = get_meta(skill_name)
            if meta is None:
                continue
            conf = read_tool_policies(meta).get(tool_name)
            if conf is not None:
                return conf
        return {}

    def _decide(
        self,
        tool_name: str | None,
        tool_cfg: dict,
        state: AgentState,
        tool_args: dict | None = None,
    ) -> str:
        """给一条待审调用定处置：`unknown` / `review` / `free`。

        - **unknown**：名字不在当前工具表里（模型编的，或它所属技能刚被卸载）→ 回执、**不弹面板**。
          否则模型每编一个名字就打断人一次，而它无论如何都会被 ToolNode 拒掉；
        - **free（命令级）**：tool.json 标了 `free_when: readonly_shell` 的工具（当前只有
          run_command），**还要它的参数**判为只读终端命令才算数，见 `gates.free_shell_verdict`。
          判定不过就落回 review —— 这条路是加宽免审面的唯一入口，失败方向必须是收紧。
        - **review**：策略说 `need_review: true`（内置表或技能自己的 skill.json）；**或**它在
          工具表里却两头都没有政策条目 —— 这条是 §13.4 的第二道防线（fail-closed）：有人往
          MCP server 加了个写工具却忘了声明，旧行为是"未声明 = 免审"，等于静默放行一次写操作。
          限定在"在表里"是为了不误伤模型编出来的名字（那是 unknown，回执而不弹面板）。
        - **free**：有政策条目且 `need_review: false`。
        """
        if tool_name is None:
            return "free"
        known = self._known_names(state)
        if known is not None and tool_name not in known:
            return "unknown"
        if tool_cfg.get("free_when") == "readonly_shell" and gates.free_shell_verdict(tool_args or {}):
            return "free"
        if tool_cfg.get("need_review"):
            return "review"
        if known is not None and not tool_cfg:
            return "review"
        return "free"

    def _receipt_result(
        self, state: AgentState, current_tool: dict, content: str, error_type: str
    ) -> dict:
        """"回执而不执行"的通用返回切片：兑现悬空 tool_call + 给模型一条可行动的错误说明。

        未知工具与参数非法的提问共用它——两者都是"这条调用不会被送去执行，但模型必须知道
        为什么"。**以原 tool_call_id 兑现**是必须的：否则历史里会留下一条永远没有回应的
        assistant tool_calls 块。
        """
        return {
            "pending_tool_calls": list(state.get("pending_tool_calls", []))[1:],
            "approved_orchestrate_calls": list(state.get("approved_orchestrate_calls", [])),
            "approved_tool_calls": list(state.get("approved_tool_calls", [])),
            "messages": [
                ToolMessage(
                    name=current_tool.get("name"),
                    tool_call_id=current_tool.get("id"),
                    content=content,
                    additional_kwargs=_tool_stamp(error_type=error_type),
                )
            ],
        }

    def _unknown_result(self, state: AgentState, current_tool: dict) -> dict:
        """未知工具：兑现悬空 tool_call + 回一条可行动回执（既不打扰人、也不放行去执行）。"""
        tool_name = current_tool.get("name")
        return self._receipt_result(
            state,
            current_tool,
            (
                f"工具不存在或未注册: {tool_name}"
                f"（不在本会话当前可用的工具表里——可能所属技能刚刚被卸载、或它的运行体"
                f"已断开）。若仍需要它，请先用 get_skill 重新加载对应技能；不要原样重试。"
            ),
            "unknown_tool",
        )

    async def _log_event(
        self,
        state: AgentState,
        tool_call: dict,
        *,
        status: str,
        error_type: str | None,
        approval: str,
        content_chars: int = 0,
    ) -> None:
        """写一行确定性 event log（只记未被执行 / 被拒的闸门结果）。

        已放行的执行结果由 ToolNode / OrchestrateNode 写——同一 tool_call 只落一行，不重复。
        """
        workspace = getattr(self.toolset, "workspace", None)
        session = state.get("session_id")
        if not workspace or not session:
            return
        await asyncio.to_thread(
            event_log.append_event,
            workspace,
            session,
            tool=tool_call.get("name") or "",
            tool_call_id=tool_call.get("id") or "",
            args=tool_call.get("args") or {},
            status=status,
            error_type=error_type,
            approval=approval,
            content_chars=content_chars,
        )

    async def __call__(self, state: AgentState) -> dict | AgentState:
        pending = list(state.get("pending_tool_calls", []))
        if not pending:
            return {"pending_tool_calls": []}

        current_tool = pending[0]
        tool_name = current_tool.get("name")
        if self.log:
            # 工具调用日志：每条待审调用过审核节点时打一行（含需要人工审批与免审放行者）
            print(_tool_call_log(current_tool))
        tool_cfg = self._tool_cfg(tool_name, state)
        approved = True
        denied_messages: list[ToolMessage] = []

        action = self._decide(tool_name, tool_cfg, state, current_tool.get("args", {}))
        if action == "unknown":
            # 模型编出来的名字（本会话根本没绑定过它）：回执而不弹面板，见 _decide 的说明
            result = self._unknown_result(state, current_tool)
            content = result["messages"][0].content
            await self._log_event(
                state,
                current_tool,
                status="failed",
                error_type="unknown_tool",
                approval="not_required",
                content_chars=len(content),
            )
            return result

        ask_answers: dict | None = None
        if action == "review":
            payload = {
                "type": GATE_TOOL_APPROVAL,
                "current_step": "1/1",
                "tool_name": tool_name,
                "tool_args": current_tool.get("args", {}),
                "tool_call_id": current_tool.get("id"),
            }
            decision = interrupt(payload)
            approved = isinstance(decision, dict) and decision.get("approved", False)
            if not approved:
                # 被拒的调用必须回灌一条显式消息，而不是静默丢弃：
                # - 模型得知道"这次调用没执行、原因是被拒"，否则会原样重试同一命令；
                # - 这条 ToolMessage 以原 tool_call_id 兑现历史里悬空的 tool_calls 块，
                #   避免下一条用户消息紧跟未兑现的 assistant tool_call（协议隐患）。
                denied_messages.append(
                    ToolMessage(
                        name=tool_name,
                        tool_call_id=current_tool.get("id"),
                        content=(
                            "[approval_denied] 工具调用被人工拒绝，未执行。"
                            "不要原样重试相同参数；若仍认为该操作必要，"
                            "先向用户说明意图或提出替代方案。"
                        ),
                        # 结构化戳：压缩消费端读 decision=denied 即可判定，不必解析前缀
                        additional_kwargs=_tool_stamp(decision="denied"),
                    )
                )
                await self._log_event(
                    state,
                    current_tool,
                    status="denied",
                    error_type=None,
                    approval="denied",
                    content_chars=len(denied_messages[-1].content),
                )

        elif tool_cfg.get("source") == self.ASK_SOURCE and not self.allow_ask:
            # worker：提问载荷在这张图里不成立（worker 是只读资料收集器、不该问用户）。它**结构性**
            # 拿不到 ask_user（source=ask 不在 worker_tools 的规则里），但模型仍可能编出这个名字——
            # 而 worker 的 ReviewNode 没有 toolset，"未知工具"那道防线不生效。真按提问挂起的话，
            # 主侧会收到一张语义错乱的面板、人还得白答一次。回执让它把"该问什么"写进结论交回去。
            return self._receipt_result(
                state,
                current_tool,
                f"{tool_name} 在本图不可用：你不能向用户提问——把「需要用户定夺什么」写进"
                "结论交回主 agent，由它来问。不要原样重试。",
                "ask_not_available",
            )
        elif tool_cfg.get("source") == self.ASK_SOURCE:
            # 人机闸门的第二种载荷：把问题摆给人、等回答（docs/MULTI_AGENT.md §6）。
            # 参数先在弹面板**之前**校验——空问题 / 超限选项摆到人面前是最糟的形态。
            request, problem = gates.ask_request(current_tool.get("args") or {})
            if problem is not None:
                result = self._receipt_result(
                    state, current_tool, problem, "invalid_ask"
                )
                await self._log_event(
                    state,
                    current_tool,
                    status="failed",
                    error_type="invalid_ask",
                    approval="not_required",
                    content_chars=len(problem),
                )
                return result
            # 不带 current_step：那是审批面板的遗留样式（恒为 "1/1"），提问面板不需要。
            decision = interrupt(
                {
                    "type": GATE_ASK_USER,
                    "why": request["why"],
                    "question": request["question"],
                    "options": request["options"],
                    # 模态透传给面板：它据此提示"可多选"、并决定输入多个序号时收下还是重问
                    "select": request["select"],
                    "tool_call_id": current_tool.get("id"),
                }
            )
            # 回答**不回 messages**（不像被拒那样在此地造 ToolMessage）：写进 state 的 ask_answers，
            # 由 ask_user 工具（下游 OrchestrateNode 执行）按 tool_call_id 取回、渲染成回执。
            # 闸门的产出是 state，执行器消费 state——与 approved_* 队列同一条路子。
            ask_answers = dict(state.get("ask_answers") or {})
            ask_answers[current_tool.get("id")] = gates.normalize_ask_answer(
                decision, request["options"]
            )

        # 按 tool.json 的 source 分流：编排类进 orchestrate 队列，其余进普通工具队列
        is_orchestrate = tool_cfg.get("source") in self.ORCHESTRATE_SOURCES

        approved_orchestrate = list(state.get("approved_orchestrate_calls", []))
        approved_calls = list(state.get("approved_tool_calls", []))
        if approved:
            # 带审批结果下放：event log 用它区分 approved / not_required，不必在 ToolNode
            # 再猜一次策略。普通工具执行、编排工具执行都读这把字段。
            routed = dict(current_tool)
            routed["_approval"] = "approved" if action == "review" else "not_required"
            (approved_orchestrate if is_orchestrate else approved_calls).append(routed)

        remaining = pending[1:]

        result: dict = {
            "pending_tool_calls": remaining,
            "approved_orchestrate_calls": approved_orchestrate,
            "approved_tool_calls": approved_calls,
        }
        if ask_answers is not None:
            result["ask_answers"] = ask_answers
        if denied_messages:
            result["messages"] = denied_messages
        return result


#----------------------编排节点----------------------

#-------------------编排工具返回切片的**形状定义处**（协议权威，2026-09-21）-------------------
# 编排工具（app/agent/tools.py）的返回值是一个 dict，键只有三类：
#   - `messages`：**必给**，工具自己拼好的 ToolMessage 回执（原样并入对话，不解析内容）；
#   - `current_plan`：可选，有链式语义——同回合的后续编排调用看得到上一步改过的它；
#   - `_EXTRA_SLICE_KEYS` 里列的：可选，写回 state 的同名切片（**加新切片 = 改这一行 + state.py**）。
# **白名单之外的键一律被静默丢弃**（与键名有没有下划线无关）：这是刻意的——state 的写入面必须
# 显式，工具返回里多出来的东西（写错的键、顺手塞的调试字段）不该悄悄进 state。
# ⚠️ 代价是"两侧靠约定相连"：生产者（tools.py 的各编排工具）**不知道这张白名单存在**，只是照
# 惯例返回 `{"loaded_skills": …}`——白名单删一项、或工具开始返回新切片，症状都是**静默丢弃**
# （回执正常、状态没生效）。所以 `tests/test_plan_tools.py` 有一条契约用例扫 tools.py 各工具返回
# 的键，断言它们 ⊆ {messages, current_plan} ∪ `_EXTRA_SLICE_KEYS`。
#
# **权威为什么在消费端（本节点）而不是生产端**：两者分居 `nodes.py` / `tools.py`，而
# `nodes.py` **刻意不 import `tools.py`**（它拿的是构图期传进来的工具对象，见本文件顶部对
# `_invoke` 的说明）——权威放生产端就得造一条反向 import。所以约定写成"**执行器定义协议、
# 生产端按它写**"：`tools.py` 的模块 docstring 只描述**本组工具给哪些键**，形状规则以这里为准。
_EXTRA_SLICE_KEYS: tuple[str, ...] = ("loaded_skills",)


class OrchestrateNode:
    """消费编排类调用（approved_orchestrate_calls），执行编排工具并合并写回 state。

    编排工具 = **改变 agent 自身编排语义**的工具（计划 / 笔记 / 提问 / 记忆 / 技能），与
    改变或读取外部世界的 MCP 工具相对（判据见 docs/ARCHITECTURE.md §4「工具面」）。它们住在主
    进程、需要注入，但**改变的方式各不相同**——故本节点**不假设统一的返回值形状**，只做两件事：
    把工具产出的消息并入 messages、把工具写回的切片合并进 state。**不调 format_tool_result**
    （那是 MCP 侧的跨进程解封装；返回切片的形状规则见本节点上方那段权威说明）。当前出现两种
    形状（**现状描述，不是规则**）：

    - **带切片**：`{current_plan?, loaded_skills?, messages}`——计划三件套、`get_skill` / `drop_skill`；
    - **纯回执**：`{"messages": [ToolMessage]}`——`read_note`、`ask_user`、`write_memory` /
      `read_memory`。

    执行细节：

    - 逐个调用编排工具的底层原函数，显式注入 `state`（整份 AgentState）与
      `tool_call_id`（本次调用 id）——这两个参数用 InjectedState / InjectedToolCallId
      标注、对模型隐藏，由本节点代填；
    - 同回合多次编排调用按顺序执行，并把上一步写回的 `current_plan` 链式传给下一步
      （如 create_plan → update_plan_step → clear_plan 依次生效）；
    - 把工具返回的 ToolMessage 原样并入 messages 交回模型；工具失败/未注册时生成
      对应的错误 ToolMessage，且不改动 current_plan；
    - 结束后清空 approved_orchestrate_calls；不动 approved_tool_calls（留给 tool_node）。

    ⚠️ **本节点不承载 interrupt，也不得把它搬进来**：编排工具**并非都无真实副作用**——只有
    计划三件套、`read_note`、`ask_user`、`read_memory` 是只碰会话内 state 的；另外三个**有真实
    副作用**：`write_memory`（写盘）、`get_skill` / `drop_skill`（起/关技能运行体）。而 resume 会
    **把整节点从头重跑**、且**不回滚外部副作用**（约束全文见 ReviewNode 的 docstring）——把
    interrupt 放进这里，重跑一次就多写一次盘、多重起一次运行体。**闸门的唯一落点在 ReviewNode。**
    （`dispatch_subtasks` 2026-09-21 已搬去 `mcp_service/dispatch.py`，不再进本节点。）
    """

    def __init__(
        self,
        orchestrate_tools: list[Callable],
        workspace_path: str | None = None,
    ) -> None:
        self.tools_map = {}
        self.workspace_path: str | None = workspace_path
        for tool_obj in orchestrate_tools:
            name = getattr(tool_obj, "name", None)
            if name:
                self.tools_map[name] = tool_obj

    @staticmethod
    def _invoke(
        tool_obj: Callable,
        tool_args: dict,
        state: dict | AgentState,
        tool_call_id: str,
        workspace: str | None = None,
    ):
        """调用编排工具的底层原函数，按签名注入 state / tool_call_id / workspace。

        编排工具为 langchain @tool 包装的同步或异步函数：同步取 `tool_obj.func`，
        异步取 `tool_obj.coroutine`（async @tool 的 func 为 None）。直接以关键字调用，
        跳过 langchain 的注入管线（与 tests/test_plan_tools.py 一致）。workspace 供
        dispatch 类编排工具用（对模型隐藏的 InjectedWorkspace 参数），node 持有。
        """
        fn = (
            getattr(tool_obj, "coroutine", None)
            or getattr(tool_obj, "func", None)
            or tool_obj
        )
        kwargs = dict(tool_args)
        # 只向签名里确实声明了对应注入参数的编排工具注入（覆写模型可能伪造的同名键）
        params = signature(fn).parameters
        if "state" in params:
            kwargs["state"] = state
        if "tool_call_id" in params:
            kwargs["tool_call_id"] = tool_call_id
        if "workspace" in params and workspace is not None:
            kwargs["workspace"] = workspace

        # 返回可能为 coroutine（async 编排工具），由调用方 await
        return fn(**kwargs)

    async def _log_event(
        self, state, tool_call, *, status, error_type, content_chars
    ) -> None:
        """写一行确定性 event log；工作区/会话缺失或写盘失败都不影响编排执行。"""
        session = state.get("session_id")
        if not self.workspace_path or not session:
            return
        await asyncio.to_thread(
            event_log.append_event,
            self.workspace_path,
            session,
            tool=tool_call.get("name") or "",
            tool_call_id=tool_call.get("id") or "",
            args=tool_call.get("args") or {},
            status=status,
            error_type=error_type,
            approval=tool_call.get("_approval") or "not_required",
            content_chars=content_chars,
        )

    async def __call__(self, state: AgentState) -> dict | AgentState:
        tool_calls = list(state.get("approved_orchestrate_calls", []))
        if not tool_calls:
            return {}

        # 链式工作的 state：同回合多次编排调用都基于它更新 current_plan
        working = dict(state)
        working.setdefault("current_plan", [])

        messages: list[ToolMessage] = []
        # 本回合真的被工具写过的额外切片（working 由 state 复制而来，所以"键存在"不等于
        # "被写过"——只有写过的才需要回传，其余交给 LangGraph 保留原值）
        written: set[str] = set()

        for tool_call in tool_calls:
            tool_name = tool_call.get("name")
            tool_args = tool_call.get("args", {})
            tool_call_id = tool_call.get("id", "unknown")

            tool_obj = self.tools_map.get(tool_name) if tool_name else None
            if tool_obj is None:
                content = f"工具不存在或未注册: {tool_name}"
                messages.append(
                    ToolMessage(
                        content=content,
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    )
                )
                await self._log_event(
                    state,
                    tool_call,
                    status="failed",
                    error_type="unknown_tool",
                    content_chars=len(content),
                )
                continue

            try:
                result = self._invoke(
                    tool_obj, tool_args, working, tool_call_id, self.workspace_path
                )
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                content = f"工具执行失败: {exc}"
                messages.append(
                    ToolMessage(
                        content=content,
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    )
                )
                await self._log_event(
                    state,
                    tool_call,
                    status="failed",
                    error_type="tool_error",
                    content_chars=len(content),
                )
                continue

            if not isinstance(result, dict):
                content = f"工具返回格式非法: {type(result).__name__}"
                messages.append(
                    ToolMessage(
                        content=content,
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    )
                )
                await self._log_event(
                    state,
                    tool_call,
                    status="failed",
                    error_type="invalid_tool_result",
                    content_chars=len(content),
                )
                continue

            # 工具返回切片：链式更新进 working（同回合后续调用看得到），messages 回执原样收集
            before = len(messages)
            if "current_plan" in result:
                working["current_plan"] = result["current_plan"]
            for key in _EXTRA_SLICE_KEYS:
                if key in result:
                    working[key] = result[key]
                    written.add(key)
            for block in result.get("messages", []) or []:
                if isinstance(block, ToolMessage):
                    messages.append(block)
                else:
                    messages.append(
                        ToolMessage(content=str(block), tool_call_id=tool_call_id, name=tool_name)
                    )
            await self._log_event(
                state,
                tool_call,
                status="ok",
                error_type=None,
                content_chars=sum(
                    len(str(message.content)) for message in messages[before:]
                ),
            )

        # current_plan 恒带回（既有行为不变，链式默认值已在上面 setdefault）；
        # 额外切片只在本回合真的被写过时才带回。
        return {
            "approved_orchestrate_calls": [],
            "messages": messages,
            "current_plan": working["current_plan"],
            **{key: working[key] for key in _EXTRA_SLICE_KEYS if key in written},
        }


#----------------------审核队列处理节点-------------------
class QueueNode:
    """轮次开始时的队列复位：把模型这一轮要调的工具搬进 pending，并清空上一轮的放行/回答。

    三个 approved_* 队列与 ask_answers 都是**单轮内**的东西（审批状态本身则持久在队列里，
    见 docs/ARCHITECTURE.md）：新的一轮开始时它们必须归零，否则上一轮没执行完的调用会漏进
    这一轮，而上一轮的回答会一直躺在 checkpoint 里。
    """

    async def __call__(self,state: AgentState) -> dict | AgentState:
        tool_calls = getattr(state["messages"][-1], "tool_calls", None) or []
        return {
            "pending_tool_calls": tool_calls,
            "approved_tool_calls": [],
            "approved_orchestrate_calls": [],
            "ask_answers": {},
        }


#----------------------轮末压缩（compact_node）----------------------
# 轮内压缩的预算默认值。**轮末 CompactNode 不再使用它**——2026-10-20 起轮末压缩无条件
# 启用，没有字符预算门；保留本值是为了轮内节点仍有同一个"字符数代理"口径可用。
_CONTEXT_BUDGET_DEFAULT = 60000


def needs_compact(state, budget: int) -> bool:
    """预算门（**仅测试/兼容保留**）：历史 content 字符总数超过 budget。

    轮末 `CompactNode` 已改为无条件启用；轮内 `CompressNode` 已改用可压正文水位
    （`compressible_chars`，见 §8.6），不再调用本函数。
    """
    total = sum(len(str(getattr(m, "content", "") or "")) for m in (state.get("messages") or []))
    return total > budget


class CompactNode:
    """轮末压缩节点（END 前执行）：`compression.compact` 的 **state 适配器**。

    引擎产出一个 `FoldPlan`；本节点只做翻译：

    - 每个 `FoldGroup` 生成一条 `SystemMessage(摘要, id=该组最早被删消息的 id)` 原位替换；
    - `RemoveMessage` 删除该组其余被折块；
    - `notes` 合并（仅联网四工具的成功正文）；
    - 混合块里保留调用后的 `AIMessage` 副本由 `group.replaced` 原位写回。

    折叠区 = 最后一条 HumanMessage 之前（含更早轮次）；当前轮原样保留。折叠规则
    （完整/已消费、编排工具保护、失败/被拒整块保留、联网正文归档）见
    `app/agent/compression/compact.py`。纯确定性、不调 LLM；没有可折块时返回 `{}`。
    """

    def __init__(self) -> None:
        """编排工具集合从 tool.json 的 source 派生（策略单点：compression.policy）。"""
        self._orchestrate_tools = compression_policy.orchestrate_tools()

    async def __call__(self, state: AgentState) -> dict | AgentState:
        messages = list(state.get("messages") or [])
        if not messages:
            return {}

        notes = dict(state.get("notes") or {})
        plan = plan_compact(messages, notes, self._orchestrate_tools)
        if not plan.groups:
            return {}

        updates: list = []
        for group in plan.groups:
            if not group.anchor_id or not group.removed:
                continue
            updates.append(SystemMessage(content=group.summary, id=group.anchor_id))
            updates += group.replaced
            updates += [
                RemoveMessage(id=m.id)
                for m in group.removed[1:]
                if getattr(m, "id", None)
            ]
        if not updates:
            return {}

        result: dict = {"messages": updates}
        if plan.archived_notes:
            notes.update(plan.archived_notes)
            result["notes"] = notes
        return result


#----------------------轮内压缩（compress_node）----------------------
# 区域边界（2026-10-20 定，与 CompactNode 互不相交）：
#   CompactNode  : messages[:last_human_index]        旧轮，轮末无条件执行
#   CompressNode : messages[last_human_index + 1:]    当前轮，工具循环中执行
# 轮内**不删消息**：图路由中间删掉结果会破坏 tool_call / ToolMessage 配对与调用方状态，
# 因此只改 ToolMessage.content。算法与阈值见 `app/agent/compression/compress.py`。
class CompressNode:
    """轮内压缩节点：`compression.compress.TurnCompressor` 的 **state 适配器**。

    引擎负责当前轮的三件事（细节见引擎模块 docstring）：read_file 去重指针化、批级
    移动窗口、状态回执变薄。本节点只把引擎返回的 ToolMessage 替换按原顺序写回 state；
    不增删消息、不改 tool_call 配对、不调 LLM。

    状态（2026-10-20）：已接进主图（`tool_node` / `orchestrate_node` → `compress_node`
    → `llm_node`）；worker 子图不带本节点。
    """

    def __init__(
        self,
        read_dedup_min_chars: int = READ_DEDUP_MIN_CHARS,
        high_watermark_chars: int = TURN_HIGH_WATERMARK_CHARS,
        low_watermark_chars: int = TURN_LOW_WATERMARK_CHARS,
        keep_blocks: int = TURN_KEEP_BLOCKS,
        mask_min_chars: int = MASK_MIN_CHARS,
        max_batch_chars: int = MAX_BATCH_CHARS,
        min_batch_chars: int = MIN_BATCH_CHARS,
    ) -> None:
        """参数语义与校验见 `TurnCompressor.__init__`（本节点只做透传）。"""
        self._engine = TurnCompressor(
            read_dedup_min_chars=read_dedup_min_chars,
            high_watermark_chars=high_watermark_chars,
            low_watermark_chars=low_watermark_chars,
            keep_blocks=keep_blocks,
            mask_min_chars=mask_min_chars,
            max_batch_chars=max_batch_chars,
            min_batch_chars=min_batch_chars,
        )

    async def __call__(self, state: AgentState) -> dict | AgentState:
        messages = list(state.get("messages") or [])
        if not messages:
            return {}

        updates = self._engine.compress(messages, state.get("current_plan") or [])
        return {"messages": updates} if updates else {}
