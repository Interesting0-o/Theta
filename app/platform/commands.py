"""基座的控制面命令（图之外）：把 `/` 前缀的一行输入解析成某个意图，再由基座处理。

定位（docs/ARCHITECTURE.md §2.7/§4）：命令是**基座**的事，与图无关——前端只负责取回一行
输入、把事件渲染出去，解析与处理都在这里。

命令分两类（本轮只落地第一类的 `/init`，第二类**位置已留好**）：

1. **提示词型（`PromptCommand`）**：把一段预设提示词**当用户输入投出去**，随后走一条正常
   turn（模型用已有工具干活，敏感动作照常撞审批）。基座不代做、也不为此起专门的一条 run。
2. **基座动作型（`ActionCommand`）**：基座自己做的事——改/读运行时状态，结果经 `Notice`
   事件回给前端。已落地 `/help`（列出全部命令，由两张表生成）；规划中（**位置已留**）：
   - `/session`  切换会话：换 checkpoint / thread_id（关旧连接 → 以目标 id 重开
     AsyncSqliteSaver → 重置 runtime 的 step），列出 `resource/<ws>/sessions/` 供选；
   - `/content`  显示当前上下文占用量：数当前消息栈的字符/token → `Notice`（只读）；
   - `/compact`  要求模型压缩上下文：把"压缩"提示词投成一轮 turn（与提示词型同路），
     或改由基座触发 compact 节点（形态待定）。

退出词（`q`/`quit`/`exit`）仍留在 loop.py，未纳入本表。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

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


@dataclass(frozen=True)
class ActionCommand:
    """基座动作型命令：`handler(platform)` 由基座直接执行（改运行时状态 / 读状态回显）。"""

    name: str
    handler: Callable[[AgentPlatform], Awaitable[None]]


# 一行输入的解析结果：命中某张表就是对应类型的命令，否则 None（普通消息 / 未识别命令）
Command = PromptCommand | ActionCommand


_INIT_PROMPT = """\
请生成或更新本工作区的项目画像文件 AGENT.md。

"生成或更新"都由**写入**完成：文件不存在就新建，已存在就更新——不要预设它一定是新建的。

AGENT.md 会随仓库走、并在之后**每个会话开始时被读入系统上下文**，所以它写的是"这个项目长期
成立的事实"，不是本次任务的记录（临时结论写进长期记忆或直接答复即可）。

怎么做：
1. 先摸清现状，**不要凭猜测写**：get_directory_tree / list_dir 看布局，读 README（若有）、
   依赖与构建清单（pyproject.toml / package.json / requirements.txt / Makefile 等），
   必要时用 search_content 定位主流程、入口与测试目录。
2. 落盘 = **写入工作区根目录的 "AGENT.md"**（相对路径直接写 "AGENT.md"）：
   - **不存在** → 用 create_file 新建；
   - **已存在** → 先 read_file 看懂现状再更新：改动小用 edit_file 逐处刷新，改动面覆盖大半
     才用 write_file 整文件重写；两条路都要**保住仍然成立的内容**，别把已攒下的共识写丢。
   （AGENT.md 是可反复刷新的文件，不必一次写到完美。）
3. 内容按"项目整体画像"分块写清：
   - 这个项目是做什么的（一两句）；
   - 目录与技术栈：关键目录各放什么、用什么语言/框架/包管理器；
   - 主流程与入口：程序从哪进、一次请求或一次运行怎么走；
   - 怎么跑：构建、启动、跑测试的**具体命令**；
   外加**容易踩的坑或必须遵守的约定**（如测试必须怎么跑、哪些目录不要动）。
4. 写法：中文、简明分节（Markdown 小标题）、只写事实；**不要把大段代码抄进来**，需要时给出
   文件路径让人自己去看；篇幅控制在能一眼扫完的量级。

完成后用一两句说明 AGENT.md 落在哪、这次是新建还是更新、写了哪几块，不必复述全文。\
"""

# 提示词型命令表：名字 → {desc（给用户看的一行说明）, prompt（投给模型的预设提示词）}
PROMPT_COMMANDS: dict[str, dict[str, str]] = {
    "/init": {
        "desc": "让 agent 通读当前工作区，生成或更新工作区根的 AGENT.md（项目画像）",
        "prompt": _INIT_PROMPT,
    },
}

# 基座动作型命令表：名字 → {desc, handler}。handler(platform) 由基座直接执行（改运行时状态
# 或读状态回显），结果经 `Notice` 回给前端。规划中（位置已留）：/session 换 checkpoint、
# /content 显示上下文占用量、/compact 要求模型压缩上下文（见模块 docstring）。加项即生效，
# 无需改 loop.py。
ACTION_COMMANDS: dict[str, dict] = {}


async def _show_help(platform: "AgentPlatform") -> None:
    """`/help`：把全部可用命令与说明回给前端（只读，不动任何状态）。"""
    platform.ui.emit(Notice(text=help_text()))


ACTION_COMMANDS["/help"] = {
    "desc": "显示所有可用命令与说明",
    "handler": _show_help,
}


def _iter_commands():
    """两张表的 (名字, 条目) 汇总，按名字排序（帮助/提示文案的唯一来源）。"""
    merged = {**PROMPT_COMMANDS, **ACTION_COMMANDS}
    for name in sorted(merged):
        yield name, merged[name]


def help_text() -> str:
    """全部可用命令的一行说明（由两张表生成——别把命令清单另写一遍）。"""
    lines = [f"  {name:<8} {entry['desc']}" for name, entry in _iter_commands()]
    return "可用命令：\n" + "\n".join(lines)


# 未识别命令的提示（清单同源）
UNKNOWN_COMMAND_HINT = "未识别的命令。\n" + help_text()


def is_command(line: str) -> bool:
    """以 `/` 开头即视作命令——未识别的命令**不该被当成消息喂给模型**。"""
    return (line or "").strip().startswith("/")


def parse_command(line: str) -> Command | None:
    """解析一行输入：命中命令表返回对应命令，否则（普通文本/空行/未识别命令）返回 None。

    "是命令但没识别出来"由调用方用 `is_command()` 另行判定——那时应当提示而不是起 turn。
    """
    text = (line or "").strip()

    prompt_entry = PROMPT_COMMANDS.get(text)
    if prompt_entry is not None:
        return PromptCommand(name=text, prompt=prompt_entry["prompt"])

    action_entry = ACTION_COMMANDS.get(text)
    if action_entry is not None:
        return ActionCommand(name=text, handler=action_entry["handler"])

    return None
