import inspect
from inspect import signature
from typing import Any, Callable

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.types import interrupt

from app.agent.state import AgentState
from app.agent.utils import (
    format_tool_result,
    get_tool_config,
    ORCHESTRATE_SOURCES,
)

#-------------------大模型节点----------------------
class LLMNode:
    def __init__(self, model: Runnable) -> None:
        self.model: Runnable = model

    async def __call__(self, state: AgentState) -> dict | AgentState:
        """大模型处理节点"""
        messages = state["messages"]
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
        # 与 ToolNode/OrchestrateNode 共用同一份 tool.json（缓存加载见 app/agent/utils.py）
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
