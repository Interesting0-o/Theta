"""CompressNode：轮内压缩的 state 适配器。"""
from app.agent.compression.compress import (
    MAX_BATCH_CHARS,
    MASK_MIN_CHARS,
    MIN_BATCH_CHARS,
    READ_DEDUP_MIN_CHARS,
    TURN_HIGH_WATERMARK_CHARS,
    TURN_KEEP_BLOCKS,
    TURN_LOW_WATERMARK_CHARS,
    TurnCompressor,
)
from app.agent.state import AgentState


#----------------------轮内压缩（compress_node）----------------------
# 区域边界（2026-10-20 定，与 CompactNode 互不相交）：
#   CompactNode  : messages[:last_human_index]        旧轮，轮末无条件执行
#   CompressNode : messages[last_human_index + 1:]    当前轮，工具循环中执行
# 轮内**不删消息**：图路由中间删掉结果会破坏 tool_call / ToolMessage 配对与调用方状态，
# 因此只改 ToolMessage.content。算法与阈值见 `app/agent/compression/compress.py`。
class CompressNode:
    """轮内压缩节点：`compression.compress.TurnCompressor` 的 **state 适配器**。

    引擎负责当前轮的三件事（细节见引擎模块 docstring）：read_file 去重指针化、批级
    移动窗口、状态回执变薄。本节点只把引擎返回的 ToolMessage 替换按原顺序写回 state；
    不增删消息、不改 tool_call 配对、不调 LLM。

    状态（2026-10-20）：已接进主图（`tool_node` / `orchestrate_node` → `compress_node`
    → `llm_node`）；worker 子图不带本节点。
    """

    def __init__(
        self,
        read_dedup_min_chars: int = READ_DEDUP_MIN_CHARS,
        high_watermark_chars: int = TURN_HIGH_WATERMARK_CHARS,
        low_watermark_chars: int = TURN_LOW_WATERMARK_CHARS,
        keep_blocks: int = TURN_KEEP_BLOCKS,
        mask_min_chars: int = MASK_MIN_CHARS,
        max_batch_chars: int = MAX_BATCH_CHARS,
        min_batch_chars: int = MIN_BATCH_CHARS,
        tombstone_blocks: bool = False,
    ) -> None:
        """参数语义与校验见 `TurnCompressor.__init__`（本节点只做透传）。"""
        self._engine = TurnCompressor(
            read_dedup_min_chars=read_dedup_min_chars,
            high_watermark_chars=high_watermark_chars,
            low_watermark_chars=low_watermark_chars,
            keep_blocks=keep_blocks,
            mask_min_chars=mask_min_chars,
            max_batch_chars=max_batch_chars,
            min_batch_chars=min_batch_chars,
            tombstone_blocks=tombstone_blocks,
        )

    async def __call__(self, state: AgentState) -> dict | AgentState:
        messages = list(state.get("messages") or [])
        if not messages:
            return {}

        updates = self._engine.compress(messages, state.get("current_plan") or [])
        return {"messages": updates} if updates else {}
