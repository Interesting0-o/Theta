"""轮末折叠引擎：把更早轮次已消费的旧工具块规划成 `FoldPlan`。

纯确定性、不调 LLM、不碰 LangGraph state。节点适配器（`nodes.py::CompactNode`）只把
`FoldPlan` 翻译成 `SystemMessage` / `RemoveMessage` / `notes` 更新。

Stage 3（2026-11-04）：折叠单位从“整段一条扁平摘要”改为 `FoldGroup`——同一 plan
epoch/step 内、被硬边界隔开的连续工具块为一组，每组一条 `SystemMessage`。块内按调用
逐条处置：编排 / 失败 / 被拒保持原文，联网正文归档 notes，其余折叠。
"""
from __future__ import annotations

import time

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.config import TEXT_BUDGET_CHARS
from app.schema.agent_schema import NoteEntry

from . import parser, planner, policy
from .models import FoldGroup, FoldPlan, ToolBlock

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


def group_header(key: tuple[int, str | None]) -> str:
    """每组摘要的标题：优先 step，其次 plan epoch。"""
    epoch, step = key
    if step is not None:
        return f"# 历史工具记录（已折叠 · 步骤 {step}）"
    if epoch:
        return f"# 历史工具记录（已折叠 · 计划 #{epoch}）"
    return FOLD_HEADER


def _block_folding(
    block: ToolBlock,
    region: list,
    orchestrate_tools: frozenset[str] | set[str],
) -> tuple[set[str], set[str]] | None:
    """决定块内逐调用处置；返回 `(folded_ids, kept_ids)`，`None` = 整块保留。

    KEEP：编排工具（state 回执）、worker 结论（`policy.KEEP_TOOLS`）、失败/被拒结果
    ——这些是“压了就丢真值”的东西。
    FOLD：其余成功结果（联网四件套在折叠时归档 notes）。

    删除机制要求摘要 `SystemMessage` 顶替第一条被折 `ToolMessage`，因此保留回执必须
    全部排在折叠回执之前；顺序不满足就整块保留，宁可少压也不产生非法消息序列。
    """
    block_messages = region[block.start:block.end + 1]
    kept_ids: set[str] = set()
    folded_ids: set[str] = set()
    for call in block.calls:
        if call.tool_message is None or call.call_id is None:
            return None
        if (
            call.name in orchestrate_tools
            or call.name in policy.KEEP_TOOLS
            or call.status in {"failed", "denied"}
        ):
            kept_ids.add(call.call_id)
        else:
            folded_ids.add(call.call_id)
    if not folded_ids:
        return None
    if kept_ids:
        last_kept = max(
            index
            for index, item in enumerate(block_messages)
            if isinstance(item, ToolMessage) and item.tool_call_id in kept_ids
        )
        first_folded = min(
            index
            for index, item in enumerate(block_messages)
            if isinstance(item, ToolMessage) and item.tool_call_id in folded_ids
        )
        if first_folded < last_kept:
            return None
    return folded_ids, kept_ids


def _archive_web_result(
    name: str,
    status: str,
    message: ToolMessage | None,
    notes: dict,
    archive: dict[str, NoteEntry],
    ref_num: int | None,
) -> tuple[str, int | None]:
    """联网四工具的成功正文进 notes；返回 `(note_ref, 下一个 ref_num)`。

    `notes` 只用于续编号（会话已有 notes），`archive` 是本组新增条目。
    """
    if name not in policy.ARCHIVE_TOOLS or status != "ok" or message is None:
        return "", ref_num
    content = str(message.content or "").strip()
    if not content or content.startswith("["):
        return "", ref_num
    if ref_num is None:
        _, ref_num = new_note_ref(notes)
    ref = f"notes#r{ref_num}"
    content_cap = content if len(content) <= NOTE_MAX else content[:NOTE_MAX] + "…"
    archive[ref] = {
        "title": parser.first_line(content, 80),
        "source_tool": name,
        "content": content_cap,
        "created": int(time.time()),
        "size": len(content),
    }
    return ref, ref_num + 1


