"""图节点包（2026-11-04 由 `nodes.py` 拆出）。

`graph.py` 与测试仍从 `app.agent.nodes` 导入；本文件只做显式 re-export，不承载逻辑。
拆包后 monkeypatch 必须打到**具体子模块**（如 `app.agent.nodes.review.interrupt`），
打在本包门面上不会传导到子模块的模块全局。
"""
from ._shared import _tool_stamp, logger, settings
from .compact_node import CompactNode, needs_compact
from .compress_node import CompressNode
from .llm import LLMNode
from .orchestrate import _EXTRA_SLICE_KEYS, OrchestrateNode
from .queue import QueueNode
from .review import ReviewNode
from .tool import ToolNode

__all__ = [
    "CompactNode",
    "CompressNode",
    "LLMNode",
    "OrchestrateNode",
    "QueueNode",
    "ReviewNode",
    "ToolNode",
    "_EXTRA_SLICE_KEYS",
    "_tool_stamp",
    "logger",
    "needs_compact",
    "settings",
]
