"""上下文压缩引擎（2026-11-04 从 `nodes.py` 剥离）。

依赖方向是单向的：`nodes.py → compression`。本包**不** import `nodes.py`、
`app.agent.tools` 或 `app.platform.mcp`，因此引擎本身不依赖图与审批。注意父包
`app/agent/__init__.py` 仍会 re-export 图，所以导入路径本身仍受既有 `.env` 约束
（与仓库其余测试一致，不在本包处理）。

模块分工：
- `models.py`：结构原子（`ToolBlock` / `ToolCallInfo`）、折叠组（`FoldGroup`）与
  折叠计划（`FoldPlan`）；
- `parser.py`：协议块解析、状态判定、read_file 行区间；
- `policy.py`：编排 / 保护 / 归档 / 状态回执策略；
- `planner.py`：批级分组（硬边界 → plan step → 预算）；
- `compact.py`：轮末引擎，产出 `FoldPlan`；
- `compress.py`：轮内引擎，产出 ToolMessage 替换。

测试与调用方应 import 具体子模块；本文件只做显式公共 API 汇总，不做兼容补丁。
"""
from .compact import FOLD_HEADER, plan_compact
from .compress import TurnCompressor
from .models import FoldGroup, FoldPlan, ToolBlock, ToolCallInfo
from .parser import first_line, iter_tool_blocks, one_line, tm_status
from .planner import CycleBatchPlanner, group_segments
from .policy import (
    ARCHIVE_TOOLS,
    KEEP_TOOLS,
    NO_MASK_TOOLS,
    STATE_RECEIPT_TOOLS,
    WRITE_TOOLS,
    orchestrate_tools,
)

__all__ = [
    "ARCHIVE_TOOLS",
    "CycleBatchPlanner",
    "FOLD_HEADER",
    "FoldGroup",
    "FoldPlan",
    "KEEP_TOOLS",
    "NO_MASK_TOOLS",
    "STATE_RECEIPT_TOOLS",
    "ToolBlock",
    "ToolCallInfo",
    "TurnCompressor",
    "WRITE_TOOLS",
    "first_line",
    "group_segments",
    "iter_tool_blocks",
    "one_line",
    "orchestrate_tools",
    "plan_compact",
    "tm_status",
]
