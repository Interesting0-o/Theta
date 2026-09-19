import asyncio
import base64
import inspect
import json
import re
import shlex
import time
from functools import lru_cache
from inspect import signature
from pathlib import Path
from typing import Callable

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.types import interrupt
from app.agent.memory import memory_block
from app.agent.skills import skills_block
from app.agent.state import AgentState
from app.agent.utils import coerce_tool_result, format_tool_result
from app.agent.prompt import SYSTEM_PROMPT, handoff_pointer_block, workspace_context_block
from app.schema.agent_schema import ImageRef, NoteEntry, PlanStep
from app.schema.approval_schema import GATE_ASK_USER, GATE_TOOL_APPROVAL

_TOOL_CONFIG_PATH = Path(__file__).with_name("tool.json")


#----------------------工具调用日志（审核节点逐条输出）----------------------

def _short(text: str, limit: int = 120) -> str:
    """压成单行并截断，用于控制台日志的紧凑展示。"""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _tool_call_log(tool_call: dict) -> str:
    """把一条待审工具调用渲染成一行日志（工具名 + 紧凑参数）。"""
    name = tool_call.get("name") or "?"
    args = tool_call.get("args") or {}
    try:
        brief = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except TypeError:
        brief = str(args)
    return f"→ 工具调用: {name}  {_short(brief)}"

#-------------------大模型节点----------------------

# 工作区根的项目画像文件名（`/init` 命令生成的就是它；与 file_io 沙箱同一根）
AGENT_MD_FILENAME = "AGENT.md"

# 画像注入上限（与 memory.py::MEMORY_INJECT_CAP 同量级）：超长只截断、不做压缩/摘要
AGENT_MD_INJECT_CAP = 8000

# 注入块标题：AGENT.md 由模型/人自由撰写，加一行标题让模型知道这段是什么
_PROFILE_BLOCK_HEADER = "# 项目画像（工作区 AGENT.md）\n\n"

#-------------------@图片路径 的调用期附图通道----------------------
# 用户在消息里写 @路径（相对工作区或绝对），LLMNode 构造请求体副本时把图**临时**附上：
# state 里的消息始终是带 @路径 的纯文本，base64 只活在副本那一瞬、从不进
# messages/checkpoint——所以没有"轮末剔除"这回事，从一开始就没写进去。
# 勘察与方案见 docs/TODO.md「图像输入」条目。

# 扩展名 → data URI 的 mime。命中才算"长得像图片"（宽进策略：不像的 @token 是普通文本，
# 原样放行——@人名、@note.txt 不该被碰）
_IMAGE_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}
_IMAGE_EXT_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|bmp)", re.IGNORECASE)
# 单张体积上限：超限不读盘、给模型一行说明。这是**本地上限**，不是厂商限额（未实测核实）
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
# 一条消息最多附几张，多的只给说明（防 payload 失控）
_MAX_IMAGES_PER_MESSAGE = 4

