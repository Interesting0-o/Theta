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

# 长轮询单次 block 的上限（秒）：`GET /requests/{id}?block=1` 最多攥着连接这么久，到点回
# `{"status":"pending"}` 让客户端重试。**两侧必须共用这一个值**——客户端 httpx 的 read
# timeout 若小于它，人还在思考时客户端就先 ReadTimeout 了，而那个异常会让 worker 整条子任务
# 失败、决定无人领取（2026-09-15 审计发现的实际缺陷）。
INBOX_BLOCK_SECONDS = 120.0


class ApprovalRequest(TypedDict):
    """worker→主 agent 的一条审批请求（跨进程 HTTP JSON 结构）。"""

    worker_id: str
    tool_name: str
    tool_args: dict[str, Any]
    # 终端/命令类必填的人话解释，审批时与命令同屏展示
    description: NotRequired[str]
    # 仅远端 worker 携带：**派给它的那条子任务原文**。主 agent 的对话人一直在跟，worker 的
    # 推理过程人看不到——只有命令 + 解释时，"审的是什么"就没有来龙去脉。带上它是让审批从
    # "审一条来源不明的命令"回到"审一个有上下文的动作"（见 docs/MULTI_AGENT.md）。
    subtask: NotRequired[str]
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
    decided: bool | None


class ApprovalStatus(TypedDict):
    """查询一条审批的当前状态。"""

    status: Literal["pending", "decided"]
    approved: bool | None
