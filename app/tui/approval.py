"""审批的**判定面**：渲染 + 收 y/n + 回填统一 broker（driver 的事件循环消费）。

从 app/main.py 迁入并单点化（docs/MULTI_AGENT.md §6 统一审批视图）。职责边界：
- 判定权威在人，本模块是**人审的交互实现**（渲染 + 提问 + 决定回填），不产生决定本身
  （决定由人给出）；放行策略在图的 review 配置层，不在此。
- 主 agent 自身 interrupt 与 worker HTTP 审批**一视同仁**：都化作 broker 里的一条
  ApprovalRecord，经 `_record_to_value` 渲染成面板、`decide_approval` 收 y/n、由
  `drain_approvals` 调 `inbox.complete` 回填（唤醒 park 的本地 run 或长轮询的 worker）。
  差异只在 payload 的 `worker_id` 标记（本地="main"，远端=真实 worker id）。

提供：
- `truncate` / `format_tool_approval`：面板渲染（长内容截断）。
- `_record_to_value`：一条 ApprovalRecord → 与 ReviewNode interrupt 同构的 value
  （worker_id=="main" 渲染成"主 agent"，否则 "worker:<id>"）。
- `decide_approval`：渲染一条 value + 收 y/n + 返回是否批准。
- `drain_approvals`：排空 broker 里全部 pending（本地 + worker）——每个一个面板。
"""
import json
from types import SimpleNamespace

from app.schema.approval_schema import ApprovalRecord
from app.tui.input import _ask_yes_no

# 本地主 agent 中断入 broker 时用的 worker_id 标记（远端 worker 用真实 id）
LOCAL_WORKER_ID = "main"

# 审批面板标题：沿用仓库初始 main.py（git 295a775）的 ASCII 框样式
_COMMAND_TITLE = """
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
    """
    把 langgraph 的 Interrupt 列表格式化成易读的审批信息。

    原始数据形如 [Interrupt(value={'type': 'tool_approval', 'tool_name': ...,
    'tool_args': {...}}, id='...')]，直接 print 会是一大坨难看的 repr。
    这里提取出工具名、步骤、参数、调用ID，并对长内容做截断。
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


def _record_to_value(record: ApprovalRecord) -> dict:
    """把 broker 里一条待审记录归一成 interrupt 同构的 value。

    value 与 ReviewNode interrupt payload 对齐：{tool_name, current_step, tool_args,
    description?, tool_call_id?}，供 decide_approval 渲染。
    - 步骤：本地主 agent 中断带 driver 塞的 ReviewNode current_step（如 "1/1"，保持旧面板
      样式）；远端 worker 无该字段时回退为来源标记（主 agent / worker:<id>）。
    - tool_call_id：仅本地中断携带（纯展示）。
    description 在 payload 层（ApprovalRequest 语义），format_tool_approval 主要展示
    tool_args.description。
    """
    payload = record.get("payload") or {}
    worker_id = payload.get("worker_id", "")
    step = payload.get("current_step") or (
        "主 agent" if worker_id == LOCAL_WORKER_ID else f"worker:{worker_id}"
    )
    value: dict = {
        "tool_name": payload.get("tool_name", "?"),
        "tool_args": payload.get("tool_args") or {},
        "description": payload.get("description"),
    }
    if step:
        value["current_step"] = step
    if payload.get("tool_call_id"):
        value["tool_call_id"] = payload["tool_call_id"]
    return value


async def decide_approval(value: dict, out_q) -> bool:
    """审核判定单元：渲染 Command 框 + 面板，向用户收 y/n、返回是否批准。

    沿用初始 main.py 样式：`print(command_title)` → 工具面板 → 空行 → 问 y/n。
    这里是人审的交互实现，不产生其它副作用；决定由调用方消费
    （主图 resume → Command(resume)；worker 请求 → queue.complete）。
    """
    print(_COMMAND_TITLE)
    print(format_tool_approval([SimpleNamespace(value=value)]))
    print()
    return await _ask_yes_no(out_q, "是否批准该操作？(y/n): ")


async def drain_approvals(inbox, out_q) -> None:
    """排空 broker 里全部 pending（本地主 agent interrupt + worker HTTP 请求）。

    逐条渲染 Command 框 + 收 y/n → inbox.complete 回填（唤醒 park 的本地 run / 长轮询的
    worker）。判定权威在人；一条面板只服务一个请求，判定互不阻塞进程。
    """
    for entry in inbox.pending():
        approved = await decide_approval(_record_to_value(entry), out_q)
        inbox.complete(entry["approval_id"], approved)
    inbox.clear_new()
