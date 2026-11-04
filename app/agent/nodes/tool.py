"""ToolNode：执行已批准的 MCP 工具，并把 ToolMessage 写回 state。"""
import asyncio
from typing import Callable

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

from app.agent.state import AgentState
from app.platform.tool_results import coerce_tool_result, format_tool_result
from app.resource import event_log
from ._shared import _tool_stamp


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