# read_bytes 失败的哨兵（与"文件不存在"区分：前者给"读取失败"，后者给"未找到"）
_UNREADABLE = object()


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

        每次生成都在头部拼接系统提示词，顺序：行为契约 SYSTEM_PROMPT（见 prompt.py）+
        本次会话的具体工作区上下文 workspace_context_block（若有，含工作区根目录与
        终端 cwd 差异提醒）+ 当前计划的状态回显（若有）+ **项目画像**（工作区根的 AGENT.md，
        会话首启读入、实例内 memo）+ **长期记忆**（若有条目，`# 长期记忆（工作区 …）` 正文）
        + **已加载技能正文**（若有，`# 已加载技能` 正文）。
        前两环见 docs/LONG_TERM_MEMORY.md §4、末环见 docs/SKILL_DESIGN.md §3.5。只构造成临时
        列表传给模型，不改动 state["messages"]（LangGraph state 应不可变更新；系统消息也不应
        渗入历史被持久化）。

        记忆与技能正文**每轮实时读盘**（文件即真值）；画像**只读一次**（画像慢变）。三处读盘
        都是阻塞调用，走 to_thread 以避开 langgraph dev 的 blockbuster。

        用户消息里的 @图片路径走**调用期附图通道**（attach_images_to_payload）：base64 只临时
        进请求体副本，state 里的消息永远是带 @路径 的纯文本。
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
        plans = state.get("current_plan", [])
        if plans:
            system_messages.append(
                SystemMessage(
                    content="# 当前任务:\n"
                    + "\n".join(self.format_plan_status(plan) for plan in plans)
                )
            )
        if self.inject_session_context and self.workspace_path:
            profile = await self._read_profile()
            if profile:
                system_messages.append(SystemMessage(content=profile))
            block = await asyncio.to_thread(memory_block, self.workspace_path)
            if block:
                system_messages.append(SystemMessage(content=block))
            # 交接材料指针（project-handoff 技能的"接续"入口）：只 stat 存在性、不读正文，
            # 正文由模型按需 read_file。新会话靠这一行知道"有交接可接"。
            pointer = await asyncio.to_thread(handoff_pointer_block, self.workspace_path)
            if pointer:
                system_messages.append(SystemMessage(content=pointer))

        # 技能正文**独立于 inject_session_context**：它不是"这个项目/用户是谁"那类会话背景，
        # 而是模型自己按需取来的领域纪律。worker 没有 get_skill，loaded_skills 天然为空，
        # 所以不需要额外开关。state 里只有名字，正文每轮从盘上读（§3.5 的铁律）。
        loaded_skills = state.get("loaded_skills") or []
        if loaded_skills:
            skill_text = await asyncio.to_thread(skills_block, loaded_skills)
            if skill_text:
                system_messages.append(SystemMessage(content=skill_text))

        messages = [*system_messages, *state["messages"]]
        # @图片 的调用期附图：改的是**请求体副本**，state["messages"] 始终是纯文本（@路径
        # 原样留在历史里，本身就是"看过哪张图"的日志）。新附上的图记账写回 state，下一轮
        # 据此不再重发（首次-only）。worker 已在构造期关掉这条通道。
        newly_attached: list[ImageRef] = []
        if self.attach_images and self.workspace_path:
            messages, newly_attached = await self.attach_images_to_payload(
                messages, state.get("attached_images") or [], self.workspace_path
            )
        model = await self._model_for(state)
        res = await model.ainvoke(messages)
        # 原样写回：**思考内容不会进 history，但这不是因为我们剥了它**——`langchain-openai` 的
        # 响应转换是白名单式的，`reasoning_content` 压根没被提取进 AIMessage（实测见
        # tests/test_thinking_config.py 的契约用例）。所以这里没有剥离代码是**有意的**：那段代码
        # 今天空转。哪天库改成会提取它，那个用例会红——届时在这里（唯一写回点）补一手剥离，
        # 并顺带决定要不要给人看。
        out = {"messages": [res]}
        if newly_attached:
            out["attached_images"] = [*(state.get("attached_images") or []), *newly_attached]
        return out

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

    @staticmethod
    def agent_md_block(workspace_path: str, cap: int = AGENT_MD_INJECT_CAP) -> str:
        """读工作区根的 AGENT.md，返回注入用的正文（带块标题）；没有该文件/内容为空 → `""`。

        超 `cap` 截断并附一行提示——画像本应精简，截断只是兜底。

        原为独立模块 `app/agent/profile.py`（2026-09-12 并入）：生产侧只有本节点一个消费者，
        独立成模块的收益（与 memory.py 成对、常量集中）不抵一层间接。**留作静态方法而非内联**
        ——测试仍可直接调用它，不必构造 LLMNode 实例。
        """
        path = Path(workspace_path) / AGENT_MD_FILENAME
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return ""
        if len(text) > cap:
            text = text[:cap] + f"\n…（AGENT.md 超长已截断，共 {len(text)} 字符；用 read_file 看全文）"
        return _PROFILE_BLOCK_HEADER + text

    async def _read_profile(self) -> str:
        """项目画像只读一次（会话首启读入，§4）：实例内 memo——画像默认慢变。

        注意这份 memo 是**实例内**的：TUI 路径下每个会话新建一张图/一个实例，改了 AGENT.md
        重开会话即生效；但零参入口（langgraph dev）一图多 thread、同一实例跨会话复用，那条
        路径下不重开进程就不会刷新（与 `_model_for` 注释里记的是同一类偏差）。
        """
        if self._profile_block is None:
            self._profile_block = await asyncio.to_thread(
                self.agent_md_block, self.workspace_path
            )
        return self._profile_block

    #-------------------@图片 的调用期附图通道----------------------

    @staticmethod
    def _image_tokens(text: str) -> list[tuple[int, int, str, str]]:
        """扫出文本里的 @图片引用：返回 (起始, 结束, 原始路径, mime)，区间即 @token 的原位替换范围。

        mime 在**收词时**一并定下（用命中的那个扩展名反查 _IMAGE_MIME）。调用方别再拿
        `Path(raw).suffix` 反推：那是"最后一个点号之后"的整段，而收词口径是"任意处命中"，
        两者不等价——`@"shot.png "` 的 suffix 是 `.png `、`@"a.png（新版）"` 是 `.png（新版）`，
        反推出来的键根本不在表里。

        - 引号形式 `@"…"`：路径可含空格，整段须有图片扩展名才算引用，否则整个引号段原样放行；
        - 裸 token 到首个空白为止；整 token 不以图片扩展名收尾（中文习惯 `@a.png帮我看` 不打
          空格）时**截到首个图片扩展名处**，余下字符留在正文里；
        - 扩展名不在 _IMAGE_MIME 里的 @token 根本不是引用（@人名、@note.txt），原样放行
          （宽进策略，2026-09-14 与用户议定）。
        """
        tokens: list[tuple[int, int, str]] = []
        i = 0
        while (i := text.find("@", i)) != -1:
            j = i + 1
            if j >= len(text) or text[j].isspace():
                i += 1  # 裸 @ 或后面是空白：不是引用
                continue
            if text[j] == '"':
                end = text.find('"', j + 1)
                if end == -1:  # 引号没闭合：当普通文本
                    i += 1
                    continue
                raw = text[j + 1 : end]
                m = _IMAGE_EXT_RE.search(raw)
                if m:
                    tokens.append((i, end + 1, raw, _IMAGE_MIME[m.group(0)[1:].lower()]))
                i = end + 1  # 无论是不是引用都跳过整个引号段
                continue
            k = j
            while k < len(text) and not text[k].isspace():
                k += 1
            token = text[j:k]
            m = _IMAGE_EXT_RE.search(token)
            if m:
                tokens.append(
                    (i, j + m.end(), token[: m.end()], _IMAGE_MIME[m.group(0)[1:].lower()])
                )
                i = j + m.end()  # 从路径结束处继续扫（余下字符回正文，其中的 @ 还能命中）
            else:
                i = k
        return tokens

    @staticmethod
    def _resolve_image_path(raw: str, workspace_path: str) -> Path:
        """解析 @路径：相对路径以工作区为基准、绝对路径原样（含 ~ 展开），resolve() 消掉
        `../` 与符号链接。

        与 mcp_service/file_io.py::_resolve_path 同形但**不 import 它**：那个模块在 import 期
        就校验 WORKSPACE_PATH env（主进程没设，import 即炸），且锚死 MCP server 的全局
        工作区；这里锚本节点收到的 workspace_path，也**不做沙箱拒绝**——@ 是用户亲手输入的
        通道，沙箱约束的是模型的工具调用，不约束用户自己（截图在桌面/下载是常态）。
        """
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = Path(workspace_path) / p
        return p.resolve()

    @staticmethod
    async def attach_images_to_payload(
        messages: list,
        already_attached: list[ImageRef],
        workspace_path: str,
    ) -> tuple[list, list[ImageRef]]:
        """把最后一条 HumanMessage 里的 @图片引用临时附进**请求体副本**（不动 state）。

        返回 (新消息列表, 本次新附上的 ImageRef)。每个 @token 的处置都写成正文里的状态说明，
        模型自然会把失败转告用户（LLMNode 没有 Notice 通道，让模型当信使）：
        `[图片已附加: …]` / `[图片已在上文发送过…: …]` / `[图片未找到: …]` /
        `[图片过大…: …]` / `[图片读取失败: …]`。base64 只活在本函数的调用栈里；记账由
        调用方写回 state["attached_images"]，下一轮据此不再重发（首次-only 语义；进程重启
        也不怕——记账里的图重读盘即得）。

        只解析 **HumanMessage** 且只是最后一条：AIMessage/ToolMessage 里的 @ 一律不认——
        否则模型输出 `@C:\\…` 就等于模型能指挥宿主读任意本地文件送进 API。
        """
        sent = {ref["path"] for ref in already_attached}
        last_human = max(
            (i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=None
        )
        if last_human is None or not isinstance(messages[last_human].content, str):
            return messages, []
        text = messages[last_human].content
        tokens = LLMNode._image_tokens(text)
        if not tokens:
            return messages, []

        def _load(p: Path):
            if not p.is_file():
                return None
            try:
                return p.read_bytes()
            except OSError:
                return _UNREADABLE

        parts: list[str] = []
        blocks: list[dict] = []
        newly: list[ImageRef] = []
        pos = 0
        for start, end, raw, mime in tokens:
            resolved = LLMNode._resolve_image_path(raw, workspace_path)
            key = str(resolved)
            if key in sent or any(ref["path"] == key for ref in newly):
                marker = f"[图片已在上文发送过，不重复附图: {raw}]"
            elif len(newly) >= _MAX_IMAGES_PER_MESSAGE:
                marker = f"[图片数量超上限（{_MAX_IMAGES_PER_MESSAGE} 张），未附加: {raw}]"
            else:
                data = await asyncio.to_thread(_load, resolved)
                if data is None:
                    marker = f"[图片未找到: {raw}]"
                elif data is _UNREADABLE:
                    marker = f"[图片读取失败: {raw}]"
                elif len(data) > _MAX_IMAGE_BYTES:
                    marker = (
                        f"[图片过大（上限 {_MAX_IMAGE_BYTES // (1024 * 1024)}MB），未附加: {raw}]"
                    )
                else:
                    b64 = base64.b64encode(data).decode()
                    blocks.append(
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
                    )
                    newly.append(ImageRef(path=key, mime=mime))
                    marker = f"[图片已附加: {raw}]"
            parts.append(text[pos:start])
            parts.append(marker)
            pos = end
        parts.append(text[pos:])

        out = list(messages)
        out[last_human] = HumanMessage(
            content=[{"type": "text", "text": "".join(parts)}, *blocks]
        )
        return out, newly

#-------------------工具调用节点-----------------------
class ToolNode:
    """执行已批准的普通 MCP 工具，并把工具返回渲染成 ToolMessage 回传模型。

    工具返回的归一化已抽到 app/agent/utils.py（format_tool_result 产模型可见文本、
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
                results.append(
                    ToolMessage(
                        content=(
                            f"工具不存在或未注册: {tool_name}"
                            f"（不在本会话当前可用的工具表里——可能所属技能刚被卸载、"
                            f"或它的运行体已断开）。若仍需要它，请先用 get_skill 重新加载对应技能，"
                            f"不要原样重试。"
                        ),
                        tool_call_id=tool_call_id,
                        name=tool_name,
                        additional_kwargs={"error_type": "unknown_tool"},
                    )
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
                    extra = {"error_type": tr.error_type or "tool_error"}
            except Exception as exc:
                result_content = f"工具执行失败: {exc}"
                extra = {"error_type": "tool_error"}

            msg_kwargs: dict = {
                "content": result_content,
                "tool_call_id": tool_call_id,
                "name": tool_name,
            }
            if extra:
                msg_kwargs["additional_kwargs"] = extra
            results.append(ToolMessage(**msg_kwargs))

        return {"messages": results, "approved_tool_calls": []}


#----------------------终端命令的免审判定--------------------

# 免审命令的**策略表是数据，不是代码**：加一族命令 = 改 app/agent/command_policy.json，
# 判定逻辑一行不动——这就是"把命令审核从逻辑里解耦出来"的具体含义。
#
# 为什么要这张表：终端工具在 tool.json 里一律 need_review:true，唯独落在表里的形态免人工审批。
# 这是**新增授权**（不是把旧行为平移过来）——git 用法一律走 run_command（原先那批 git_* 原子
# 工具已随 mcp_service/git.py 一并删除），只读的读操作若每次都要人按 y，摩擦就是净增。
_COMMAND_POLICY_PATH = Path(__file__).with_name("command_policy.json")


@lru_cache(maxsize=1)
def _command_policy() -> dict:
    """command_policy.json 的解析结果（整进程只读一次，口径同 tool.json）。

    列表在这里归一成元组/冻结集合——调用方要的是"能 startswith 的序列"和"能 O(1) 判成员的
    集合"，别把裸 list 递给 `str.startswith`（它只认 str 或 tuple）。
    """
    with _COMMAND_POLICY_PATH.open("r", encoding="utf-8") as file:
        raw = json.load(file)
    return {
        "free_commands": {name: frozenset(subs) for name, subs in raw["free_commands"].items()},
        "write_flags": tuple(raw["git_write_flags"]),
        "global_skip": frozenset(raw["git_global_skip"]),
        "branch_list_flags": frozenset(raw["git_branch_list_flags"]),
    }


# cmd.exe 的元字符：出现即不判免审。`;` `&&` `|` `>` 能改写一条命令的作用域与去向，
# `^` 是 cmd 的转义符（可用来拆词绕过白名单）。换行同理（一条命令伪装成两条）。
#
# **这一条刻意留在代码里、不进 JSON**：它是解析器的**词法定义**，不是审批策略。放进数据文件
# 意味着有人能在不改代码的情况下削弱解析器——把 `&&` 从表里删掉，命令拼接的口子就开了。
# 词法事实也没有"按工作区 / 按命令族调"的诉求。
# 注：`%` 是 cmd 的变量展开符，但它同时是 git 格式化串的常客（`--format=%h`），而只读 git
# 命令里它不构成写向量——等 `cat` 那类**按路径判定**的命令进表时，再连它一起处理。
_SHELL_METACHARS: tuple[str, ...] = (";", "&&", "||", "|", "&", "`", "$(", ">", "<", "^", "\n", "\r")


# ---------------------- 提问闸门的参数契约 ----------------------
# 选项上限：面板与人的注意力都有限，"5 个选项比 1 个问题更难答"。开放式提问（0 项）不限。
_MAX_ASK_OPTIONS = 5

# 题目的模态：单选 / 复选。面板据此决定提示语，以及"用户输入多个序号"时是收下还是重问。
_ASK_SELECT_MODES: tuple[str, ...] = ("one", "many")
_ASK_SELECT_DEFAULT = "one"


def _ask_request(args: dict) -> tuple[dict | None, str | None]:
    """校验并归一提问参数，返回 `(归一后的请求, 问题说明)`；无问题时前者为 None。

    **为什么校验在闸门这里**：编排工具不走 langchain 的参数校验管线
    （`OrchestrateNode._invoke` 直接调底层函数，见该处说明），"必填"与"上限"都得自己拦。
    而拦的位置必须是**弹面板之前**——把一个空问题、或 8 个选项摆到人面前，比回一条可行动回执
    让模型自己改要糟得多。
    """
    why = str(args.get("why") or "").strip()
    question = str(args.get("question") or "").strip()
    raw_options = args.get("options")
    options = (
        [str(item).strip() for item in raw_options if str(item).strip()]
        if isinstance(raw_options, list)
        else []
    )
    # 缺省 = 单选（也是历史行为的默认值，模型不传就一切照旧）
    select = str(args.get("select") or _ASK_SELECT_DEFAULT).strip()

    problems: list[str] = []
    if not why:
        problems.append("why（为什么问）为空")
    if not question:
        problems.append("question（问题本身）为空")
    if len(options) > _MAX_ASK_OPTIONS:
        problems.append(f"options 给了 {len(options)} 项，超过上限 {_MAX_ASK_OPTIONS} 项")
    if select not in _ASK_SELECT_MODES:
        # fail-closed：模态说不清就不摆问题（与 why 为空同档），回执教它怎么改
        problems.append(
            f"select 只能是 {' / '.join(repr(m) for m in _ASK_SELECT_MODES)}，收到 {select!r}"
        )
    if problems:
        return None, (
            "提问未发出：" + "；".join(problems) + "。请补齐后重发"
            f"（options 至多 {_MAX_ASK_OPTIONS} 项，也可以留空做开放式提问）。"
        )
    return {"why": why, "question": question, "options": options, "select": select}, None


def _normalize_ask_answer(decision, options: list[str]) -> dict:
    """把闸门收到的回答归一成 `AskAnswer`（写进 state.ask_answers 的形状）。

    - **序号按 options 逐个校验**：越界的、非整数的**丢掉那一项**，保留其余合法的，并去重保序
      （单选与复选走同一段代码，单选只是长度 0 或 1 的特例）。取向同 `_decide` 的 fail-closed
      ——宁可让模型看到"用户没选这几项"，也不能把对不上号的序号当成有效选择喂过去。
      面板侧在输入时就提示过越界（单选直接重问），所以这里**不把丢弃项回灌给模型**；
    - 补充去首尾空白，空串归一成 None——让"**两段皆空 = 未回答**"这条判据在 state 里直接可读，
      消费端不必各自再判一次空白。
    """
    payload = decision if isinstance(decision, dict) else {}
    raw = payload.get("option_indexes")
    indexes: list[int] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, int) and 0 <= item < len(options) and item not in indexes:
            indexes.append(item)
    supplement = str(payload.get("supplement") or "").strip() or None
    return {"option_indexes": indexes, "supplement": supplement}


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

    与 ToolNode/OrchestrateNode 共用同一份 tool.json（_tool_config 整进程缓存）。
    """

    # "state 工具"在 tool.json 的 source 取值集合：命中即分流进 approved_orchestrate_calls，
    # 交 OrchestrateNode（通用 state 工具执行器）处理。plan=编排；notes=笔记读回；
    # ask=向用户提问（见下 ASK_SOURCE 的说明）；
    # dispatch=并发资料收集派发（app/agent/tools.py::dispatch_subtasks）；
    # memory=长期记忆读写（同文件 write_memory/read_memory，靠注入的 workspace 定位记忆文件）；
    # skill=技能加载/卸载（同文件 get_skill/drop_skill）。
    # 注意：技能**带来**的工具不走这里——它们的 source 取 `skills/<name>`，不命中本集合 → 进普通
    # 工具队列。这正是想要的：get_skill 是编排动作，它带来的工具不是。
    ORCHESTRATE_SOURCES: frozenset[str] = frozenset(
        {"plan", "notes", "ask", "dispatch", "memory", "skill"}
    )

    # 人机闸门的第二种载荷（docs/MULTI_AGENT.md §6）：source=ask 的工具（当前只有 ask_user）
    # **除了走编排执行器，还要在本节点再挂一次 interrupt 向人提问**。分流进编排队列照旧——
    # 回答经 state 的 ask_answers 交给工具体渲染成回执，见下面 __call__ 的 ASK 分支。
    ASK_SOURCE = "ask"

    @classmethod
    @lru_cache(maxsize=1)
    def _tool_config(cls) -> dict[str, dict]:
        """tool.json 解析结果：{tool_name: {need_review, source}}。整进程只读一次。"""
        with _TOOL_CONFIG_PATH.open("r", encoding="utf-8") as file:
            return json.load(file)

    def __init__(self, log: bool = True, toolset=None, allow_ask: bool = True) -> None:
        """log=False 供 stdio MCP server 型 worker 使用（stdout 即 JSON-RPC 协议，不能 print）。

        toolset：给了才做"存在性 + 漏登记"那道防线（主图给；worker 与单测不给 → 行为与改造前一致）；
        allow_ask=False 供 worker 用：**本图不承载提问载荷**（见 __call__ 的 ASK 分支）。
        """
        self.tool_review = self._tool_config()
        self.log = log
        self.toolset = toolset
        self.allow_ask = allow_ask

    def _known_names(self, state: AgentState) -> set[str] | None:
        """当前可用的工具名（含编排工具）；无 toolset（worker / 单测）→ None = 不判存在性。"""
        if self.toolset is None:
            return None
        return self.toolset.known_names(state)

    @staticmethod
    def _free_shell_verdict(tool_args: dict) -> bool:
        """run_command 的免审判定：这条命令是名单里的**只读**形态 → True（不弹审批面板）。

        保守优先（fail-closed）：拿不准一律 False，照常弹面板让人看一眼——误判成 review 只是多
        按一次 y，误判成 free 却是静默放行一次没人看过的命令。判定链（全过才 True）：
        ① 命令文本不含 cmd 元字符；② 能切分出词；③ 首词在免审命令表里；
        ④ 跳过 git 全局选项后子命令在白名单；⑤ 无写文件选项；⑥ branch 只认列分支用法。

        ③–⑥ 的口径全部来自 `app/agent/command_policy.json`（经 `_command_policy()`）——加一族
        命令是改那张表，不是改本函数。①（元字符）刻意留在代码里，理由见 `_SHELL_METACHARS`。
        """
        policy = _command_policy()
        command = tool_args.get("command")
        if not isinstance(command, str) or not command.strip():
            return False
        if any(char in command for char in _SHELL_METACHARS):
            return False
        try:
            # posix=False 是**必须的**：终端跑在 cmd.exe 上，POSIX 规则会把 `C:\work\a` 的
            # 反斜杠当转义符吃掉，切出与实际执行不同的词串——而"解析结果 ≠ 实际执行的命令"
            # 正是这类判定最不能有的性质。
            tokens = shlex.split(command, posix=False)
        except ValueError:  # 引号不闭合等
            return False
        if not tokens:
            return False

        head = tokens[0].strip("\"'").lower()
        allowed = policy["free_commands"].get(head)
        if allowed is None:
            return False

        # 找子命令：跳过带值的全局选项（`-C <dir>` 占两个词）
        index = 1
        while index < len(tokens):
            token = tokens[index].strip("\"'")
            if token in policy["global_skip"]:
                index += 2 if token == "-C" else 1
                continue
            break
        if index >= len(tokens):
            return False
        subcommand = tokens[index].strip("\"'")
        if subcommand not in allowed:
            return False

        rest = [token.strip("\"'") for token in tokens[index + 1 :]]
        if any(token.startswith(policy["write_flags"]) for token in rest):
            return False
        if subcommand == "branch":
            # 只认列分支：不能有位置实参，选项也得在列表 flag 白名单里（挡住 -d/-D/-m/-c …）
            return all(token in policy["branch_list_flags"] for token in rest)
        return True

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
          run_command），**还要它的参数**判为只读终端命令才算数，见 `_free_shell_verdict`。
          判定不过就落回 review —— 这条路是加宽免审面的唯一入口，失败方向必须是收紧。
        - **review**：`tool.json` 说要审批；**或**它在工具表里却没登记 —— 这条是 §13.4 的第二道防线
          （fail-closed）：有人往 MCP server 加了个写工具却忘了登记，旧行为是"未登记 = 免审"，
          等于静默放行一次写操作。限定在"在表里"是为了不误伤模型编出来的名字。
        - **free**：登记过且 `need_review: false`。
        """
        if tool_name is None:
            return "free"
        known = self._known_names(state)
        if known is not None and tool_name not in known:
            return "unknown"
        if tool_cfg.get("free_when") == "readonly_shell" and self._free_shell_verdict(tool_args or {}):
            return "free"
        if tool_cfg.get("need_review"):
            return "review"
        if known is not None and tool_name not in self.tool_review:
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
                    additional_kwargs={"error_type": error_type},
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

    async def __call__(self, state: AgentState) -> dict | AgentState:
        pending = list(state.get("pending_tool_calls", []))
        if not pending:
            return {"pending_tool_calls": []}

        current_tool = pending[0]
        tool_name = current_tool.get("name")
        if self.log:
            # 工具调用日志：每条待审调用过审核节点时打一行（含需要人工审批与免审放行者）
            print(_tool_call_log(current_tool))
        tool_cfg = self.tool_review.get(tool_name, {}) if tool_name else {}
        approved = True
        denied_messages: list[ToolMessage] = []

        action = self._decide(tool_name, tool_cfg, state, current_tool.get("args", {}))
        if action == "unknown":
            # 模型编出来的名字（本会话根本没绑定过它）：回执而不弹面板，见 _decide 的说明
            return self._unknown_result(state, current_tool)

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
                        additional_kwargs={"decision": "denied"},
                    )
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
            request, problem = _ask_request(current_tool.get("args") or {})
            if problem is not None:
                return self._receipt_result(state, current_tool, problem, "invalid_ask")
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
            ask_answers[current_tool.get("id")] = _normalize_ask_answer(
                decision, request["options"]
            )

        # 按 tool.json 的 source 分流：编排类进 orchestrate 队列，其余进普通工具队列
        is_orchestrate = tool_cfg.get("source") in self.ORCHESTRATE_SOURCES

        approved_orchestrate = list(state.get("approved_orchestrate_calls", []))
        approved_calls = list(state.get("approved_tool_calls", []))
        if approved:
            (approved_orchestrate if is_orchestrate else approved_calls).append(current_tool)

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

# 编排工具可以写回的"额外" state 切片（current_plan 不在此列——它有链式语义、单独处理，
# 且最终返回恒带上它）。**加新切片只改这一行 + state.py**，别再去 __call__ 里加分支。
_EXTRA_SLICE_KEYS: tuple[str, ...] = ("loaded_skills",)


class OrchestrateNode:
    """消费编排类调用（approved_orchestrate_calls），执行编排工具并合并写回 state。

    编排工具（app/agent/tools.py 的 orchestrate_tool）与普通工具的关键区别：
    不产生真实副作用，返回的是"要写回 state 的切片"——`{current_plan?, messages}`
    其中 messages 是工具已经拼好的 ToolMessage 回执。因此本节点不像 ToolNode 那样
    `ainvoke` 后 format_tool_result，而是：

    - 逐个调用编排工具的底层原函数，显式注入 `state`（整份 AgentState）与
      `tool_call_id`（本次调用 id）——这两个参数用 InjectedState / InjectedToolCallId
      标注、对模型隐藏，由本节点代填；
    - 同回合多次编排调用按顺序执行，并把上一步写回的 `current_plan` 链式传给下一步
      （如 create_plan → update_plan_step → clear_plan 依次生效）；
    - 把工具返回的 ToolMessage 原样并入 messages 交回模型；工具失败/未注册时生成
      对应的错误 ToolMessage，且不改动 current_plan；
    - 结束后清空 approved_orchestrate_calls；不动 approved_tool_calls（留给 tool_node）。
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
                messages.append(
                    ToolMessage(
                        content=f"工具不存在或未注册: {tool_name}",
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    )
                )
                continue

            try:
                result = self._invoke(
                    tool_obj, tool_args, working, tool_call_id, self.workspace_path
                )
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                messages.append(
                    ToolMessage(
                        content=f"工具执行失败: {exc}",
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    )
                )
                continue

            if not isinstance(result, dict):
                messages.append(
                    ToolMessage(
                        content=f"工具返回格式非法: {type(result).__name__}",
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    )
                )
                continue

            # 工具返回切片：链式更新进 working（同回合后续调用看得到），messages 回执原样收集
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
# 折叠预算默认值：messages content 总字符数超过它才触发折叠。
# 由 CompactNode(content_budget_chars=…) 构造参数覆盖（默认取此值），实例化时可调。
_CONTEXT_BUDGET_DEFAULT = 60000

# 归档正文截断上限（字符）
_NOTE_MAX = 8000


def needs_compact(state, budget: int) -> bool:
    """预算门：历史 content 字符总数超过 budget 才需要触发压缩。

    graph 的 llm 收尾路由与 CompactNode.__call__ 共用同一计量口径。
    """
    total = sum(len(str(getattr(m, "content", "") or "")) for m in (state.get("messages") or []))
    return total > budget


class CompactNode:
    """轮末压缩节点（END 前执行）：把更早轮次已消费的旧工具块折叠成一条 SystemMessage。

    - 预算未超 → 直通不动（幂等、零成本）；
    - 折叠区 = 最后一条 HumanMessage 之前（含更早轮次）；当前轮（Human 及其后）原样保留；
    - 只折"完整工具块"（AIMessage(tool_calls) + 兑现它的全部 ToolMessages），状态据
      ToolMessage 判定；联网四工具的成功正文归档进 notes；
    - 返回：`SystemMessage(摘要, id=折叠区最早被删消息的 id)` 原位替换 + `RemoveMessage`
      删除其余被折块——不尾追加，messages 尾部仍是本轮最终答复（main.py 打印不受影响）。

    纯确定性逻辑，不调 LLM。压缩的内部纯函数只被本节点使用，故收成静态方法，不外泄。
    """
    _FOLD_HEADER = "# 历史工具记录（已折叠）"
    # 已知失败标记（ToolNode / ReviewNode 注入 content 的前缀；供无戳旧消息回退判定）
    _FAIL_PREFIXES: tuple[str, ...] = (
        "[approval_denied]", "[workspace_violation]", "[io_error]", "[internal_error]",
        "[invalid_argument]", "[config_error]",
    )
    # 进笔记区的工具：仅联网四件套——其结果花钱、不可免费重取（时效性/可重取性判据）；
    # 其余工具（文件读/命令输出/git/终端）结果时效强、重取便宜，折叠即弃、绝不进笔记。
    ARCHIVE_TOOLS: frozenset[str] = frozenset(
        {"web_search", "extract_urls", "crawl_website", "deep_research"}
        )

    def __init__(self, content_budget_chars: int = _CONTEXT_BUDGET_DEFAULT) -> None:
        """构造参数：messages content 总字符数超过它才触发折叠（默认 60000）。"""
        self.content_budget_chars = content_budget_chars

    async def __call__(self, state: AgentState) -> dict | AgentState:
        messages = list(state.get("messages") or [])
        if not messages:
            return {}

        if not needs_compact(state, self.content_budget_chars):
            return {}

        notes = dict(state.get("notes") or {})
        removed, lines, additions = self._plan(messages, notes)
        if not removed or not lines:
            return {}

        summary = CompactNode._FOLD_HEADER + "\n" + "\n".join(lines)
        anchor = removed[0]
        updates: list = [SystemMessage(content=summary, id=anchor.id)]
        updates += [RemoveMessage(id=m.id) for m in removed[1:] if getattr(m, "id", None)]

        result: dict = {"messages": updates}
        if additions:
            notes.update(additions)
            result["notes"] = notes
        return result

    # ---------- 压缩内部纯函数（只被本节点单方使用 → 静态方法，保证职责单一） ----------

    @staticmethod
    def _short(text: str, limit: int = 60) -> str:
        """取多行文本首行、超长截断——用于摘要行的紧凑 detail。"""
        line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
        return line if len(line) <= limit else line[:limit] + "…"

    @staticmethod
    def _tm_status(message) -> tuple[str, str]:
        """从一条 ToolMessage 判定其工具块状态。

        优先读 additional_kwargs 的结构化戳（ToolNode/ReviewNode 生产端已挂）：
        - decision=denied → denied（未执行）；
        - error_type=… → failed（该类型）。
        未挂戳的旧消息再回退解析 content 前缀（[approval_denied] / [error_type]…）。
        判定只信工具回执，绝不信模型 content。
        """
        extra = getattr(message, "additional_kwargs", None) or {}
        if extra.get("decision") == "denied":
            return "denied", "未执行"
        error_type = extra.get("error_type")
        if error_type:
            return "failed", str(error_type)

        # 回退：解析 content 前缀（兼容戳落地前 checkpoint 里的历史消息）
        text = str(getattr(message, "content", "") or "").strip()
        if not text:
            return "ok", ""
        if text.startswith("[approval_denied]"):
            return "denied", "未执行"
        for tag in CompactNode._FAIL_PREFIXES[1:]:
            if text.startswith(tag):
                return "failed", tag.strip("[]")
        if text.startswith(("工具不存在或未注册", "工具执行失败", "工具返回格式非法")):
            return "failed", CompactNode._short(text)
        return "ok", ""

    @staticmethod
    def _notes_kind(tool: str) -> str:
        return {
            "web_search": "web",
            "extract_urls": "extract",
            "crawl_website": "crawl",
            "deep_research": "research",
        }.get(tool, "web")

    @staticmethod
    def _render_line(reason: str, tool: str, status: str, detail: str = "", note_ref: str = "") -> str:
        """拼一条摘要行：工作日志 → 工具调用 → 状态（→ 详情见 notes）。"""
        parts: list[str] = []
        if reason:
            parts.append(f"工作日志: {reason}")
        if tool:
            parts.append(f"工具调用: {tool}")
        state_part = f"状态: {status}"
        if detail:
            state_part += f"（{detail}）"
        parts.append(state_part)
        line = " | ".join(parts)
        if note_ref:
            line += f" | 详情见 {note_ref}"
        return line

    @staticmethod
    def _reason_of(ai) -> str:
        """取块内 AIMessage 的 content（prompt 引导模型写成一句话工作日志），非 str 视为空。"""
        content = getattr(ai, "content", None)
        return content.strip() if isinstance(content, str) else ""

    @staticmethod
    def _first_line(text: str, limit: int = 80) -> str:
        """取文本首行用于 notes 标题。"""
        line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
        return line if len(line) <= limit else line[:limit] + "…"

    @staticmethod
    def _new_note_ref(notes: dict) -> tuple[str, int]:
        """下一个笔记 key（notes#r<n>）与其编号，取现有 key 最大号 +1。"""
        max_n = 0
        for key in notes:
            if isinstance(key, str) and key.startswith("notes#r"):
                try:
                    max_n = max(max_n, int(key[len("notes#r"):]))
                except ValueError:
                    pass
        return f"notes#r{max_n + 1}", max_n + 1

    @staticmethod
    def _plan(
        messages: list, notes: dict
    ) -> tuple[list, list[str], dict[str, NoteEntry]]:
        """扫描消息，决定折叠哪些"更早轮次、已消费的完整工具块"，并生成摘要行与新增 notes。

        返回 (removed, summary_lines, archived)：
        - removed：可整体删除的块内消息（AIMessage(tool_calls) + 兑现它的 ToolMessages），
          按消息顺序排列，removed[0] 即折叠区最早被删消息（将作为 SystemMessage 原位替换锚点）；
        - summary_lines：每个被折 tool_call 一行（reason · 工具 · 状态 · 指向）；
        - archived：本次新增的 notes（key=notes#r<n>，仅联网四工具的成功正文）。

        不折叠：无 tool_calls 的 AIMessage（答复）、Human、SystemMessage、块不完整（有悬空
        call）、块内消息缺稳定 id、以及折叠区为空（单轮会话）。
        """
        # 锚点 = 最后一条 HumanMessage；它及其后（当前轮）原样保留，只折更早轮次
        anchor = -1
        for i, m in enumerate(messages):
            if isinstance(m, HumanMessage):
                anchor = i
        if anchor <= 0:
            return [], [], {}
        region = messages[:anchor]

        removed: list = []
        lines: list[str] = []
        archived: dict[str, NoteEntry] = {}
        ref_num: int | None = None  # 首次归档时初始化为现有最大号 +1

        i = 0
        n = len(region)
        while i < n:
            m = region[i]
            if not (isinstance(m, AIMessage) and getattr(m, "tool_calls", None)):
                i += 1
                continue

            call_ids = [tc.get("id") for tc in m.tool_calls if isinstance(tc, dict)]
            block: list = [m]
            claimed: dict[str, ToolMessage] = {}
            j = i + 1
            # 收集紧随其后、兑现本块各 tool_call 的 ToolMessage
            while j < n:
                nxt = region[j]
                if (
                    isinstance(nxt, ToolMessage)
                    and nxt.tool_call_id in call_ids
                    and nxt.tool_call_id not in claimed
                ):
                    claimed[nxt.tool_call_id] = nxt
                    block.append(nxt)
                    j += 1
                else:
                    break
            i = j  # 游标越过本块 ToolMessage 段

            complete = len(claimed) == len(call_ids) and all(cid is not None for cid in call_ids)
            # 原位替换要求每条待删消息都有稳定 id（生产 add_messages 自动赋 uuid）
            if not (complete and all(getattr(b, "id", None) for b in block)):
                continue

            reason = CompactNode._reason_of(m)
            for tc in m.tool_calls:
                name = (tc.get("name") or "").strip()
                tm = claimed.get(tc.get("id"))  # type: ignore
                status, detail = CompactNode._tm_status(tm) if tm is not None else ("ok", "")

                note_ref = ""
                # 仅联网四工具的成功正文进笔记区（其余工具结果时效强、可重取，折叠即弃）
                if name in CompactNode.ARCHIVE_TOOLS and status == "ok" and tm is not None:
                    content = str(tm.content or "").strip()
                    if content and not content.startswith("["):
                        if ref_num is None:
                            _, ref_num = CompactNode._new_note_ref(notes)
                        ref = f"notes#r{ref_num}"
                        content_cap = content if len(content) <= _NOTE_MAX else content[:_NOTE_MAX] + "…"
                        archived[ref] = {
                            "kind": CompactNode._notes_kind(name),
                            "title": CompactNode._first_line(content),
                            "source_tool": name,
                            "content": content_cap,
                            "created": int(time.time()),
                            "size": len(content),
                        }
                        note_ref = ref
                        ref_num += 1

                lines.append(CompactNode._render_line(reason, name, status, detail, note_ref))
            removed.extend(block)

        return removed, lines, archived
