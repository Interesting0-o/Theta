import asyncio
import inspect
import json
import time
from functools import lru_cache
from inspect import signature
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.types import interrupt
from app.agent.memory import memory_block
from app.agent.state import AgentState
from app.agent.utils import coerce_tool_result, format_tool_result
from app.agent.prompt import SYSTEM_PROMPT, workspace_context_block
from app.schema.agent_schema import NoteEntry, PlanStep

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
class LLMNode:
    def __init__(
        self,
        model: Runnable,
        workspace_path: str | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        inject_memory: bool = True,
    ) -> None:
        self.model: Runnable = model
        self.workspace_path: str | None = workspace_path
        self.system_prompt: str = system_prompt
        # worker（子 agent）传 False：它是临时资料收集器，不读写记忆、保持只读隔离
        # （docs/LONG_TERM_MEMORY.md §4）。
        self.inject_memory: bool = inject_memory

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
        终端 cwd 差异提醒）+ 当前计划的状态回显（若有）+ **长期记忆**（若有条目，
        `# 长期记忆（工作区 …）` 正文，见 docs/LONG_TERM_MEMORY.md §4）。只构造成临时列表
        传给模型，不改动 state["messages"]（LangGraph state 应不可变更新；系统消息也不应
        渗入历史被持久化）。

        记忆**每轮实时读盘**（记忆文件即真值，不做缓存）；读盘是阻塞调用，走 to_thread 以
        避开 langgraph dev 的 blockbuster。
        """
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
        if self.inject_memory and self.workspace_path:
            block = await asyncio.to_thread(memory_block, self.workspace_path)
            if block:
                system_messages.append(SystemMessage(content=block))

        messages = [*system_messages, *state["messages"]]
        res = await self.model.ainvoke(messages)
        return {"messages": [res]}

#-------------------工具调用节点-----------------------
class ToolNode:
    """执行已批准的普通 MCP 工具，并把工具返回渲染成 ToolMessage 回传模型。

    工具返回的归一化已抽到 app/agent/utils.py（format_tool_result 产模型可见文本、
    coerce_tool_result 还原结构化 ToolResult 供打戳，ToolNode 与 dispatch 共用），
    本节点只负责 ainvoke 执行已批准工具 + 拼 ToolMessage。
    """

    def __init__(self, tools: list[Callable] | list[BaseTool]) -> None:
        self.tools_map = {}

        for tool_obj in tools:
            name = getattr(tool_obj, "name", None)
            if name:
                self.tools_map[name] = tool_obj

    async def __call__(self, state: AgentState) -> dict | AgentState:
        tool_calls = list(state.get("approved_tool_calls", []))

        results: list[ToolMessage] = []

        for tool_call in tool_calls:
            tool_name = tool_call.get("name")
            tool_args = tool_call.get("args", {})
            tool_call_id = tool_call.get("id", "unknown")

            extra: dict = {}
            if not tool_name or tool_name not in self.tools_map:
                results.append(
                    ToolMessage(
                        content=f"工具不存在或未注册: {tool_name}",
                        tool_call_id=tool_call_id,
                        name=tool_name,
                        additional_kwargs={"error_type": "unknown_tool"},
                    )
                )
                continue

            tool_obj = self.tools_map[tool_name]
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


#----------------------工具审核节点--------------------
class ReviewNode:
    """逐条审批 pending 工具调用，按 tool.json 分流进编排/普通执行队列。

    与 ToolNode/OrchestrateNode 共用同一份 tool.json（_tool_config 整进程缓存）。
    """

    # "state 工具"在 tool.json 的 source 取值集合：命中即分流进 approved_orchestrate_calls，
    # 交 OrchestrateNode（通用 state 工具执行器）处理。plan=编排；notes=笔记读回；
    # dispatch=并发资料收集派发（app/agent/tools.py::dispatch_subtasks）；
    # memory=长期记忆读写（同文件 write_memory/read_memory，靠注入的 workspace 定位记忆文件）。
    ORCHESTRATE_SOURCES: frozenset[str] = frozenset({"plan", "notes", "dispatch", "memory"})

    @classmethod
    @lru_cache(maxsize=1)
    def _tool_config(cls) -> dict[str, dict]:
        """tool.json 解析结果：{tool_name: {need_review, source}}。整进程只读一次。"""
        with _TOOL_CONFIG_PATH.open("r", encoding="utf-8") as file:
            return json.load(file)

    def __init__(self, log: bool = True) -> None:
        """log=False 供 stdio MCP server 型 worker 使用（stdout 即 JSON-RPC 协议，不能 print）。"""
        self.tool_review = self._tool_config()
        self.log = log

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

        if tool_name and tool_cfg.get("need_review"):
            payload = {
                "type": "tool_approval",
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
        if denied_messages:
            result["messages"] = denied_messages
        return result


#----------------------编排节点----------------------
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

            # 工具返回切片：有 current_plan 则链式更新；messages 回执原样收集
            if "current_plan" in result:
                working["current_plan"] = result["current_plan"]
            for block in result.get("messages", []) or []:
                if isinstance(block, ToolMessage):
                    messages.append(block)
                else:
                    messages.append(
                        ToolMessage(content=str(block), tool_call_id=tool_call_id, name=tool_name)
                    )

        return {
            "approved_orchestrate_calls": [],
            "messages": messages,
            "current_plan": working["current_plan"],
        }


#----------------------审核队列处理节点-------------------
class QueueNode:
    """
    
    """
    def __init__(self) -> None:
        pass 

    async def __call__(self,state: AgentState) -> dict | AgentState:
        tool_calls = getattr(state["messages"][-1], "tool_calls", None) or []
        return {
            "pending_tool_calls": tool_calls,
            "approved_tool_calls": [],
            "approved_orchestrate_calls": []
        }


#----------------------轮末压缩（compact_node）----------------------
# 折叠预算默认值：messages content 总字符数超过它才触发折叠。
# 由 CompactNode(content_budget_chars=…) 构造参数覆盖（默认取此值），实例化时可调。
_CONTEXT_BUDGET_DEFAULT = 60000

# 归档正文截断上限（字符）
_NOTE_MAX = 8000
# 摘要 SystemMessage 的标题头



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
