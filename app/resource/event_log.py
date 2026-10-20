"""确定性 event log：每个 tool_call 一行，append-only JSONL。

**为什么独立于 notes / memory**：notes 是会话内可读的折叠归档（联网正文），memory 是跨会话
稳定事实；event log 是**审计层**——记录"哪个 tool_call 在什么状态、被怎么审批、目标是什么"，
可无上限落盘，上下文只放指针。它不调 LLM，不做语义压缩（语义层是 checkpoint 的 LLM digest）。

落点：`resource/<ws_key>/sessions/<sid>/event_log.jsonl`（与 agent.db 同会话域，私有、不污染
用户工作区）。模型侧通过 `read_event_log` 工具按需读取，不直接 `read_file`（在沙箱外）。

**不存正文**：只存 `content_chars`、路径/URL/命令哈希。完整正文在 messages / notes / 工具
自己的落盘点里；event log 的职责是"可审计的索引"，不是第二份正文。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Iterable

from app.resource.paths import session_event_log_path

logger = logging.getLogger(__name__)

_MAX_TARGET_CHARS = 200
_PATH_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "create_file",
        "delete_file",
        "list_dir",
        "get_directory_tree",
    }
)


def args_hash(args: dict | None) -> str:
    """参数规范化哈希：排序键、紧凑 JSON、UTF-8，取前 16 位十六进制。"""
    packed = json.dumps(
        args or {}, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(packed.encode("utf-8")).hexdigest()[:16]


def _short(value, limit: int = _MAX_TARGET_CHARS) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _command_hash(command) -> str:
    text = str(command or "").strip()
    return "command:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def target_of(tool: str, args: dict | None) -> str:
    """从调用参数提取可读目标：路径 / URL / 命令哈希 / 编排对象。

    终端命令只存哈希，避免把可能含 token 的命令原文写进日志；路径与 URL 保留可读形式，
    便于 digest 与人工审计。
    """
    args = args or {}
    if tool in _PATH_TOOLS:
        return _short(args.get("path") or args.get("dir_path"))
    if tool == "copy_path":
        source, destination = args.get("source"), args.get("destination")
        if source or destination:
            return _short(f"{source} -> {destination}")
        return ""
    if tool == "glob":
        return _short(f"{args.get('pattern') or ''} @ {args.get('path') or '.'}")
    if tool == "search_content":
        return _short(f"{args.get('query') or ''} @ {args.get('path') or '.'}")
    if tool in {"run_command", "start_process"}:
        return _command_hash(args.get("command"))
    if tool.startswith("process_"):
        return _short(args.get("process_id"))
    if tool in {"web_search", "deep_research"}:
        return _short(args.get("query"))
    if tool == "extract_urls":
        urls = args.get("urls") or []
        if isinstance(urls, list) and urls:
            return _short(urls[0]) + (f" (+{len(urls) - 1})" if len(urls) > 1 else "")
        return _short(urls)
    if tool == "crawl_website":
        return _short(args.get("url"))
    if tool.startswith("github_"):
        repo = args.get("repo")
        owner = args.get("owner")
        if owner and repo:
            return _short(f"{owner}/{repo}")
        return _short(args.get("query"))
    if tool == "dispatch_subtasks":
        sub_tasks = args.get("sub_tasks") or []
        return f"subtasks:{len(sub_tasks)}" if isinstance(sub_tasks, list) else "subtasks"
    if tool in {"create_plan", "clear_plan"}:
        return "plan"
    if tool == "update_plan_step":
        return _short(f"step:{args.get('step_id')}")
    if tool == "read_note":
        return _short(args.get("note_id"))
    if tool in {"write_memory", "read_memory"}:
        return _short(args.get("key") or args.get("type"))
    if tool in {"get_skill", "drop_skill"}:
        return _short(args.get("name"))
    if tool == "ask_user":
        return _short(args.get("question"))
    if tool == "read_event_log":
        return _short(args.get("tool") or "recent")
    return ""


def append_event(
    workspace_path: str,
    session_id: str | None,
    *,
    tool: str,
    tool_call_id: str,
    args: dict | None,
    status: str,
    error_type: str | None = None,
    approval: str = "not_required",
    content_chars: int = 0,
    summary_version: int | None = None,
) -> dict | None:
    """追加一行事件；`session_id` 缺失 / 写盘失败 → 返回 None，不影响工具执行。"""
    if not workspace_path or not session_id:
        return None

    record = {
        "event_id": "evt_" + uuid.uuid4().hex[:12],
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "session_id": session_id,
        "tool_call_id": tool_call_id or "",
        "tool": tool or "",
        "target": target_of(tool, args),
        "args_hash": args_hash(args),
        "status": status,
        "error_type": error_type,
        "approval": approval,
        "content_chars": int(content_chars or 0),
        "summary_version": summary_version,
    }
    path = session_event_log_path(workspace_path, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        logger.warning("event log 写入失败: %s", path, exc_info=True)
        return None
    return record


def iter_events(workspace_path: str, session_id: str | None) -> Iterable[dict]:
    """按写入顺序迭代事件；坏行跳过（日志是审计材料，不因一行损坏阻断读取）。"""
    if not workspace_path or not session_id:
        return
    path = session_event_log_path(workspace_path, session_id)
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def read_events(
    workspace_path: str,
    session_id: str | None,
    *,
    limit: int | None = 50,
    tool: str | None = None,
) -> list[dict]:
    """读取最近 `limit` 条事件（`limit=None` = 全部）；可按工具名过滤。"""
    records: Iterable[dict] = iter_events(workspace_path, session_id)
    if tool:
        records = (record for record in records if record.get("tool") == tool)
    if limit is None:
        return list(records)
    if limit <= 0:
        return []
    return list(deque(records, maxlen=limit))
