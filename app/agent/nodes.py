import inspect
import json
from functools import lru_cache
from inspect import signature
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.types import interrupt
from app.agent.state import AgentState
from app.agent.prompt import SYSTEM_PROMPT, workspace_context_block
from app.schema.agent_schema import PlanStep, ToolResult

_TOOL_CONFIG_PATH = Path(__file__).with_name("tool.json")

# 编排类工具在 tool.json 中的 source 取值集合：命中即视为编排调用，
# 由 ReviewNode 分流进 approved_orchestrate_calls，交 OrchestrateNode 处理。
ORCHESTRATE_SOURCES: frozenset[str] = frozenset({"plan"})

#-------------------大模型节点----------------------
class LLMNode:
    def __init__(
        self,
        model: Runnable,
        workspace_path: str | None = None,
    ) -> None:
        self.model: Runnable = model
        self.workspace_path: str | None = workspace_path

    async def __call__(self, state: AgentState) -> dict | AgentState:
        """大模型处理节点。

        每次生成都在头部拼接系统提示词：行为契约 SYSTEM_PROMPT（见 prompt.py）+
        本次会话的具体工作区上下文 workspace_context_block（若有，含工作区根目录与
        终端 cwd 差异提醒）+ 当前计划的状态回显（若有）。只构造成临时列表传给模型，
        不改动 state["messages"]（LangGraph state 应不可变更新；系统消息也不应渗入
        历史被持久化）。
        """
        system_messages = [SystemMessage(content=SYSTEM_PROMPT)]
        if self.workspace_path:
            system_messages.append(
                SystemMessage(content=workspace_context_block(self.workspace_path))
            )
        plans = state.get("current_plan", [])
        if plans:
            system_messages.append(
                SystemMessage(
                    content="# 当前任务:\n"
                    + "\n".join(format_plan_status(plan) for plan in plans)
                )
            )

        messages = [*system_messages, *state["messages"]]
        res = await self.model.ainvoke(messages)
        return {"messages": [res]}

#-------------------工具调用节点-----------------------
class ToolNode:
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

            if not tool_name or tool_name not in self.tools_map:
                results.append(
                    ToolMessage(
                        content=f"工具不存在或未注册: {tool_name}",
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    )
                )
                continue

            tool_obj = self.tools_map[tool_name]
            try:
                # MCP 工具是 async-only(只实现了 coroutine)，必须用 ainvoke
                # (同步 invoke 会抛 "StructuredTool does not support sync invocation")
                result_value = await tool_obj.ainvoke(tool_args)
                result_content = format_tool_result(result_value)
            except Exception as exc:
                result_content = f"工具执行失败: {exc}"

            results.append(
                ToolMessage(
                    content=result_content,
                    tool_call_id=tool_call_id,
                    name=tool_name,
                )
            )

        return {"messages": results, "approved_tool_calls": []}


#----------------------工具审核节点--------------------
class ReviewNode:
    def __init__(self) -> None:
        # 与 ToolNode/OrchestrateNode 共用同一份 tool.json（缓存加载见本文件 get_tool_config）
        self.tool_review = get_tool_config()

    async def __call__(self, state: AgentState) -> dict | AgentState:
        pending = list(state.get("pending_tool_calls", []))
        if not pending:
            return {"pending_tool_calls": []}

        current_tool = pending[0]
        tool_name = current_tool.get("name")
        tool_cfg = self.tool_review.get(tool_name, {}) if tool_name else {}
        approved = True

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

        # 按 tool.json 的 source 分流：编排类进 orchestrate 队列，其余进普通工具队列
        is_orchestrate = tool_cfg.get("source") in ORCHESTRATE_SOURCES

        approved_orchestrate = list(state.get("approved_orchestrate_calls", []))
        approved_calls = list(state.get("approved_tool_calls", []))
        if approved:
            (approved_orchestrate if is_orchestrate else approved_calls).append(current_tool)

        remaining = pending[1:]

        return {
            "pending_tool_calls": remaining,
            "approved_orchestrate_calls": approved_orchestrate,
            "approved_tool_calls": approved_calls,
        }


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

    def __init__(self, orchestrate_tools: list[Callable]) -> None:
        self.tools_map = {}
        for tool_obj in orchestrate_tools:
            name = getattr(tool_obj, "name", None)
            if name:
                self.tools_map[name] = tool_obj

    @staticmethod
    def _invoke(tool_obj: Callable, tool_args: dict, state:dict|AgentState, tool_call_id: str):
        """调用编排工具的底层原函数，按签名注入 state / tool_call_id。

        编排工具为 langchain @tool 包装的普通同步函数，`tool_obj.func` 即原函数；
        直接以关键字调用，跳过 langchain 的注入管线（与 tests/test_plan_tools.py 一致）。
        """
        fn = getattr(tool_obj, "func", tool_obj)
        kwargs = dict(tool_args)
        # 只向签名里确实声明了对应注入参数的编排工具注入（覆写模型可能伪造的同名键）
        params = signature(fn).parameters
        if "state" in params:
            kwargs["state"] = state
        if "tool_call_id" in params:
            kwargs["tool_call_id"] = tool_call_id

        # 返回可能为 coroutine（若未来编排工具改为 async），由调用方 await
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
                result = self._invoke(tool_obj, tool_args, working, tool_call_id)
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

