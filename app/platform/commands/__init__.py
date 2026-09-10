"""基座的控制面命令（图之外）：把 `/` 前缀的一行输入解析成某个意图，再由基座处理。

定位（docs/ARCHITECTURE.md §2.7/§4）：命令是**基座**的事，与图无关——前端只负责取回一行
输入、把事件渲染出去，解析与处理都在本包。

命令分两类：

1. **提示词型（`PromptCommand`）**：把一段预设提示词**当用户输入投出去**，随后走一条正常
   turn（模型用已有工具干活，敏感动作照常撞审批）。基座不代做、也不为此起专门的一条 run。
   —— `/init`；`/fast readme [语言]`（载荷按参数渲染：给了 zh/cn/de/jp… 就用对应语言写，
   不给默认英语）。载荷在 `prompts.py`：常量字符串或收 args 的函数。
2. **基座动作型（`ActionCommand`）**：基座自己做的事——改/读运行时状态，结果经 `Notice`
   事件回给前端，**不起 turn**。 —— `/help`（本文件）；`/list session`、`/new session`、
   `/session <id>`（在 `session.py`，连同会话的 checkpoint 读取）。

规划中（**位置已留**）：`/content`（显示当前上下文占用量：数消息栈的字符/token → `Notice`）、
`/compact`（要求模型压缩上下文：把"压缩"提示词投成一轮 turn，与提示词型同路）。

包结构与分工：
- `__init__.py`（本文件）：机制 + **唯一一张命令表**（名字 → desc + 载荷/handler）+ 解析 +
  帮助文案。新命令 = 在对应子模块实现，再往下面的表里加一行。
- `prompts.py`：提示词型命令的载荷正文（大段散文搬出机制文件）。
- `session.py`：会话三条命令 + 会话数据访问（列出 / 新建 / 切换是一个子系统）。

命中规则：命令名可含空格（`/list session`），解析取**最长匹配**的名字，其后余下的部分作为
`args` 交给 handler（`/session <id>` 就靠它）。

退出词（`q`/`quit`/`exit`）仍留在 loop.py，未纳入本表。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

from app.platform.commands.prompts import INIT_PROMPT, fast_readme_prompt
from app.platform.commands.session import SESSION_ACTIONS
from app.schema.ui_schema import Notice

if TYPE_CHECKING:  # 只为类型：不真 import，避免与 loop 成环
    from app.platform.loop import AgentPlatform


@dataclass(frozen=True)
class PromptCommand:
    """提示词型命令：`prompt` 将被当作本轮的用户输入投给模型。

    这是**解析中间态**（不序列化、不出基座包），故不进 app/schema——那里只放跨边界的数据形状。
    """

    name: str
    prompt: str
    args: str = ""


@dataclass(frozen=True)
class ActionCommand:
    """基座动作型命令：`handler(platform, args)` 由基座直接执行（改运行时状态 / 读状态回显）。"""

    name: str
    handler: Callable[["AgentPlatform", str], Awaitable[None]]
    args: str = ""


# 一行输入的解析结果：命中某张表就是对应类型的命令，否则 None（普通消息 / 未识别命令）
Command = PromptCommand | ActionCommand


async def _show_help(platform: "AgentPlatform", args: str) -> None:
    """`/help`：把全部可用命令与说明回给前端（只读，不动任何状态；忽略 args）。"""
    platform.ui.emit(Notice(text=help_text()))


# ------------------------- 命令表（全仓唯一的命令清单）-------------------------

# 提示词型：名字 → {desc（给用户看的一行说明）, prompt}
# prompt 可以是**常量字符串**（不接参数的命令），也可以是**收 args 的函数**（按参数渲染提示词）。
PROMPT_COMMANDS: dict[str, dict] = {
    "/init": {
        "desc": "让 agent 通读当前工作区，生成或更新工作区根的 AGENT.md（项目画像）",
        "prompt": INIT_PROMPT,
    },
    "/fast readme": {
        "desc": "快速生成一个开源项目的 README；可给语言（zh/cn/de/jp…），默认英语",
        "prompt": fast_readme_prompt,
    },
}

# 基座动作型：名字 → {desc, handler}。加项即生效，无需改 loop.py。
ACTION_COMMANDS: dict[str, dict] = {
    "/help": {"desc": "显示所有可用命令与说明", "handler": _show_help},
    **SESSION_ACTIONS,
}


def _iter_commands():
    """两张表的 (名字, 条目) 汇总，按名字排序（帮助/提示文案的唯一来源）。"""
    merged = {**PROMPT_COMMANDS, **ACTION_COMMANDS}
    for name in sorted(merged):
        yield name, merged[name]


def help_text() -> str:
    """全部可用命令的一行说明（由两张表生成——别把命令清单另写一遍）。"""
    lines = [f"  {name:<14} {entry['desc']}" for name, entry in _iter_commands()]
    return "可用命令：\n" + "\n".join(lines)


# 未识别命令的提示（清单同源）
UNKNOWN_COMMAND_HINT = "未识别的命令。\n" + help_text()


def is_command(line: str) -> bool:
    """以 `/` 开头即视作命令——未识别的命令**不该被当成消息喂给模型**。"""
    return (line or "").strip().startswith("/")


def _render_prompt(entry: dict, args: str) -> str:
    """取一条提示词型命令的载荷：常量直接返回，函数则按 `args` 渲染（如 `/fast readme zh`）。"""
    prompt = entry["prompt"]
    return prompt(args) if callable(prompt) else prompt


def parse_command(line: str) -> Command | None:
    """解析一行输入：命中命令表返回对应命令，否则（普通文本/空行/未识别命令）返回 None。

    规则：先把行内空白折叠（`/session   abc` 与 `/session abc` 等价），再取**最长匹配**的
    命令名——名字可以含空格（`/list session`），其后余下的部分作为 `args` 交给 handler。

    "是命令但没识别出来"由调用方用 `is_command()` 另行判定——那时应当提示而不是起 turn。
    """
    text = " ".join((line or "").split())
    if not text:
        return None

    for name in sorted({**PROMPT_COMMANDS, **ACTION_COMMANDS}, key=len, reverse=True):
        if text == name:
            args = ""
        elif text.startswith(name + " "):
            args = text[len(name) + 1 :].strip()
        else:
            continue

        if name in PROMPT_COMMANDS:
            return PromptCommand(name=name, prompt=_render_prompt(PROMPT_COMMANDS[name], args), args=args)
        return ActionCommand(name=name, handler=ACTION_COMMANDS[name]["handler"], args=args)

    return None
