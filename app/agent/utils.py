"""agent 侧共享工具函数（与"agent 专属/编排工具"无关的通用辅助都放这里）。

- `format_tool_result`：把工具返回的对象转成大模型实际读到的文本
  （EXCEPTION_DESIGN.md §5 / §7）。分类已发生在边界（guard / MCP server），
  这里只负责"提取正文"。
- `format_tool_approval` / `truncate` / `get_agent_db_path`：从 app/main.py 的
  TUI 迁来的辅助函数（审批信息排版、长文本截断、checkpoint 数据库路径）。
- `get_tool_config`：tool.json 的共享缓存加载（ReviewNode 等复用，避免各自读文件）。
- `ORCHESTRATE_SOURCES`：编排类工具的 source 集合，供 ReviewNode 分流判断。

计划（plan）的核心逻辑不在本文件：编排工具 `create_plan` / `update_plan_step` /
`clear_plan` 自身实现，见 app/agent/tools.py。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.schema.agent_schema import ToolResult, PlanStep

_TOOL_CONFIG_PATH = Path(__file__).with_name("tool.json")

# 编排类工具在 tool.json 中的 source 取值集合：命中即视为编排调用，
# 由 review_node 分流进 approved_orchestrate_calls，交 orchestrate_node 处理。
ORCHESTRATE_SOURCES: frozenset[str] = frozenset({"plan"})


# ---------------- tool.json 共享加载 ----------------

@lru_cache(maxsize=1)
def get_tool_config() -> dict[str, dict]:
    """返回 tool.json 解析结果：{tool_name: {need_review, source}}。整进程只读一次。"""
    with _TOOL_CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


# ---------------- 模型工具结果 / TUI 辅助 ----------------

def format_tool_result(result) -> str:
    """把工具返回统一格式化为模型可见的字符串。

    处理顺序：
    - `ToolResult`：取 `content`，若有 `error_type` 则加 `[error_type] ` 前缀，
      让模型一眼看出这是哪一类失败（如 "[workspace_violation] 路径…"）。
    - `dict`：优先取 `content` 字段；若 content 是 content-block 列表则逐个取
      `text` 拼接（兼容 langchain MCP 适配器的返回形态）。
    - `list`：MCP 工具经 ToolNode 拿到的真实形态——langchain MCP 适配器把每个
      content block 转成 dict（text/id 等），FastMCP 又把工具返回的 ToolResult
      整包 JSON 化进 text。这里逐个取 `text`（丢弃随机 id 噪声），json.loads
      还原 ToolResult 后交上面 ToolResult 分支；还原失败（非 JSON / 非
      ToolResult 形态，如未来直接返回 str 的工具）则拼接后原样透传。
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

    if isinstance(result, list):
        parts: list[str] = []
        for block in result:
            if isinstance(block, dict):
                parts.append(block.get("text") or block.get("content") or "")
            else:
                parts.append(str(block))
        text = "\n".join(part for part in parts if part)
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
        if isinstance(data, dict) and "success" in data and isinstance(data.get("content"), str):
            return format_tool_result(ToolResult(**data))
        return text

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

def format_plan_status(status: PlanStep) -> str:
    """把 plan 状态转换为易读的字符串。"""
    status_map = {
        "pending": "待处理",
        "in_progress": "进行中",
        "done": "已完成",
    }
    if not isinstance(status, dict):
        raise ValueError("status 必须是 PlanStatus 类型")

    status_value = status.get("status")
    if not isinstance(status_value, str):
        raise ValueError("status['status'] 必须是字符串")

    task = status.get("task")
    task_str = task if isinstance(task, str) else "未知状态"
    status_id = status.get("id")
    status_id_str = status_id if isinstance(status_id, str) else "未知状态"

    state = status_map.get(status_value, "错误")
    return f"{status_id_str}. {task_str} 当前状态为 {state}"


