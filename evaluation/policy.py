"""闸门策略：评估时"模拟人"作答（审批给 y/n，提问给回答）。

真实 TUI（`app/platform/turn.py::drive_turn` 的 park/resume）里闸门的决定来自用户输入；
评估要把这部分替换成**确定性策略**，否则无法批量跑。每个策略是 `(payload: dict) -> Decision`
的函数，payload 即 ReviewNode interrupt 出的字典（见 app/agent/nodes.py::ReviewNode）：
审批含 tool_name / tool_args / tool_call_id / current_step，提问含 why / question / options
——用 `payload["type"]` 区分（`app/schema/approval_schema.py`）。

三个工厂，按"规则 / 脚本"两个维度组合：

| 工厂 | 审批怎么定 | 提问怎么答 | 用来测 |
| --- | --- | --- | --- |
| `allow_except(deny, answers)` | 按工具名名单 | 按 `answers` 顺序 | 常规任务（默认）；给了 answers 才能测提问 |
| `deny_all()` | 一律拒绝 | 一律不回答 | **拒绝路径**（`[approval_denied]` 之后模型怎么办） |
| `scripted(...)` | 按序脚本 | 按序脚本 | **逐次不同**的决定（第 1 次批、第 2 次拒） |

`allow_except(answers=…)` 与 `scripted` 都能给提问作答，区别在**审批那一侧**：前者按名单规则、
后者逐次回放。要"审批按规则 + 提问按脚本"用前者；要"每次审批都不一样"用后者。
"""
from __future__ import annotations

from typing import Callable, Sequence

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


def _is_ask(payload: dict) -> bool:
    return payload.get("type") == GATE_ASK_USER


def allow_except(deny: tuple[str, ...] = DEFAULT_DENY_TOOLS, answers: Sequence[str] = ()) -> Policy:
    """"除黑名单工具外都批准"；提问按 `answers` 依次作答——**评估默认用它**。

    黑名单默认把 terminal（任意命令执行）与四个联网工具（花钱/外发）挡住，保证评估沙箱安全。

    `answers` 只作用于**提问**载荷（`type == "ask_user"`）：按顺序取，每条作为"用户补充的一段话"
    （`Decision(kind="answer", supplement=…)`）。用尽后回落到"未作答"——这正是本设计里"没人答"的
    那条现成语义，模型会按最佳判断继续而不是卡住。**不给 `answers` 时提问恒为未作答**，所以想测
    `ask_user` 必须给。
    """

    pending = list(answers)

    def policy(payload: dict) -> Decision:
        if _is_ask(payload):
            if pending:
                return Decision(kind="answer", supplement=pending.pop(0))
            return Decision(kind="answer")
        return Decision(kind="approval", approved=payload.get("tool_name") not in deny)

    return policy


def deny_all() -> Policy:
    """一律拒绝审批、提问一律不回答——专测**拒绝路径**。

    比 `allow_except(deny=<全部工具名>)` 直白：不必先把工具名列全，改工具表也不会漏。
    提问侧照旧回"未作答"（不是"否决"）：拒绝与没人答在闸门语义上本就不同。
    """

    def policy(payload: dict) -> Decision:
        if _is_ask(payload):
            return Decision(kind="answer")
        return Decision(kind="approval", approved=False)

    return policy


def scripted(*decisions) -> Policy:
    """按序回放决定，**用尽后粘住最后一条**——测"逐次不同"的闸门序列。

    用尽后粘住 = 天然的"前 N 次这样、之后都那样"：`scripted(True, False)` 就是"第一次批、
    之后全拒"。

    每条可以是 `Decision`，也可以用**简写**（评估里手写三个字段太吵）：

    - `True` / `False` → 审批批准 / 拒绝；
    - `"一段话"` → 提问的回答（放进 `supplement`，即"用户没选选项、自己说了方案"）。

    简写与闸门种类**对不上就当场报错**（如拿 `True` 去答一个提问）：顺序错位属于脚本写错，
    静默降级成"未作答"会让人以为模型没提问，比直接失败糟得多。
    """
    if not decisions:
        raise ValueError("scripted 至少要给一条决定")
    remaining = list(decisions)

    def policy(payload: dict) -> Decision:
        index = len(decisions) - len(remaining)
        raw = remaining[0] if len(remaining) == 1 else remaining.pop(0)
        return _as_decision(raw, payload, index)

    return policy


def _as_decision(raw, payload: dict, index: int) -> Decision:
    """把一条脚本条目 + 闸门载荷翻成 `Decision`，并对不上号的情形当场报错。"""
    if isinstance(raw, Decision):
        decision = raw
    elif isinstance(raw, bool):
        decision = Decision(kind="approval", approved=raw)
    elif isinstance(raw, str):
        decision = Decision(kind="answer", supplement=raw)
    else:
        raise TypeError(f"scripted 第 {index + 1} 条看不懂：{raw!r}（用 bool / str / Decision）")

    expected = "answer" if _is_ask(payload) else "approval"
    if decision.kind != expected:
        seen = payload.get("type") or "tool_approval"
        raise ValueError(
            f"scripted 第 {index + 1} 条是 {decision.kind} 决定，但第 {index + 1} 个闸门是 "
            f"{expected}（载荷 type={seen}）——脚本与闸门顺序对不上。"
        )
    return decision
