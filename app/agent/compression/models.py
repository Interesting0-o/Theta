"""压缩引擎的结构原子与折叠计划。

本模块只放**数据结构**：不做解析、不做策略、不碰 LangGraph state。字段刻意与
`app/agent/nodes.py` 的 state 适配器解耦；后续若把处置升级成显式 `FoldDecision`
枚举，只在本模块扩展。
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
class FoldGroup:
    """轮末折叠单位：同一 plan epoch/step 内、被硬边界隔开的一组连续工具块。

    一个 group 对应一条摘要 `SystemMessage`（`summary`），锚点是 `removed[0]` 的 id。
    `lines` 内部仍按被折调用一行，保留工作日志与 notes ref；按组聚合的压缩版留待实测。
    """

    key: tuple[int, str | None]
    blocks: list[ToolBlock] = field(default_factory=list)
    header: str = ""
    lines: list[str] = field(default_factory=list)
    removed: list = field(default_factory=list)
    replaced: list[AIMessage] = field(default_factory=list)
    archived_notes: dict[str, NoteEntry] = field(default_factory=dict)
    anchor_id: str | None = None

    @property
    def summary(self) -> str:
        parts = ([self.header] if self.header else []) + self.lines
        return "\n".join(parts)


@dataclass
class FoldPlan:
    """轮末折叠计划：由引擎产出，适配器只负责翻译成 state 更新。

    `groups` 是真正的折叠单位；`removed` / `replaced` / `archived_notes` / `summaries`
    是展平后的汇总，便于调用方与测试按整体断言。
    """

    groups: list[FoldGroup] = field(default_factory=list)
    removed: list = field(default_factory=list)
    replaced: list[AIMessage] = field(default_factory=list)
    archived_notes: dict[str, NoteEntry] = field(default_factory=dict)
    summaries: dict[str, str] = field(default_factory=dict)
