import json
from typing import Any, Callable
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.types import interrupt

from app.agent.state import AgentState

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
                result_content = str(result_value)
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
        tool_config_path = Path(__file__).with_name("tool.json")
        with tool_config_path.open("r", encoding="utf-8") as file:
            self.tool_review = json.load(file)

    async def __call__(self, state: AgentState) -> dict | AgentState:
        pending = list(state.get("pending_tool_calls", []))
        if not pending:
            return {"pending_tool_calls": []}

        current_tool = pending[0]
        tool_name = current_tool.get("name")
        approved = True

        if tool_name and self.tool_review.get(tool_name, {}).get("need_review"):
            payload = {
                "type": "tool_approval",
                "current_step": "1/1",
                "tool_name": tool_name,
                "tool_args": current_tool.get("args", {}),
                "tool_call_id": current_tool.get("id"),
            }
            decision = interrupt(payload)
            approved = isinstance(decision, dict) and decision.get("approved", False)

        approved_calls = list(state.get("approved_tool_calls", []))
        if approved:
            approved_calls.append(current_tool)

        remaining = pending[1:]

        return {
            "pending_tool_calls": remaining,
            "approved_tool_calls": approved_calls,
        }


#----------------------审核队列处理节点-------------------

async def queue_node(state: AgentState) -> dict | AgentState:
    tool_calls = getattr(state["messages"][-1], "tool_calls", None) or []
    return {
        "pending_tool_calls": tool_calls,
        "approved_tool_calls": [],
    }