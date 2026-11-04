"""LLMNode：一拍内装配请求、调用模型、写回消息。"""
import asyncio

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import Runnable

from app.agent.prompt import (
    RUNTIME_STATE_CLOSE,
    RUNTIME_STATE_OPEN,
    RUNTIME_STATE_SEPARATOR,
    SYSTEM_PROMPT,
    handoff_pointer_block,
    workspace_context_block,
)
from app.agent.state import AgentState
from app.config import THINKING_MODES
from app.exception import ConfigError
from app.resource import images, profile
from app.resource.memory import memory_block
from app.resource.skills import skills_block
from app.schema.agent_schema import ImageRef, PlanStep
from ._shared import logger, settings


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

