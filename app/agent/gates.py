"""闸门判定材料：**挂起之前必须成立**的那几样判定**（2026-09-21 从 `nodes.py::ReviewNode` 剥出）。

本模块的名字刻意不叫 `policy`——它装的比"策略"宽：

1. **策略表的读取单点**：`tool.json`（`tool_config`）——审批分流、worker 可用子集都从这一份读，
   原先三个读者两份缓存（`ReviewNode` 一份 lru_cache、`tools.py` 一份、`worker_tools` 干脆不缓存），
   现在是一份缓存一个入口；
2. **命令级免审判定**（`free_shell_verdict` + `command_policy` + `SHELL_METACHARS`）：`run_command`
   的只读形态不弹面板的判定链；
3. **提问闸门的输入校验**（`ask_request` / `normalize_ask_answer`）：空问题、超限选项**必须在弹面板
   之前**拦掉——把一个空问题摆到人面前是最糟的形态。

**为什么都在这里、而不各归各的落点**（2026-09-21 拍板，理由见 docs/DONE.md 的「节点过度实现」/「`app/agent/` 的判据」两条归档）：

- 三样都是"闸门挂起前必须成立的判定材料"，与 `ReviewNode` 的挂起决策是同一件事的两半；
- 提问校验按定义属于 `tools.py::ask_user` 这个 action 的参数校验，但**搬去 `tools.py` 会造一条
  `nodes → tools` 的边**（`nodes.py` 刻意不 import `tools.py`：它拿的是构图期传进来的工具对象），
  而它又必须在弹面板之前跑完，所以落在闸门这一侧；
- **策略表（tool.json / command_policy.json）刻意留在代码旁边、不进资源层**：那是纪律不是整洁
  ——`SHELL_METACHARS` 是解析器的**词法定义**，把它挪进数据文件等于让人能在不改代码的情况下削弱
  解析器（把 `&&` 从表里删掉，命令拼接的口子就开了）。表与解析器必须住在一起。

本模块只依赖 stdlib + `app/schema`，不碰 state、不碰图、不碰工具对象——**纯判定**，便于单测。
"""
from __future__ import annotations

import json
import shlex
from functools import lru_cache
from pathlib import Path

from app.schema.agent_schema import ASK_SELECT_DEFAULT, ASK_SELECT_MODES

_TOOL_CONFIG_PATH = Path(__file__).with_name("tool.json")


@lru_cache(maxsize=1)
def tool_config() -> dict[str, dict]:
    """`tool.json` 的解析结果：`{tool_name: {need_review, source, worker_allow?, free_when?}}`。

    整进程只读一次。**这是审批与分流的策略单点**，两个消费者：
    - `ReviewNode`（`_tool_cfg`：内置表优先，其次已加载技能自己的 skill.json 声明）；
    - `tools.py::worker_tools`（按 source / worker_allow 筛 worker 可用子集）。

    ⚠️ 因为缓存"读一次用一辈子"，**改 tool.json 要重启进程**（含给新技能登记工具）。
    """

    with _TOOL_CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


#----------------------终端命令的免审判定--------------------

# 免审命令的**策略表是数据，不是代码**：加一族命令 = 改 app/agent/command_policy.json，
# 判定逻辑一行不动——这就是"把命令审核从逻辑里解耦出来"的具体含义。
#
# 为什么要这张表：终端工具在 tool.json 里一律 need_review:true，唯独落在表里的形态免人工审批。
# 这是**新增授权**（不是把旧行为平移过来）——git 用法一律走 run_command（原先那批 git_* 原子
# 工具已随 mcp_service/git.py 一并删除），只读的读操作若每次都要人按 y，摩擦就是净增。
_COMMAND_POLICY_PATH = Path(__file__).with_name("command_policy.json")


@lru_cache(maxsize=1)
def command_policy() -> dict:
    """command_policy.json 的解析结果（整进程只读一次，口径同 tool.json）。

    列表在这里归一成元组/冻结集合——调用方要的是"能 startswith 的序列"和"能 O(1) 判成员的
    集合"，别把裸 list 递给 `str.startswith`（它只认 str 或 tuple）。
    """
    with _COMMAND_POLICY_PATH.open("r", encoding="utf-8") as file:
        raw = json.load(file)
    return {
        "free_commands": {name: frozenset(subs) for name, subs in raw["free_commands"].items()},
        "write_flags": tuple(raw["git_write_flags"]),
        "global_skip": frozenset(raw["git_global_skip"]),
        "branch_list_flags": frozenset(raw["git_branch_list_flags"]),
    }


# cmd.exe 的元字符：出现即不判免审。`;` `&&` `|` `>` 能改写一条命令的作用域与去向，
# `^` 是 cmd 的转义符（可用来拆词绕过白名单）。换行同理（一条命令伪装成两条）。
#
# **这一条刻意留在代码里、不进 JSON**：它是解析器的**词法定义**，不是审批策略。放进数据文件
# 意味着有人能在不改代码的情况下削弱解析器——把 `&&` 从表里删掉，命令拼接的口子就开了。
# 词法事实也没有"按工作区 / 按命令族调"的诉求。
# 注：`%` 是 cmd 的变量展开符，但它同时是 git 格式化串的常客（`--format=%h`），而只读 git
# 命令里它不构成写向量——等 `cat` 那类**按路径判定**的命令进表时，再连它一起处理。
SHELL_METACHARS: tuple[str, ...] = (";", "&&", "||", "|", "&", "`", "$(", ">", "<", "^", "\n", "\r")




