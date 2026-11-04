"""OrchestrateNode：执行只操作会话 state 的编排工具。"""
import asyncio
import inspect
from inspect import signature
from typing import Callable

from langchain_core.messages import ToolMessage

from app.agent.state import AgentState
from app.resource import event_log


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
# **权威为什么在消费端（本节点）而不是生产端**：两者分居 `app/agent/nodes/` / `tools.py`，而
# `app/agent/nodes/` **刻意不 import `tools.py`**（它拿的是构图期传进来的工具对象，见本文件顶部对
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


