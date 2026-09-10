"""审批域纯数据结构（可跨边界序列化/传参，无运行时句柄）。

约定（docs 与 app/platform/approvals.py 注释）：含 asyncio.Future 的运行时记录不进这里，
只放**可序列化的结构**——与 agent_schema.py 的 ToolResult( BaseModel )/PlanStep 同类。

审批通道的默认地址也算这套协议的契约，故常量放这里——**主侧与 worker 侧共用同一份默认值**
（worker 的 env 是整体替换、不继承父进程，两边各写一个字面量迟早会漂）。
"""

from typing import Any, Literal, NotRequired, TypedDict

# 审批收件箱默认监听地址（仅本机）。
# 端口选 25010 而不是常见的 8010/8080：① 避开高频 dev 端口；② **低于 OS 临时端口段**
# （Linux 32768+ / Windows 49152+）——固定监听口落在临时端口段里，会被偶发的外连占走。
# 两侧都可用 env 覆盖：主侧 AGENT_INBOX_PORT、worker 侧 AGENT_INBOX_URL（由主侧转发）。
DEFAULT_INBOX_HOST = "127.0.0.1"
DEFAULT_INBOX_PORT = 25010


class ApprovalRequest(TypedDict):
    """worker→主 agent 的一条审批请求（跨进程 HTTP JSON 结构）。"""

    worker_id: str
    tool_name: str
    tool_args: dict[str, Any]
    # 终端/命令类必填的人话解释，审批时与命令同屏展示
    description: NotRequired[str]
    # 仅本地（主 agent 自身 interrupt）携带的**纯展示**字段：保持审批面板旧样式
    # （"步骤 1/1" + 调用ID），不参与决定逻辑；远端 worker 请求不带。
    current_step: NotRequired[str]
    tool_call_id: NotRequired[str | None]


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
