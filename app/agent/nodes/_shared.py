"""节点间共享的运行时依赖。

`settings = get_settings()` **必须在 import 期执行**（行为契约：缺 `.env` 时在 import
阶段就响亮报错）。拆包后这个时机收在本模块，所有节点模块经它取 settings / logger。

`_tool_stamp` 是 ToolMessage 结构化戳的生产端唯一构造入口（消费端
`compression.parser.tm_status`）。
"""
import logging

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger("app.agent.nodes")


#-------------------ToolMessage 的结构化戳（**生产端单点**）-------------------
# 形状：`additional_kwargs` 里 `{"error_type": <语义标记>}` 或 `{"decision": "denied"}`（首次落地
# 2026-09-16）。**生产端 = 本模块的 ToolNode / ReviewNode**（全仓只有这两个写出点，且都经
# `_tool_stamp` 构造）；**消费端 = `compression.parser.tm_status`**（读这两个键）。
#
# 为什么不把键名/取值做成常量：**它们是字典的键，用变量承接与直接写字面量没有区别**
# （2026-09-22 用户拍板）——常量该留给"要按值分派或校验"的地方（如 `GATE_*` / `ASK_SELECT_*`）。
# 真正有价值的是 `_tool_stamp` 这个**构造器**：它保证两个生产端写出同一种形状。
def _tool_stamp(*, error_type: str | None = None, decision: str | None = None) -> dict:
    """构造 ToolMessage 的结构化戳（生产端唯一构造入口）。"""
    stamp: dict = {}
    if error_type:
        stamp["error_type"] = error_type
    if decision:
        stamp["decision"] = decision
    return stamp