def free_shell_verdict(tool_args: dict) -> bool:
    """run_command 的免审判定：这条命令是名单里的**只读**形态 → True（不弹审批面板）。

    保守优先（fail-closed）：拿不准一律 False，照常弹面板让人看一眼——误判成 review 只是多
    按一次 y，误判成 free 却是静默放行一次没人看过的命令。判定链（全过才 True）：
    ① 命令文本不含 cmd 元字符；② 能切分出词；③ 首词在免审命令表里；
    ④ 跳过 git 全局选项后子命令在白名单；⑤ 无写文件选项；⑥ branch 只认列分支用法。

    ③–⑥ 的口径全部来自 `app/agent/command_policy.json`（经 `command_policy()`）——加一族
    命令是改那张表，不是改本函数。①（元字符）刻意留在代码里，理由见 `SHELL_METACHARS`。
    """
    policy = command_policy()
    command = tool_args.get("command")
    if not isinstance(command, str) or not command.strip():
        return False
    if any(char in command for char in SHELL_METACHARS):
        return False
    try:
        # posix=False 是**必须的**：终端跑在 cmd.exe 上，POSIX 规则会把 `C:\work\a` 的
        # 反斜杠当转义符吃掉，切出与实际执行不同的词串——而"解析结果 ≠ 实际执行的命令"
        # 正是这类判定最不能有的性质。
        tokens = shlex.split(command, posix=False)
    except ValueError:  # 引号不闭合等
        return False
    if not tokens:
        return False

    head = tokens[0].strip("\"'").lower()
    allowed = policy["free_commands"].get(head)
    if allowed is None:
        return False

    # 找子命令：跳过带值的全局选项（`-C <dir>` 占两个词）
    index = 1
    while index < len(tokens):
        token = tokens[index].strip("\"'")
        if token in policy["global_skip"]:
            index += 2 if token == "-C" else 1
            continue
        break
    if index >= len(tokens):
        return False
    subcommand = tokens[index].strip("\"'")
    if subcommand not in allowed:
        return False

    rest = [token.strip("\"'") for token in tokens[index + 1 :]]
    if any(token.startswith(policy["write_flags"]) for token in rest):
        return False
    if subcommand == "branch":
        # 只认列分支：不能有位置实参，选项也得在列表 flag 白名单里（挡住 -d/-D/-m/-c …）
        return all(token in policy["branch_list_flags"] for token in rest)
    return True

# ---------------------- 提问闸门的参数契约 ----------------------
# 选项上限：面板与人的注意力都有限，"5 个选项比 1 个问题更难答"。开放式提问（0 项）不限。
MAX_ASK_OPTIONS = 5

# 题目的模态词表（单选 / 复选）归 `app/schema/agent_schema.py`：它**多处使用**（本模块校验、
# tools 的签名、approvals 透传、panels/tui 的渲染分支），按 §4.6 该由 schema 给出唯一一份，
# 消费方 import——这里只保留"校验它在不在取值域内"这条判定。


def ask_request(args: dict) -> tuple[dict | None, str | None]:
    """校验并归一提问参数，返回 `(归一后的请求, 问题说明)`；无问题时前者为 None。

    **为什么校验在闸门这里**：编排工具不走 langchain 的参数校验管线
    （`OrchestrateNode._invoke` 直接调底层函数，见该处说明），"必填"与"上限"都得自己拦。
    而拦的位置必须是**弹面板之前**——把一个空问题、或 8 个选项摆到人面前，比回一条可行动回执
    让模型自己改要糟得多。
    """
    why = str(args.get("why") or "").strip()
    question = str(args.get("question") or "").strip()
    raw_options = args.get("options")
    options = (
        [str(item).strip() for item in raw_options if str(item).strip()]
        if isinstance(raw_options, list)
        else []
    )
    # 缺省 = 单选（也是历史行为的默认值，模型不传就一切照旧）
    select = str(args.get("select") or ASK_SELECT_DEFAULT).strip()

    problems: list[str] = []
    if not why:
        problems.append("why（为什么问）为空")
    if not question:
        problems.append("question（问题本身）为空")
    if len(options) > MAX_ASK_OPTIONS:
        problems.append(f"options 给了 {len(options)} 项，超过上限 {MAX_ASK_OPTIONS} 项")
    if select not in ASK_SELECT_MODES:
        # fail-closed：模态说不清就不摆问题（与 why 为空同档），回执教它怎么改
        problems.append(
            f"select 只能是 {' / '.join(repr(m) for m in ASK_SELECT_MODES)}，收到 {select!r}"
        )
    if problems:
        return None, (
            "提问未发出：" + "；".join(problems) + "。请补齐后重发"
            f"（options 至多 {MAX_ASK_OPTIONS} 项，也可以留空做开放式提问）。"
        )
    return {"why": why, "question": question, "options": options, "select": select}, None


def normalize_ask_answer(decision, options: list[str]) -> dict:
    """把闸门收到的回答归一成 `AskAnswer`（写进 state.ask_answers 的形状）。

    - **序号按 options 逐个校验**：越界的、非整数的**丢掉那一项**，保留其余合法的，并去重保序
      （单选与复选走同一段代码，单选只是长度 0 或 1 的特例）。取向同 `_decide` 的 fail-closed
      ——宁可让模型看到"用户没选这几项"，也不能把对不上号的序号当成有效选择喂过去。
      面板侧在输入时就提示过越界（单选直接重问），所以这里**不把丢弃项回灌给模型**；
    - 补充去首尾空白，空串归一成 None——让"**两段皆空 = 未回答**"这条判据在 state 里直接可读，
      消费端不必各自再判一次空白。
    """
    payload = decision if isinstance(decision, dict) else {}
    raw = payload.get("option_indexes")
    indexes: list[int] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, int) and 0 <= item < len(options) and item not in indexes:
            indexes.append(item)
    supplement = str(payload.get("supplement") or "").strip() or None
    return {"option_indexes": indexes, "supplement": supplement}
