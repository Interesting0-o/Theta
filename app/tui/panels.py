"""终端渲染素材：审批面板 / 标题框 / 截断 / markdown 渲染——只把数据变成文本，不含判定也
不读输入。

位置：`app/tui/panels.py`（2026-09-10 基座/前端分家时从 app/tui/approval.py 拆出——判定面
归基座 `app/platform/approvals.py`，渲染留前端）。

本模块不发请求、不读 stdin、不改状态：除 `render_markdown` 往终端**写**渲染结果外都是纯函数
（renders 本身就是"把数据变成终端输出"，归此天经地义）。

markdown 渲染用 **rich**——它是**可选依赖**（pyproject 的 `ui` 组；实测常被 langchain /
langsmith 传递装上）：装了就把模型答复渲染成有标题/列表/代码块的样式，没装就退回纯文本原样
打印，不阻塞主流程（与 TAVILY_API_KEY 留空则跳过联网 server 同一取舍）。审批面板**不走**
它：那里要精确等宽、不能重排（见 format_tool_approval）。
"""
import json

try:  # 可选依赖：没装则两个名字为 None，render_markdown 据此退回纯文本
    from rich.console import Console
    from rich.markdown import Markdown
except ImportError:  # pragma: no cover —— 取决于环境是否装了 rich
    Console = Markdown = None  # type: ignore[assignment,misc]

# 界面标题：沿用仓库初始 main.py（git 历史 295a775 "初始化"）的 ASCII 框样式。
USER_TITLE = """
+------+
| User |
+------+
"""
AGENT_TITLE = """
+-------+
| Agent |
+-------+
"""
COMMAND_TITLE = """
+---------+
| Command |
+---------+
"""


def truncate(text: str, limit: int = 200) -> str:
    """超长文本截断显示，避免整屏刷出大段文件内容。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f" ...(共 {len(text)} 字符，已截断)"


def format_tool_approval(interrupts) -> str:
    """把待审请求格式化成易读的审批面板文本。

    接受 langgraph 的 Interrupt 列表（逐项取 `.value`），也接受已归一成 dict 的 value 列表
    ——后者是基座 `drain_approvals` 交过来、前端 `decide` 收到的形状（app/schema 的
    ApprovalRequest 语义：{tool_name, tool_args, description?, current_step?, tool_call_id?}）。
    直接 print repr 是一大坨，这里提取工具名/步骤/参数并做长内容截断。
    """
    lines: list[str] = []
    for item in interrupts:
        value = getattr(item, "value", item)
        if not isinstance(value, dict):
            lines.append(str(value))
            continue

        tool_name = value.get("tool_name", "?")
        step = value.get("current_step", "")
        tool_call_id = value.get("tool_call_id", "")

        header = f"[{tool_name}]"
        if step:
            header += f"   步骤 {step}"
        lines.append(header)

        # worker 的子任务原文：先给来龙去脉，再给解释与命令——远端 worker 的推理过程人看不到，
        # 少了这一行，人手上就只有一条来源不明的命令（见 app/schema/approval_schema.py）。
        if value.get("subtask"):
            lines.append(f"  子任务: {truncate(str(value['subtask']))}")

        args = value.get("tool_args", {})
        # 终端等工具带必填 description（模型对命令的人话解释）：单独一行突出展示，
        # 让人先看到"意图"，再核对下方命令本身是否一致。
        if isinstance(args, dict) and args.get("description"):
            lines.append(f"  解释: {truncate(str(args['description']))}")
            args = {k: v for k, v in args.items() if k != "description"}
        if args:
            lines.append("  参数:")
            if isinstance(args, dict):
                for k, v in args.items():
                    if isinstance(v, str):
                        lines.append(f"    - {k}: {truncate(v)}")
                    else:
                        lines.append(f"    - {k}: {json.dumps(v, ensure_ascii=False)}")
            else:
                lines.append(f"    - {json.dumps(args, ensure_ascii=False)}")

        if tool_call_id:
            lines.append(f"  调用ID: {tool_call_id}")

    return "\n".join(lines)


# ------------------------- markdown 渲染（模型答复） -------------------------

# 进程内单例 Console：写真实 stdout，宽度与终端能力（颜色 / 老 conhost 降级）由 rich 自己探测。
# 缓存在这里而不是每次渲染新建，是为了让 rich 只做一次能力探测。
_console = None


def _terminal_console():
    """终端渲染用的 Console 单例（rich 未装时不会被调用）。"""
    global _console
    if _console is None:
        _console = Console()
    return _console


def render_markdown(text: str, console=None) -> bool:
    """把模型答复的 markdown 渲染到终端；**rich 不可用或渲染失败 → 返回 False**。

    调用方（`TerminalUI.emit` 的 `TurnFinished` 分支）据此回退成原样 `print(text)`——返回值
    是"渲染过了吗"，不是"成功了吗"：两种 False 都只意味着"该由调用方自己打"。

    - console：可注入（测试传 `Console(file=StringIO(), width=…)` 捕获输出并固定换行宽度）。
      缺省用 `_terminal_console()` 写真实 stdout——**让 rich 自己处理终端能力**，比先捕获成
      字符串再 `print` 稳妥（Windows 老 conhost 的降级要靠 rich 写出去时才生效）。
    - 空白正文视为"无可渲染"直接返回 True：不打印，也不让调用方再打一行空行。
    - 渲染异常只降级、不冒泡：这是**展示层**，任何显示问题都不该吞掉一整轮答复（宁可读起来
      朴素，也不能让用户看不到答复）。
    """
    if not (text or "").strip():
        return True
    if Markdown is None:  # rich 没装（可选依赖）
        return False
    try:
        (console or _terminal_console()).print(Markdown(text))
    except Exception:  # noqa: BLE001 —— 见 docstring：展示层兜底，绝不吞掉答复
        return False
    return True
