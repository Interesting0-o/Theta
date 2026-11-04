"""闸门策略：评估时"模拟人"作答（审批给 y/n，提问给回答）。

真实 TUI（`app/platform/turn.py::drive_turn` 的 park/resume）里闸门的决定来自用户输入；
评估要把这部分替换成**确定性策略**，否则无法批量跑。每个策略是 `(payload: dict) -> Decision`
的函数，payload 即 ReviewNode interrupt 出的字典（见 app/agent/nodes/review.py::ReviewNode）：
审批含 tool_name / tool_args / tool_call_id / current_step，提问含 why / question / options
——用 `payload["type"]` 区分（`app/schema/approval_schema.py`）。

三个工厂，按"规则 / 脚本"两个维度组合：

| 工厂 | 审批怎么定 | 提问怎么答 | 用来测 |
| --- | --- | --- | --- |
| `allow_except(deny, answers)` | 按工具名名单 | 按 `answers` 顺序 | 常规任务（默认）；给了 answers 才能测提问 |
| `deny_all()` | 一律拒绝 | 一律不回答 | **拒绝路径**（`[approval_denied]` 之后模型怎么办） |
| `scripted(...)` | 按序脚本 | 按序脚本 | **逐次不同**的决定（第 1 次批、第 2 次拒） |
| `allow_readonly_terminal(...)` | 只批允许的只读命令 | 一律不答 | **终端场景**（grep/wc/tail 等；重定向与写命令拒绝） |

`allow_except(answers=…)` 与 `scripted` 都能给提问作答，区别在**审批那一侧**：前者按名单规则、
后者逐次回放。要"审批按规则 + 提问按脚本"用前者；要"每次审批都不一样"用后者。
"""
from __future__ import annotations

import re
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


# 终端只读策略允许的命令族（按每段首个词判断；含管道/&& 时逐段检查）。
_READONLY_COMMANDS: tuple[str, ...] = (
    "grep", "findstr", "wc", "tail", "head", "cat", "type",
    "sed", "awk", "find", "dir", "ls", "stat",
    # 统计/格式化用的只读工具，以及常见的无副作用控制流：
    "echo", "printf", "true", "false", "sort", "uniq", "cut", "tr", "nl", "column", "pwd",
)

# 出现这些片段一律拒绝：输出重定向会写文件，命令替换/进程替换可能绕过首词检查。
# 输入重定向 `<` 是只读的，放行；`2>/dev/null` 在检查前先剥掉。
_DENY_FRAGMENTS: tuple[str, ...] = (">", "`", "$(", "<(", ">(", "sudo", "rm ", "del ", "mv ", "cp ")

# 简单的 shell 变量赋值（`f=logs/app.log`）没有副作用，允许；带空格的不算。
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")

# Windows 下 cmd 没有 grep/wc/tail，模型会自然改用 PowerShell。对 PowerShell 单独判：
# 出现写盘/执行类 cmdlet 就拒绝，其余（ReadLines/Measure-Object/Select-String 等）放行。
# 这是评估用的 pragmatic 白名单，不是通用沙箱。
_POWERSHELL_DENY = (
    "set-content", "out-file", "add-content", "remove-item", "new-item",
    "copy-item", "move-item", "rename-item", "start-process",
    "invoke-webrequest", "invoke-restmethod", "invoke-expression", "iex",
    "start-job", "stop-process", "remove-item", "rmdir", "mkdir",
)


