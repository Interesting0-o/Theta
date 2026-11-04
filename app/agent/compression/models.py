"""压缩引擎的结构原子与折叠计划。

本模块只放**数据结构**：不做解析、不做策略、不碰 LangGraph state。字段刻意与
`app/agent/nodes.py` 的 state 适配器解耦，便于后续 `FoldGroup` / 逐调用 `FoldDecision`
在 Stage 3 继续扩展。
"""
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, ToolMessage

from app.schema.agent_schema import NoteEntry


@dataclass
class ToolCallInfo:
    """一个 tool_call 及其兑现的 ToolMessage（批级压缩的最小观测）。"""

    call_id: str | None
    name: str
    args: dict
    tool_message: ToolMessage | None
    status: str
    eligible: bool = False


@dataclass
class ToolBlock:
    """结构原子：`AIMessage(tool_calls)` + 兑现它的全部 ToolMessage。"""

    ai_message: AIMessage
    calls: list[ToolCallInfo]
    start: int
    end: int
    complete: bool
    ids_ok: bool
    consumed: bool
    plan_epoch: int
    step_id: str | None
    milestone: bool
    eligible_chars: int = 0


@dataclass
class FoldPlan:
    """轮末折叠计划：由引擎产出，适配器只负责翻译成 state 更新。

    `removed[0]` 是折叠区最早被删消息，作为摘要 `SystemMessage` 的原位锚点。
    Stage 3 会在本结构上增加 `groups` / 按组摘要，不变的是“引擎出计划、节点做适配”。
    """

    removed: list = field(default_factory=list)
    summary_lines: list[str] = field(default_factory=list)
    archived_notes: dict[str, NoteEntry] = field(default_factory=dict)
    replaced: list[AIMessage] = field(default_factory=list)
