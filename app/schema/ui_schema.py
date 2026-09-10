"""基座 → 前端的**事件形状**（只有数据、可序列化），前端协议的定义在 app/platform/ui.py。

背景（docs/ARCHITECTURE.md §2.7/§4）：主程序 = 事件基座，前端（终端 TUI / 将来的 web）只
"取输入 + 渲染事件"。基座不 print，只用这里的 Event 类型与前端说话——这正是 §2.6 说的
"先把事件壳命名成清晰的'事件类型 + dispatch'"。

放 app/schema 的理由：它们是**只有属性、可序列化**的纯结构（与 ToolResult / PlanStep 同类），
且将来 web 前端要直接把事件 JSON 化过 websocket——典型跨边界数据形状。运行时句柄（asyncio
队列、Future、连接）一律不进这里（同 approval_schema.py 的约定）。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionStarted:
    """会话就绪：前端可展示 session_id 与工作区（db/图要到首次输入才真建）。"""

    session_id: str
    workspace: str


@dataclass(frozen=True)
class ReadyForInput:
    """基座空闲、等用户输入（终端画 User 框；web 推"可以说话了"）。"""


@dataclass(frozen=True)
class TurnFinished:
    """一轮 turn 抵达终态：text 为该轮末条 AI 正文（给用户的答复）。"""

    text: str


@dataclass(frozen=True)
class TurnFailed:
    """一轮 turn 抛错（内部 bug / 漏网的工具异常）：不静默，交前端展示。"""

    message: str


# 基座可能发出的全部事件（前端按类型分派渲染）
Event = SessionStarted | ReadyForInput | TurnFinished | TurnFailed
