"""闸门策略：评估时"模拟人"作答（审批给 y/n，提问给回答）。

真实 TUI（`app/platform/turn.py::drive_turn` 的 park/resume）里闸门的决定来自用户输入；
评估要把这部分替换成**确定性策略**，否则无法批量跑。每个策略是 `(payload: dict) -> Decision`
的函数，payload 即 ReviewNode interrupt 出的字典（见 app/agent/nodes.py::ReviewNode）：
审批含 tool_name / tool_args / tool_call_id / current_step，提问含 why / question / options
——用 `payload["type"]` 区分（`app/schema/approval_schema.py`），策略只按工具名决策即可覆盖
绝大多数场景。

内置策略：
- allow_except(deny)：除黑名单外批准——**评估默认用它**，把 terminal（任意命令
  执行）与四个联网工具（花钱/外发）列入黑名单，保证评估沙箱安全；对**提问**一律回
  "未回答"，理由见该函数。
"""
from __future__ import annotations

from typing import Callable

from app.schema.approval_schema import GATE_ASK_USER, Decision

Policy = Callable[[dict], Decision]

# 评估默认拒绝的工具：terminal 是任意命令执行（eval 不测它），
# web 系四个工具会真实联网花 token；文件与 git 读写走审批闸门照常放行。
DEFAULT_DENY_TOOLS: tuple[str, ...] = (
    "run_command",
    "web_search",
    "extract_urls",
    "crawl_website",
    "deep_research",
)


def allow_except(deny: tuple[str, ...] = DEFAULT_DENY_TOOLS) -> Policy:
    """构造"除黑名单工具外都批准"的策略，评估默认入口。

    对**提问**一律回"未回答"：评估里没有人可问，而"未回答"正是本设计里"没人答"那条现成
    语义——模型据此按最佳判断继续（回执会明确告诉它"用户未作答"），不会卡在等待上。
    "评估里模型爱不爱提问"这件事该由任务自己的策略去测，不该由默认策略替它决定。
    """

    def policy(payload: dict) -> Decision:
        if payload.get("type") == GATE_ASK_USER:
            return Decision(kind="answer")
        return Decision(kind="approval", approved=payload.get("tool_name") not in deny)

    return policy
