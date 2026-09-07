"""审批域纯数据结构（可跨边界序列化/传参，无运行时句柄）。

约定（docs 与 app/tui/approval_inbox.py 注释）：含 asyncio.Future 的运行时记录不进这里，
只放**可序列化的结构**——与 agent_schema.py 的 ToolResult( BaseModel )/PlanStep 同类。
"""

from typing import Any, Literal, NotRequired, TypedDict


class ApprovalRequest(TypedDict):
    """worker→主 agent 的一条审批请求（跨进程 HTTP JSON 结构）。"""

    worker_id: str
    tool_name: str
    tool_args: dict[str, Any]
    # 终端/命令类必填的人话解释，审批时与命令同屏展示
    description: NotRequired[str]


class ApprovalRecord(TypedDict):
    """broker 中一条审批的**纯数据**记录（不含等待决定的 Future）。

    decided=None 表示仍 pending；一旦 decide 后为 bool。审批 id 由 broker 分配。
    """

    approval_id: str
    payload: ApprovalRequest
    created: float
    decided: bool | None


class ApprovalStatus(TypedDict):
    """查询一条审批的当前状态。"""

    status: Literal["pending", "decided"]
    approved: bool | None
