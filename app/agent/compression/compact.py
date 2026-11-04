"""轮末折叠引擎：把更早轮次已消费的旧工具块规划成 `FoldPlan`。

纯确定性、不调 LLM、不碰 LangGraph state。节点适配器（`nodes.py::CompactNode`）只把
`FoldPlan` 翻译成 `SystemMessage` / `RemoveMessage` / `notes` 更新。
"""
from __future__ import annotations

import time

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.config import TEXT_BUDGET_CHARS
from app.schema.agent_schema import NoteEntry

from . import parser, policy
from .models import FoldPlan

FOLD_HEADER = "# 历史工具记录（已折叠）"
# 归档正文截断上限（字符；值归 app/config.py::TEXT_BUDGET_CHARS，与记忆/画像/技能同族一处出处）
NOTE_MAX = TEXT_BUDGET_CHARS


def render_line(reason: str, tool: str, status: str, detail: str = "", note_ref: str = "") -> str:
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


def reason_of(ai) -> str:
    """取块内 AIMessage 的 content（prompt 引导模型写成一句话工作日志），非 str 视为空。"""
    content = getattr(ai, "content", None)
    return content.strip() if isinstance(content, str) else ""


def new_note_ref(notes: dict) -> tuple[str, int]:
    """下一个笔记 key（notes#r<n>）与其编号，取现有 key 最大号 +1。"""
    max_n = 0
    for key in notes:
        if isinstance(key, str) and key.startswith("notes#r"):
            try:
                max_n = max(max_n, int(key[len("notes#r"):]))
            except ValueError:
                pass
    return f"notes#r{max_n + 1}", max_n + 1


def plan_compact(
    messages: list,
    notes: dict,
    orchestrate_tools: frozenset[str] | set[str],
) -> FoldPlan:
    """扫描消息，决定折叠哪些"更早轮次、已消费的完整工具块"，并生成摘要行与新增 notes。

    `removed[0]` 是折叠区最早被删消息（将作为 SystemMessage 原位替换锚点）。整块折叠时
    含 AIMessage(tool_calls) + 兑现它的 ToolMessages；混合块只含被折的普通 ToolMessages。

    不折叠：无 tool_calls 的 AIMessage（答复）、Human、SystemMessage、块不完整（有悬空
    call）、块内消息缺稳定 id、块未被消费（块后、下一条 Human 前没有 AIMessage）、
    整块都是编排工具、以及折叠区为空（单轮会话）。
    """
    # 锚点 = 最后一条 HumanMessage；它及其后（当前轮）原样保留，只折更早轮次
    anchor = -1
    for i, m in enumerate(messages):
        if isinstance(m, HumanMessage):
            anchor = i
    if anchor <= 0:
        return FoldPlan()
    region = messages[:anchor]

    removed: list = []
    lines: list[str] = []
    archived: dict[str, NoteEntry] = {}
    replaced: list[AIMessage] = []
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

        complete = (
            len(claimed) == len(call_ids)
            and all(cid is not None for cid in call_ids)
            and all(isinstance(tc, dict) for tc in m.tool_calls)
        )
        # 原位替换要求每条待删消息都有稳定 id（生产 add_messages 自动赋 uuid）；
        # 块还必须已被模型消费——折掉模型还没看过的工具结果，等于从历史里抽走它。
        if not (
            complete
            and all(getattr(b, "id", None) for b in block)
            and parser.is_consumed(region, j)
        ):
            continue

        # 编排工具只动 state（计划/提问/记忆/技能回执），不是可重取的外部结果：
        # 整块都是编排工具 → 原样保留；混合块 → 只折普通调用，保留编排调用与回执。
        kept_calls = [
            tc
            for tc in m.tool_calls
            if (tc.get("name") or "").strip() in orchestrate_tools
        ]
        folded_calls = [
            tc
            for tc in m.tool_calls
            if (tc.get("name") or "").strip() not in orchestrate_tools
        ]
        if not folded_calls:
            continue

        # 失败/被拒块不折：错误正文必须原样留在历史里（§8.3 不变量）。
        folded_status = [
            parser.tm_status(claimed.get(tc.get("id")))[0]
            for tc in folded_calls
            if claimed.get(tc.get("id")) is not None
        ]
        if any(status in {"failed", "denied"} for status in folded_status):
            continue

        # 混合块里摘要 SystemMessage 会顶替第一条被折的普通 ToolMessage。为保持
        # AIMessage(tool_calls) → ToolMessage 的邻接关系，要求编排回执全部排在
        # 普通回执之前（生产路径恒成立：OrchestrateNode 先跑、ToolNode 后跑）；
        # 顺序不满足就整块不折，宁可少压也不产生非法消息序列。
        kept_ids = {tc.get("id") for tc in kept_calls}
        folded_ids = {tc.get("id") for tc in folded_calls}
        if kept_calls:
            last_kept = max(
                (
                    index
                    for index, item in enumerate(block)
                    if isinstance(item, ToolMessage) and item.tool_call_id in kept_ids
                ),
                default=-1,
            )
            first_folded = min(
                (
                    index
                    for index, item in enumerate(block)
                    if isinstance(item, ToolMessage) and item.tool_call_id in folded_ids
                ),
                default=len(block),
            )
            if first_folded < last_kept:
                continue

        reason = reason_of(m)
        for tc in folded_calls:
            name = (tc.get("name") or "").strip()
            tm = claimed.get(tc.get("id"))  # type: ignore
            status, detail = parser.tm_status(tm) if tm is not None else ("ok", "")

            note_ref = ""
            # 仅联网四工具的成功正文进笔记区（其余工具结果时效强、可重取，折叠即弃）
            if name in policy.ARCHIVE_TOOLS and status == "ok" and tm is not None:
                content = str(tm.content or "").strip()
                if content and not content.startswith("["):
                    if ref_num is None:
                        _, ref_num = new_note_ref(notes)
                    ref = f"notes#r{ref_num}"
                    content_cap = content if len(content) <= NOTE_MAX else content[:NOTE_MAX] + "…"
                    archived[ref] = {
                        "title": parser.first_line(content, 80),
                        "source_tool": name,
                        "content": content_cap,
                        "created": int(time.time()),
                        "size": len(content),
                    }
                    note_ref = ref
                    ref_num += 1

            lines.append(render_line(reason, name, status, detail, note_ref))

        block_removed = [
            b
            for b in block
            if isinstance(b, ToolMessage) and b.tool_call_id in folded_ids
        ]
        if not kept_calls:
            block_removed.insert(0, m)
        removed.extend(block_removed)
        if kept_calls:
            # 原位改写 AIMessage：只留编排调用的 tool_calls，普通调用的回执被删。
            replaced.append(m.model_copy(update={"tool_calls": kept_calls}))

    return FoldPlan(
        removed=removed,
        summary_lines=lines,
        archived_notes=archived,
        replaced=replaced,
    )
