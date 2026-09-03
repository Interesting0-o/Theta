"""agent 侧共享工具函数（工具函数都放这里，不放节点/工具模块）。

- `format_tool_result`：把工具返回的对象转成大模型实际读到的文本
  （EXCEPTION_DESIGN.md §5 / §7）。分类已发生在边界（guard / MCP server），
  这里只负责"提取正文"。
- `format_tool_approval` / `truncate` / `get_agent_db_path`：从 app/main.py 的
  TUI 迁来的辅助函数（审批信息排版、长文本截断、checkpoint 数据库路径）。
- `get_tool_config`：tool.json 的共享缓存加载（ReviewNode/PlanNode 复用，避免各自读文件）。
- 计划模式纯函数：`steps_to_plan` / `apply_step_status` / `empty_plan` / `plan_snapshot`，
  供 tools.py 的工具与 nodes.py 的 PlanNode 共用（单一事实来源）。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.schema.agent_schema import PlanStep, ToolResult

_TOOL_CONFIG_PATH = Path(__file__).with_name("tool.json")

PLAN_STATUSES: tuple[str, ...] = ("pending", "in_progress", "done")

# 编排类工具在 tool.json 中的 source 取值集合：命中即视为编排调用，
# 由 review_node 分流进 approved_orchestrate_calls，交 orchestrate_node 处理。
ORCHESTRATE_SOURCES: frozenset[str] = frozenset({"plan"})


# ---------------- 计划纯状态转换函数 ----------------

def steps_to_plan(steps: list[str]) -> list[PlanStep]:
    """把顺序任务列表转成计划步骤；空白项跳过；id 从 "1" 起分配。"""
    plan: list[PlanStep] = []
    for task in steps:
        if not isinstance(task, str) or not task.strip():
            continue
        plan.append({"id": str(len(plan) + 1), "task": task, "status": "pending"})
    return plan


def apply_step_status(
    plan: list[PlanStep], step_id: str, status: str
) -> tuple[list[PlanStep], bool, str]:
    """把指定步骤置为新状态。成功时返回(新列表, True, 消息)；原列表不变。

    失败（status 非法 / step_id 不存在）返回(原对象, False, 消息)，
    调用方可用 `new is plan` 判断是否无改动。
    """
    if status not in PLAN_STATUSES:
        return (
            plan,
            False,
            f"非法状态 {status!r}（可用：{' / '.join(PLAN_STATUSES)}）",
        )
    idx = next((i for i, s in enumerate(plan) if s.get("id") == step_id), None)
    if idx is None:
        return plan, False, f"步骤 {step_id} 不存在"
    new_plan = list(plan)
    new_plan[idx] = {**plan[idx], "status": status}
    return new_plan, True, f"步骤 {step_id} 已标记为 {status}"


def empty_plan() -> list[PlanStep]:
    """清空后的计划。"""
    return []


def plan_snapshot(plan: list[PlanStep]) -> str:
    """把计划渲染成模型可读的清单文本（done 打勾，便于模型跨轮跟踪进度）。"""
    lines = []
    for step in plan:
        mark = "x" if step.get("status") == "done" else " "
        lines.append(f"- [{mark}] {step.get('id')}. {step.get('task')}")
    return "\n".join(lines)


# ---------------- tool.json 共享加载 ----------------

@lru_cache(maxsize=1)
def get_tool_config() -> dict[str, dict]:
    """返回 tool.json 解析结果：{tool_name: {need_review, source}}。整进程只读一次。"""
    with _TOOL_CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


# ---------------- 计划模式：纯状态转换 ----------------

# ---------------- 模型工具结果 / TUI 辅助 ----------------

def format_tool_result(result) -> str:
    """把工具返回统一格式化为模型可见的字符串。

    处理顺序：
    - `ToolResult`：取 `content`，若有 `error_type` 则加 `[error_type] ` 前缀，
      让模型一眼看出这是哪一类失败（如 "[workspace_violation] 路径…"）。
    - `dict`：优先取 `content` 字段；若 content 是 content-block 列表则逐个取
      `text` 拼接（兼容 langchain MCP 适配器的返回形态）。
    - 其它（str 等）：`str()` 原样透传。

    注意：模型读到的文本由这里决定，而不是 Python 侧的 `ToolResult.__str__`
    （MCP 跨进程返回时走的是 FastMCP 的 JSON 序列化，见 mcp_service/utils.py 注释）。
    """
    if isinstance(result, ToolResult):
        prefix = f"[{result.error_type}] " if result.error_type else ""
        return prefix + result.content

    if isinstance(result, dict):
        content = result.get("content", result)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(block.get("text") or block.get("content") or "")
                else:
                    parts.append(str(block))
            return "\n".join(part for part in parts if part)
        return str(content)

    return str(result)


def get_agent_db_path() -> Path:
    """agent checkpoint 的持久化 SQLite 路径（项目根/resource/agent.db，自动建目录）。"""
    db_dir = Path(__file__).resolve().parent.parent.parent / "resource"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "agent.db"


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
