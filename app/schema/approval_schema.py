"""人机闸门（审批 / 提问）的纯数据结构（可跨边界序列化/传参，无运行时句柄）。

约定（docs 与 app/platform/approvals.py 注释）：含 asyncio.Future 的运行时记录不进这里，
只放**可序列化的结构**——与 agent_schema.py 的 ToolResult( BaseModel )/PlanStep 同类。

**闸门有两种载荷**（`ApprovalRequest.type`）：
- `tool_approval`：工具审批，回答只有"批 / 不批"；
- `ask_user`：agent 提问，回答是**两段**——选中的选项 + 一句自由补充。
两者共用同一套 park/resume 骨架与同一个 broker（见 docs/MULTI_AGENT.md §6），
差异只在载荷形状与 `Decision.kind`。取名仍留 `Approval*`（改名要横扫 schema +
platform + sub_agent，与本条无关）：它是**闸门**域，不是只有审批。

审批通道的默认地址也算这套协议的契约，故常量放这里——**主侧与 worker 侧共用同一份默认值**
（worker 的 env 是整体替换、不继承父进程，两边各写一个字面量迟早会漂）。
"""

from dataclasses import dataclass
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

# 闸门载荷种类（`ApprovalRequest.type` 的取值）。缺省按审批处理：远端 worker 只会审批，
# 它的载荷不带本字段。
GATE_TOOL_APPROVAL = "tool_approval"
GATE_ASK_USER = "ask_user"


@dataclass(frozen=True)
class Decision:
    """人类对一条闸门请求的回答——`UI.decide` 的返回值，**唯一能让 park 的 run 继续的东西**。

    决定权威在人：本对象只承载"人说了什么"，判定策略一律不在这里（审批策略在 tool.json /
    ReviewNode，提问该不该问在模型）。

    - `kind="approval"`：只看 `approved`；
    - `kind="answer"`：回答是**两段**——选中的选项 + 一句自由补充，两段都可留空。
      `option_index` / `option_text` 只在用户真选了某项时非 None；而"没选任何给定选项、
      自己写了一段方案"落在 `supplement` 里，是**有效回答**，不是没回答（模型最容易在这里读错）。
      两段皆空 = 未回答（EOF 或直接回车），消费方只认 `unanswered` 这一条判据。
    """

    kind: Literal["approval", "answer"]
    approved: bool | None = None
    option_index: int | None = None
    option_text: str | None = None
    supplement: str | None = None

    @property
    def unanswered(self) -> bool:
        """提问是否"无人作答"（两段皆空）。审批不适用此判据（审批只看 approved）。"""
        return (
            self.kind == "answer"
            and self.option_index is None
            and not (self.supplement or "").strip()
        )


class ApprovalRequest(TypedDict):
    """一条闸门请求的载荷（跨进程 HTTP JSON 结构，也是本进程内 broker 的 payload）。"""

    worker_id: str
    # 闸门种类：GATE_TOOL_APPROVAL / GATE_ASK_USER。**缺省按审批处理**——worker 只会审批，
    # 它的载荷不带本字段（提问在 worker 侧结构性不可达，见 app/agent/tools.py::worker_tools）。
    type: NotRequired[str]
    tool_name: NotRequired[str]
    tool_args: NotRequired[dict[str, Any]]
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
    # 仅 type=ask_user（且只可能来自本地主 agent）携带：问题正文、选项清单、"为什么问"。
    # options 可空 = 纯开放式提问；选项上限由 ReviewNode 在弹面板前把关（见 app/agent/nodes.py）。
    question: NotRequired[str]
    options: NotRequired[list[str]]
    why: NotRequired[str]


class ApprovalRecord(TypedDict):
    """broker 中一条闸门请求的**纯数据**记录（不含等待决定的 Future）。

    decided=None 表示仍 pending；一旦回填后为 Decision。id 由 broker 分配。
    """

    approval_id: str
    payload: ApprovalRequest
    decided: Decision | None


class ApprovalStatus(TypedDict):
    """查询一条闸门请求的当前状态。

    `approved` 是**审批的 bool 投影**：HTTP 长轮询只有 worker 在用，而 worker 只会审批
    （提问在 worker 侧结构性不可达），所以这里不随 Decision 一起泛化。
    """

    status: Literal["pending", "decided"]
    approved: bool | None
