"""协议块解析与工具状态判定（压缩引擎的**唯一**块解析入口）。

2026-11-04 从 `nodes.py` 抽出。职责边界：

- 只负责把消息流还原成 `ToolBlock`、判定 ToolMessage 状态、解析 read_file 行区间；
- 不决定“压不压”（策略在 `policy.py`），不切批（`planner.py`），不改 state。
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from .models import ToolBlock, ToolCallInfo
from .policy import WRITE_TOOLS

# **回退路径**用的 content 前缀表：只服务"戳落地前写进 checkpoint 的旧消息"。
# 取值必须与生产端的 `error_type` 一致（`mcp_service/utils.py::guard` 的分类 + 各 server
# 手写的 io_error）。`ConfigError` 经 `_type_token` 去 "Error" 后缀 → `config`（**不是**
# config_error——2026-09-21 核对时发现旧表这一项从来没匹配上过）。
FAIL_PREFIXES: tuple[str, ...] = (
    "[approval_denied]", "[workspace_violation]", "[io_error]", "[internal_error]",
    "[invalid_argument]", "[config]",
)


def one_line(text: str, limit: int = 120) -> str:
    """把多行文本**折叠成单行**（所有空白收敛成一个空格）再截断。

    ⚠️ 与 `first_line` **语义不同、刻意不合并**：本函数要的是"JSON 参数压成一行"
    （取首行会把参数丢掉一半），`first_line` 要的是"取首个非空行当摘要"。
    """
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def first_line(text: str, limit: int) -> str:
    """取首个非空行并截断——摘要行 detail 与 notes 标题共用。"""
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[:limit] + "…"


def tm_status(message) -> tuple[str, str]:
    """从一条 ToolMessage 判定其工具块状态。

    优先读 additional_kwargs 的结构化戳（键与构造器由生产端单点给出：ToolNode / ReviewNode
    经 `_tool_stamp` 写）：
    - decision=denied → denied（未执行）；
    - error_type=… → failed（该类型）。
    未挂戳的旧消息再回退解析 content 前缀。判定只信工具回执，绝不信模型 content。
    """
    extra = getattr(message, "additional_kwargs", None) or {}
    if extra.get("decision") == "denied":
        return "denied", "未执行"
    error_type = extra.get("error_type")
    if error_type:
        return "failed", str(error_type)

    # 回退：解析 content 前缀（兼容戳落地前 checkpoint 里的历史消息）
    text = str(getattr(message, "content", "") or "").strip()
    if not text:
        return "ok", ""
    if text.startswith("[approval_denied]"):
        return "denied", "未执行"
    for tag in FAIL_PREFIXES[1:]:
        if text.startswith(tag):
            return "failed", tag.strip("[]")
    if text.startswith(("工具不存在或未注册", "工具执行失败", "工具返回格式非法")):
        return "failed", first_line(text, 60)
    return "ok", ""


def is_consumed(region: list, start: int) -> bool:
    """块之后、下一条 HumanMessage 之前是否已出现 AIMessage。

    "已消费"= 模型已经对这批工具结果作出过反应（CONTEXT_ENGINEERING §3 不变量 2）。
    只扫到折叠区末尾（最后一条 Human 之前）：
    - 更早的轮次会先撞到下一条 Human → False（该轮以 ToolMessage 收尾，模型没反应过）；
    - 当前轮（最后一条 Human 及其后）本就不在 region 里。
    """
    for message in region[start:]:
        if isinstance(message, HumanMessage):
            return False
        if isinstance(message, AIMessage):
            return True
    return False


def in_progress_step(plan) -> str | None:
    """从 current_plan 取 in_progress 的 step id；没有返回 None。"""
    for step in plan or []:
        if isinstance(step, dict) and step.get("status") == "in_progress":
            step_id = step.get("id")
            return str(step_id) if step_id is not None else None
    return None


def read_range(args: dict) -> tuple[int, int | None] | None:
    """把 read_file 参数折成请求范围；None = 无行区间的全文请求。

    `end_line=None` 表示"从 start_line 往后"（大文件最多 200 行、小文件到文件尾），
    这里保留 None，交给覆盖检查按"到文件尾/单次上限"处理。
    """
    start = args.get("start_line")
    end = args.get("end_line")
    start = start if isinstance(start, int) and start >= 1 else None
    end = end if isinstance(end, int) and end >= 1 else None
    if start is None and end is None:
        return None
    return (start or 1, end)


def iter_tool_blocks(messages, initial_plan=None) -> tuple[list[ToolBlock], list[int]]:
    """把消息序列解析成 tool-call block，并记录工具块之外的消息位置。

    plan step 从 `create_plan` / `update_plan_step` / `clear_plan` 的调用历史重建：
    `create_plan` 开启新 epoch；`in_progress` 把 block 归到新 step；`done` / `pending`
    只结束当前 step。**只认成功调用**：失败/被拒/未兑现的 plan 调用不改变分组边界。
    `step=None` 是合法段（模型还没声明 step 或已 clear）。
    """
    blocks: list[ToolBlock] = []
    boundaries: list[int] = []
    plan_epoch = 0
    active_step = in_progress_step(initial_plan)
    i = 0
    n = len(messages)
    while i < n:
        message = messages[i]
        kind = type(message).__name__
        if kind != "AIMessage" or not getattr(message, "tool_calls", None):
            boundaries.append(i)
            i += 1
            continue

        calls = list(message.tool_calls)
        call_ids = [call.get("id") for call in calls if isinstance(call, dict)]
        claimed: dict[str, ToolMessage] = {}
        j = i + 1
        while j < n and type(messages[j]).__name__ == "ToolMessage":
            tool_message = messages[j]
            call_id = getattr(tool_message, "tool_call_id", None)
            if call_id in call_ids and call_id not in claimed:
                claimed[call_id] = tool_message
                j += 1
            else:
                break

        step_before = active_step
        new_step = None
        for call in calls:
            if not isinstance(call, dict):
                continue
            # 只认成功调用：失败/被拒/未兑现的 plan 调用没有改变计划，不能当分组边界。
            call_id = call.get("id")
            tool_message = claimed.get(call_id) if call_id else None
            status = tm_status(tool_message)[0] if tool_message is not None else "missing"
            if status in {"failed", "denied", "missing"}:
                continue
            name = (call.get("name") or "").strip()
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            if name == "create_plan":
                plan_epoch += 1
                active_step = None
                new_step = None
            elif name == "update_plan_step":
                step_id = args.get("step_id")
                step_id = str(step_id) if step_id is not None else None
                status = args.get("status")
                if status == "in_progress":
                    active_step = step_id
                    new_step = step_id
                elif status in {"done", "pending"} and active_step == step_id:
                    active_step = None
            elif name == "clear_plan":
                active_step = None

        call_infos: list[ToolCallInfo] = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = call.get("id")
            tool_message = claimed.get(call_id) if call_id else None
            status = tm_status(tool_message)[0] if tool_message is not None else "missing"
            call_infos.append(
                ToolCallInfo(
                    call_id=call_id,
                    name=(call.get("name") or "").strip() or "?",
                    args=call.get("args") if isinstance(call.get("args"), dict) else {},
                    tool_message=tool_message,
                    status=status,
                )
            )

        complete = (
            len(claimed) == len(call_ids)
            and all(call_id is not None for call_id in call_ids)
            and all(isinstance(call, dict) for call in calls)
        )
        ids_ok = all(getattr(m, "id", None) for m in [message, *claimed.values()])
        consumed = False
        for k in range(j, n):
            if type(messages[k]).__name__ == "HumanMessage":
                break
            if type(messages[k]).__name__ == "AIMessage":
                consumed = True
                break
        milestone = any(
            call.name in WRITE_TOOLS
            or call.name in {"start_process", "process_kill", "update_plan_step", "create_plan", "clear_plan"}
            or call.status in {"failed", "denied"}
            for call in call_infos
        )
        blocks.append(
            ToolBlock(
                ai_message=message,
                calls=call_infos,
                start=i,
                end=j - 1,
                complete=complete,
                ids_ok=ids_ok,
                consumed=consumed,
                plan_epoch=plan_epoch,
                step_id=new_step if new_step is not None else step_before,
                milestone=milestone,
            )
        )
        i = j
    return blocks, boundaries