async def queue_node(state: AgentState) -> dict | AgentState:
    tool_calls = getattr(state["messages"][-1], "tool_calls", None) or []
    return {
        "pending_tool_calls": tool_calls,
        "approved_tool_calls": [],
        "approved_orchestrate_calls": []
    }


#----------------------节点共享辅助（只被本模块使用）----------------------

@lru_cache(maxsize=1)
def get_tool_config() -> dict[str, dict]:
    """返回 tool.json 解析结果：{tool_name: {need_review, source}}。整进程只读一次。"""
    with _TOOL_CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def format_tool_result(result) -> str:
    """把工具返回统一格式化为模型可见的字符串。

    处理顺序：
    - `ToolResult`：取 `content`，若有 `error_type` 则加 `[error_type] ` 前缀，
      让模型一眼看出这是哪一类失败（如 "[workspace_violation] 路径…"）。
    - `dict`：优先取 `content` 字段；若 content 是 content-block 列表则逐个取
      `text` 拼接（兼容 langchain MCP 适配器的返回形态）。
    - `list`：MCP 工具经 ToolNode 拿到的真实形态——langchain MCP 适配器把每个
      content block 转成 dict（text/id 等），FastMCP 又把工具返回的 ToolResult
      整包 JSON 化进 text。这里逐个取 `text`（丢弃随机 id 噪声），json.loads
      还原 ToolResult 后交上面 ToolResult 分支；还原失败（非 JSON / 非
      ToolResult 形态，如未来直接返回 str 的工具）则拼接后原样透传。
    - 其它（str 等）：`str()` 原样透传。

    注意：模型读到的文本由这里决定，而不是 Python 侧的 `ToolResult.__str__`
    （MCP 跨进程返回时走的是 FastMCP 的 JSON 序列化，见 mcp_service/utils.py 注释）。
    """
    if isinstance(result, ToolResult):
        prefix = f"[{result.error_type}] " if result.error_type else ""
        return prefix + result.content

    if isinstance(result, dict):
        content = result.get("content", result)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(block.get("text") or block.get("content") or "")
                else:
                    parts.append(str(block))
            return "\n".join(part for part in parts if part)
        return str(content)

    if isinstance(result, list):
        parts: list[str] = []
        for block in result:
            if isinstance(block, dict):
                parts.append(block.get("text") or block.get("content") or "")
            else:
                parts.append(str(block))
        text = "\n".join(part for part in parts if part)
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
        if isinstance(data, dict) and "success" in data and isinstance(data.get("content"), str):
            return format_tool_result(ToolResult(**data))
        return text

    return str(result)


def format_plan_status(status: PlanStep) -> str:
    """把 plan 状态转换为易读的字符串。"""
    status_map = {
        "pending": "待处理",
        "in_progress": "进行中",
        "done": "已完成",
    }
    if not isinstance(status, dict):
        raise ValueError("status 必须是 PlanStatus 类型")

    status_value = status.get("status")
    if not isinstance(status_value, str):
        raise ValueError("status['status'] 必须是字符串")

    task = status.get("task")
    task_str = task if isinstance(task, str) else "未知状态"
    status_id = status.get("id")
    status_id_str = status_id if isinstance(status_id, str) else "未知状态"

    state = status_map.get(status_value, "错误")
    return f"{status_id_str}. {task_str} 当前状态为 {state}"
