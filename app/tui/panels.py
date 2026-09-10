"""终端渲染素材：审批面板 / 标题框 / 截断——只把数据变成文本，不含判定也不读输入。

位置：`app/tui/panels.py`（2026-09-10 基座/前端分家时从 app/tui/approval.py 拆出——判定面
归基座 `app/platform/approvals.py`，渲染留前端）。

本模块只有纯函数与常量：不发请求、不读 stdin、不改状态。
"""
import json

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