def allow_readonly_terminal(
    *,
    deny: tuple[str, ...] = DEFAULT_DENY_TOOLS,
    commands: tuple[str, ...] = _READONLY_COMMANDS,
) -> Policy:
    """在 `allow_except` 的基础上，只批准**只读终端命令**。

    为什么需要它：评估默认把 `run_command` 整个拒掉（terminal 没有文件沙箱）。要测
    "终端错误识别 / 输出整形 / 长命令"又不能让模型随便执行命令，于是按命令族放行：
    `grep / wc / tail / head / cat / find / ls / stat` 等只读命令；含重定向或
    `sudo / rm / mv / cp` 的片段一律拒绝。`&&`、`||`、`;`、`|` 会逐段检查，防止
    `grep ... && rm ...` 这种绕过。
    """

    allowed = tuple(c.lower() for c in commands)
    deny_set = set(deny)

    def _powershell_allowed(text: str) -> bool:
        lowered = text.lower()
        if any(cmd in lowered for cmd in _POWERSHELL_DENY):
            return False
        # PowerShell 的输出重定向/命令替换同样可能写盘或执行任意代码。
        if any(frag in lowered for frag in (">", "`", "$(", "& ")):
            return False
        return True

    def _command_allowed(command: str) -> bool:
        text = (command or "").strip()
        if not text:
            return False
        first = text.split()[0].strip("\"'").lower().replace("\\", "/").rsplit("/", 1)[-1]
        if first in {"powershell", "pwsh", "powershell.exe", "pwsh.exe"}:
            return _powershell_allowed(text)
        lowered = text.lower()
        # `2>/dev/null` / `2>NUL` 只是丢弃 stderr，不写工作区，先剥掉再查重定向。
        lowered = re.sub(r"\d*>\s*(?:/dev/null|nul)\b", "", lowered)
        if any(frag in lowered for frag in _DENY_FRAGMENTS):
            return False
        segments = re.split(r"&&|\|\||;|\|", text)
        for segment in segments:
            segment = segment.strip()
            if not segment:
                return False
            if _ASSIGNMENT.match(segment):
                continue
            words = segment.split()
            head = words[0].strip("\"'").lower()
            # 允许 `C:\...\grep.exe` 这类绝对路径：取 basename 再比。
            head = head.replace("\\", "/").rsplit("/", 1)[-1]
            if head.endswith(".exe"):
                head = head[:-4]
            if head not in allowed:
                return False
        return True

    def policy(payload: dict) -> Decision:
        if _is_ask(payload):
            return Decision(kind="answer")
        name = payload.get("tool_name")
        if name == "run_command":
            command = (payload.get("tool_args") or {}).get("command", "")
            return Decision(kind="approval", approved=_command_allowed(command))
        return Decision(kind="approval", approved=name not in deny_set)

    return policy


# 开发型场景的终端白名单：只读命令 + 运行/测试脚本的 python/pytest。
# 不装依赖、不联网、不做破坏性操作；工作区仍是每任务临时目录。
_DEV_COMMANDS: tuple[str, ...] = ("python", "python3", "py", "pytest")

_DEV_DENY_FRAGMENTS: tuple[str, ...] = _DENY_FRAGMENTS + (
    "pip ", "pip3 ", "npm ", "npx ", "node ", "curl ", "wget ",
    "git clone", "git push", "git reset", "os.system", "subprocess", "shutil.rmtree",
)


def allow_dev_terminal(
    *,
    deny: tuple[str, ...] = DEFAULT_DENY_TOOLS,
    commands: tuple[str, ...] = _DEV_COMMANDS,
) -> Policy:
    """开发场景的终端策略：只读命令 + `python` / `pytest`。

    用于"多文件功能开发"这类任务：模型需要写文件、跑自己的脚本/测试，但不需要装依赖或
    联网。`pip/npm/curl/wget/git clone` 一律拒绝；输出重定向与破坏性命令也拒绝。
    这不是通用沙箱——`python <script>` 本身可以执行任意代码，只应跑在临时工作区。
    """

    allowed = set(_READONLY_COMMANDS) | set(commands)

    def _powershell_allowed(text: str) -> bool:
        lowered = text.lower()
        if any(cmd in lowered for cmd in _POWERSHELL_DENY):
            return False
        return not any(frag in lowered for frag in (">", "`", "$(", "& "))

    def _command_allowed(command: str) -> bool:
        text = (command or "").strip()
        if not text:
            return False
        first = text.split()[0].strip("\"'").lower().replace("\\", "/").rsplit("/", 1)[-1]
        if first in {"powershell", "pwsh", "powershell.exe", "pwsh.exe"}:
            return _powershell_allowed(text)
        lowered = re.sub(r"\d*>\s*(?:/dev/null|nul)\b", "", text.lower())
        if any(frag in lowered for frag in _DEV_DENY_FRAGMENTS):
            return False
        if first in allowed:
            # 运行脚本/测试时允许命令内部出现 ; 与 |（例如 python -c），
            # 但上面的 deny 片段已经挡住了安装、联网与常见破坏性调用。
            return True
        segments = re.split(r"&&|\|\||;|\|", text)
        for segment in segments:
            segment = segment.strip()
            if not segment:
                return False
            if _ASSIGNMENT.match(segment):
                continue
            head = segment.split()[0].strip("\"'").lower()
            head = head.replace("\\", "/").rsplit("/", 1)[-1]
            if head.endswith(".exe"):
                head = head[:-4]
            if head not in allowed:
                return False
        return True

    def policy(payload: dict) -> Decision:
        if _is_ask(payload):
            return Decision(kind="answer")
        name = payload.get("tool_name")
        if name == "run_command":
            command = (payload.get("tool_args") or {}).get("command", "")
            return Decision(kind="approval", approved=_command_allowed(command))
        return Decision(kind="approval", approved=name not in set(deny))

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
