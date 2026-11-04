"""批级分组：硬边界 → plan step → 预算（语义切点优先，预算是硬上界）。

纯确定性，不读 state、不改消息。Stage 3 会把轮末分组也并入同一个 planner。
"""
from __future__ import annotations

from .models import ToolBlock


class CycleBatchPlanner:
    """把当前轮 block 切成批。阈值由调用方（`TurnCompressor`）持有。"""

    def __init__(self, max_batch_chars: int, min_batch_chars: int) -> None:
        self.max_batch_chars = max_batch_chars
        self.min_batch_chars = min_batch_chars

    def plan_batches(
        self, blocks: list[ToolBlock], boundaries: list[int]
    ) -> list[list[ToolBlock]]:
        """把 blocks 切成批：硬边界 → plan step → 预算（含语义切点）。

        `boundaries` 是工具块之外的消息下标（Human/System/Assistant/孤儿 ToolMessage）；
        不完整/未消费 block 也不进批。批内可以夹着不可压的 block，只压 eligible 的
        ToolMessage；批与批之间由 step 变化、硬边界或预算切点分开。
        """
        if not blocks:
            return []
        batches: list[list[ToolBlock]] = []
        segment: list[ToolBlock] = []
        segment_key = None
        prev_end = blocks[0].start - 1

        def flush() -> None:
            nonlocal segment, segment_key
            if segment:
                batches.extend(self._plan_segment(segment))
            segment = []
            segment_key = None

        for block in blocks:
            if (
                self._has_boundary(boundaries, prev_end, block.start)
                or not block.complete
                or not block.consumed
            ):
                flush()
                prev_end = block.end
                continue
            key = (block.plan_epoch, block.step_id)
            if segment and key != segment_key:
                flush()
            segment.append(block)
            segment_key = key
            prev_end = block.end
        flush()
        return batches

    @staticmethod
    def _has_boundary(boundaries: list[int], left: int, right: int) -> bool:
        """`(left, right)` 之间是否有工具块之外的消息（硬边界）。"""
        return any(left < position < right for position in boundaries)

    def _plan_segment(self, blocks: list[ToolBlock]) -> list[list[ToolBlock]]:
        """在一个 step 段内按预算切批；预算超限时优先在里程碑附近切。"""
        batches: list[list[ToolBlock]] = []
        start = 0
        n = len(blocks)
        while start < n:
            end = start
            chars = 0
            while end < n and chars + blocks[end].eligible_chars <= self.max_batch_chars:
                chars += blocks[end].eligible_chars
                end += 1

            if end == n:
                cut = n
            else:
                cut = self._semantic_cut(blocks, start, end)
                if cut is None or cut <= start:
                    cut = end if end > start else start + 1

            batch = blocks[start:cut]
            if self.batch_chars(batch) >= self.min_batch_chars:
                batches.append(batch)
            start = cut
        return batches

    def _semantic_cut(
        self, blocks: list[ToolBlock], start: int, end: int
    ) -> int | None:
        """在 `[start, end)` 里找最近的里程碑切点；只在预算的 ±20% 容差内采用。

        `end` 是预算循环停下的“首个放不下的块”下标（或批尾）：切点只能落在它之前，
        否则会把超预算块带进本批、突破 `max_batch_chars`。里程碑 = 写操作完成、
        进程 start/kill、成功↔失败转换、plan 更新。找不到就返回 `None`，由调用方按
        预算硬切——语义优先，但预算是硬上界。单个块本身超过预算时只能独立成批。
        """
        threshold = int(self.max_batch_chars * 0.8)
        chars = 0
        cut = None
        for index in range(start, end):
            chars += blocks[index].eligible_chars
            if blocks[index].milestone and chars >= threshold:
                cut = index + 1
        return cut

    @staticmethod
    def batch_chars(batch: list[ToolBlock]) -> int:
        return sum(block.eligible_chars for block in batch)