def plan_compact(
    messages: list,
    notes: dict,
    orchestrate_tools: frozenset[str] | set[str],
) -> FoldPlan:
    """扫描消息，规划“已消费工具块”的分组折叠。

    折叠区：
    - **有最终答复**（末条是无 tool_calls 的 AIMessage）：折到最终答复之前，**当前轮
      已闭环的工具块也折**（调研结论：逻辑停止点压缩），最终答复原样保留；
    - **无最终答复**（steering 提前收口等）：只折最后一条 HumanMessage 之前的旧轮，
      当前轮在途结果保持原样。

    不折叠：无 tool_calls 的 AIMessage、Human/System、不完整块、未被消费块、整块都是
    保护调用、以及折叠区为空。
    """
    final_answer_index: int | None = None
    if messages:
        last = messages[-1]
        if isinstance(last, AIMessage) and not getattr(last, "tool_calls", None):
            final_answer_index = len(messages) - 1

    if final_answer_index is None:
        anchor = -1
        for i, m in enumerate(messages):
            if isinstance(m, HumanMessage):
                anchor = i
        if anchor <= 0:
            return FoldPlan()
        cutoff = anchor
    else:
        cutoff = final_answer_index

    # 解析整段 messages（而不是截到 cutoff）：当前轮块的 `consumed` 要看到最终答复。
    blocks, boundaries = parser.iter_tool_blocks(messages)
    blocks = [block for block in blocks if block.end < cutoff]
    decisions = {
        block.start: _block_folding(block, messages, orchestrate_tools)
        for block in blocks
    }
    # 整块保留的块是组边界：把它们的位置并进 boundaries，避免跨块成组。
    group_boundaries = sorted(
        set(boundaries) | {block.start for block in blocks if decisions[block.start] is None}
    )
    foldable = [block for block in blocks if decisions[block.start] is not None]

    plan = FoldPlan()
    ref_num: int | None = None

    for blocks_in_group in planner.group_segments(foldable, group_boundaries):
        first = blocks_in_group[0]
        key = (first.plan_epoch, first.step_id)
        group = FoldGroup(key=key, blocks=list(blocks_in_group), header=group_header(key))

        for block in blocks_in_group:
            folded_ids, kept_ids = decisions[block.start]  # type: ignore[misc]
            block_messages = messages[block.start:block.end + 1]
            reason = reason_of(block.ai_message)

            folded_calls = [
                call for call in block.calls if call.call_id in folded_ids
            ]
            kept_calls = [
                call for call in block.calls if call.call_id in kept_ids
            ]
            for call in folded_calls:
                note_ref, ref_num = _archive_web_result(
                    call.name,
                    call.status,
                    call.tool_message,
                    notes,
                    group.archived_notes,
                    ref_num,
                )
                group.lines.append(
                    render_line(reason, call.name, call.status, "", note_ref)
                )

            folded_messages = [
                message
                for message in block_messages
                if isinstance(message, ToolMessage) and message.tool_call_id in folded_ids
            ]
            if not kept_calls:
                folded_messages.insert(0, block.ai_message)
            group.removed.extend(folded_messages)
            if kept_calls:
                # 原位改写 AIMessage：只留保留调用的 tool_calls。
                group.replaced.append(
                    block.ai_message.model_copy(
                        update={"tool_calls": [
                            tc for tc in block.ai_message.tool_calls
                            if tc.get("id") in kept_ids
                        ]}
                    )
                )

        if not group.removed:
            continue
        group.anchor_id = getattr(group.removed[0], "id", None)
        plan.groups.append(group)
        plan.removed.extend(group.removed)
        plan.replaced.extend(group.replaced)
        plan.archived_notes.update(group.archived_notes)
        if group.anchor_id:
            plan.summaries[group.anchor_id] = group.summary

    return plan
