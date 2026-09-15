"""审批策略：评估时"模拟人"按下 y/n。

真实 TUI（`app/platform/turn.py::drive_turn` 的 park/resume）里审批决定来自用户输入；
评估要把这部分替换成**确定性策略**，否则无法批量跑。每个策略是 `(payload: dict) -> bool`
的函数，payload 即 ReviewNode interrupt 出的字典（含 tool_name / tool_args / tool_call_id /
current_step，见 app/agent/nodes.py::ReviewNode），策略只按工具名决策即可覆盖绝大多数场景。

内置策略：
- allow_except(deny)：除黑名单外批准——**评估默认用它**，把 terminal（任意命令
  执行）与四个联网工具（花钱/外发）列入黑名单，保证评估沙箱安全。
"""
from __future__ import annotations

from typing import Callable

Policy = Callable[[dict], bool]

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
    """构造"除黑名单工具外都批准"的策略，评估默认入口。"""

    def policy(payload: dict) -> bool:
        return payload.get("tool_name") not in deny

    return policy
