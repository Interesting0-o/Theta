"""会话子系统：`/list session`、`/new session`、`/session <id>` 三条命令 + 会话数据访问。

位置：`app/platform/commands/session.py`（2026-09-10 命令层成包时，把原 `app/platform/sessions.py`
的数据访问与这三条命令的 handler 聚到一处——它们本就是一件事：会话的**列出 / 新建 / 切换**）。

读 checkpoint 不需要图：直接开库 `AsyncSqliteSaver(conn).aget_tuple(config)` 拿一个 tuple 即可——
`aget_tuple` 自己会跑幂等的 `setup()`；**该 thread 没写过时返回 None（不抛错）**；拿到后
`checkpoint["channel_values"]["messages"]` 是**真的 BaseMessage 列表**（直接 `.content`），
`checkpoint["ts"]` 是 ISO 时间。

防御：`channel_values` 的内部形状属于 langgraph 的实现细节，读取全程包在 try 里——坏一个库
只让那一条退化成"（未读）"，绝不把 `/list session` 这种只读操作搞崩。

本模块不 import app.agent → 测试无需 .env。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.resource import iter_session_dbs
from app.schema.session_schema import SessionInfo
from app.schema.ui_schema import Notice

if TYPE_CHECKING:  # 只为类型：不真 import，避免与 loop 成环
    from app.platform.loop import AgentPlatform

# 摘要（末条 AI 正文首行）截断长度
_SUMMARY_LIMIT = 60

# 默认只给最近这么多个会话读 checkpoint；更早的只列 id + 时间（省得开一堆库）
DEFAULT_SUMMARIZE_LIMIT = 10


@dataclass(frozen=True)
class _ReadResult:
    """一次 checkpoint 读取的结果（内部中间态，不进 schema）。"""

    updated_at: str
    message_count: int
    summary: str


def _short_line(text: str, limit: int = _SUMMARY_LIMIT) -> str:
    """取首个非空行并截断（摘要用）。"""
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[:limit] + "…"


def _activity_mtime(db_path: Path) -> float:
    """该会话的"最后活动"文件时间。

    取 agent.db 与其 `-wal`/`-shm` 的最大 mtime——WAL 模式下写入先进 wal 文件，只看主库
    的 mtime 会把刚聊过的会话排到后面。
    """
    stamps = [db_path.stat().st_mtime]
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            stamps.append(side.stat().st_mtime)
    return max(stamps)


async def _read_session(db_path: Path, session_id: str) -> _ReadResult | None:
    """读一个会话的 (最后活动时间, 消息条数, 摘要)；读不动 → None（调用方退化处理）。"""
    conn = await aiosqlite.connect(str(db_path))
    try:
        saver = AsyncSqliteSaver(conn)
        tup = await saver.aget_tuple({"configurable": {"thread_id": session_id}})
        if tup is None:
            # 目录在、但该 thread 还没写过 checkpoint（建了会话还没说话）
            return _ReadResult(updated_at="", message_count=0, summary="")
        checkpoint = tup.checkpoint or {}
        values = checkpoint.get("channel_values") or {}
        messages = list(values.get("messages") or [])
        summary = ""
        # 末条**有正文的 AI 消息**：会话停在工具链中间时，最后一条可能是 ToolMessage，
        # 往前找才是"上次聊到哪"。
        for message in reversed(messages):
            content = str(getattr(message, "content", "") or "").strip()
            if getattr(message, "type", "") == "ai" and content:
                summary = _short_line(content)
                break
        return _ReadResult(
            updated_at=str(checkpoint.get("ts") or ""),
            message_count=len(messages),
            summary=summary,
        )
    except Exception:  # noqa: BLE001 —— checkpoint 内部形状是 langgraph 的实现细节
        return None
    finally:
        await conn.close()


async def list_sessions(
    workspace: str, *, summarize_limit: int = DEFAULT_SUMMARIZE_LIMIT
) -> list[SessionInfo]:
    """列出工作区的全部会话，按最后活动倒序。

    只对最近 `summarize_limit` 个会话开库读 checkpoint（摘要 + 条数），更早的只给
    id 与文件时间（`message_count=None` 即"未读"）。
    """
    db_paths = iter_session_dbs(workspace)
    # 先用文件时间倒序排（不读库就能排），再按序读最近 N 个
    ordered = sorted(db_paths, key=_activity_mtime, reverse=True)

    infos: list[SessionInfo] = []
    for index, db_path in enumerate(ordered):
        session_id = db_path.parent.name
        fallback = datetime.fromtimestamp(_activity_mtime(db_path)).isoformat(timespec="seconds")
        read = await _read_session(db_path, session_id) if index < summarize_limit else None
        if read is None:
            infos.append(SessionInfo(session_id=session_id, updated_at=fallback))
            continue
        infos.append(
            SessionInfo(
                session_id=session_id,
                updated_at=read.updated_at or fallback,
                message_count=read.message_count,
                summary=read.summary,
            )
        )
    return infos


def resolve_session_id(workspace: str, text: str) -> tuple[str | None, list[str]]:
    """把用户输入（完整 id 或前缀）解析成本工作区里**已存在**的会话 id。

    返回 `(命中 id | None, 候选 id 列表)`：
    - 命中：完整 id 直接命中，否则**唯一前缀**；
    - 未命中：`(None, [])`（没有任何会话匹配）；
    - 歧义：`(None, [全部匹配])`——调用方列出候选让用户补全，**不动当前会话**。
    """
    wanted = (text or "").strip()
    if not wanted:
        return None, []

    session_ids = [db.parent.name for db in iter_session_dbs(workspace)]
    if wanted in session_ids:  # 完整 id 优先（避免同时是别家前缀时被抢）
        return wanted, []

    matches = [sid for sid in session_ids if sid.startswith(wanted)]
    if len(matches) == 1:
        return matches[0], []
    return None, matches


# ------------------------- 三条命令的 handler -------------------------


def _short_id(session_id: str) -> str:
    """会话 id 的短形式（列表里印 8 位，也正是 `/session` 要敲的前缀）。"""
    return session_id[:8]


def _format_time(iso: str) -> str:
    """把 ISO 时间渲染成"今天 HH:MM / 昨天 HH:MM / YYYY-MM-DD HH:MM"（一律按本机时区）。

    两个来源的时区不同，必须在这里拉齐：checkpoint 的 `ts` 是 **UTC 的 aware 串**
    （langgraph 写 `datetime.now(timezone.utc)`），而文件时间回退（`list_sessions` 的
    `datetime.fromtimestamp`）是**本地 naive**。旧实现直接拿 UTC 的 date()/%H:%M 当本地用，
    整列差一个时区（CST 下真实 11:55 显示成 03:55），UTC 日期比本地早一天时还会误判"昨天"。
    """
    try:
        moment = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "时间未知"
    if moment.tzinfo is not None:
        moment = moment.astimezone()  # aware（UTC）→ 本机时区；naive 回退值原样用
    today = datetime.now().date()
    if moment.date() == today:
        return f"今天 {moment:%H:%M}"
    if (today - moment.date()).days == 1:
        return f"昨天 {moment:%H:%M}"
    return f"{moment:%Y-%m-%d %H:%M}"


def _render_session_line(info: SessionInfo, *, current_id: str) -> str:
    """列表里的一行：* 标记当前会话 · 短 id · 时间 · 条数 · 摘要。"""
    mark = "*" if info.session_id == current_id else " "
    if info.message_count is None:
        status = "（未读）"  # 旧会话按上限省读，或该库读不动
    elif info.message_count == 0:
        status = "（空会话）"
    else:
        status = f"{info.message_count} 条消息"
    summary = f"  {info.summary}" if info.summary else ""
    return f"  {mark} {_short_id(info.session_id)}  {_format_time(info.updated_at):<14} {status}{summary}"


async def _list_sessions(platform: "AgentPlatform", args: str) -> None:
    """`/list session`：列出当前工作区沙箱的全部会话（当前的那个打 *）。"""
    infos = await list_sessions(platform.workspace)
    if not infos:
        platform.ui.emit(
            Notice(text="当前工作区还没有会话（本次启动的会话在第一次输入后才建库）。")
        )
        return
    lines = [f"当前工作区共 {len(infos)} 个会话（* = 当前；用 /session <短 id> 切换）："]
    lines += [_render_session_line(info, current_id=platform.session_id) for info in infos]
    platform.ui.emit(Notice(text="\n".join(lines)))


async def _new_session(platform: "AgentPlatform", args: str) -> None:
    """`/new session`：新建一个会话并切过去（库在下次输入时才建）。"""
    previous = platform.session_id
    new_id = uuid.uuid4().hex  # 与 app/tui/runner.py 的开机新会话同规格
    await platform.switch_session(new_id)
    platform.ui.emit(
        Notice(
            text=(
                f"已新建并切到会话 {_short_id(new_id)}（下一条消息起在新会话里）。\n"
                f"原会话 {_short_id(previous)} 可用 /session {_short_id(previous)} 切回。"
            )
        )
    )


async def _switch_session(platform: "AgentPlatform", args: str) -> None:
    """`/session <id>`：切到指定会话（接受完整 id 或唯一前缀）。"""
    if not args:
        platform.ui.emit(Notice(text="用法：/session <session_id>（用 /list session 查看现有会话）"))
        return

    target, candidates = resolve_session_id(platform.workspace, args)
    if target is None:
        if candidates:
            listing = "、".join(_short_id(cid) for cid in candidates)
            text = f"前缀 {args} 匹配到多个会话：{listing}；请多给几位。当前会话未变。"
        else:
            text = f"没有找到会话 {args}（用 /list session 查看现有会话）。当前会话未变。"
        platform.ui.emit(Notice(text=text))
        return

    previous = platform.session_id
    await platform.switch_session(target)
    platform.ui.emit(
        Notice(
            text=(
                f"已切到会话 {_short_id(target)}（下一条消息起接着它的消息栈走）。\n"
                f"原会话 {_short_id(previous)} 可用 /session {_short_id(previous)} 切回。"
            )
        )
    )


# 本子系统供命令表登记的动作（名字 → {desc, handler}）——表本身在包 __init__ 里汇总
SESSION_ACTIONS: dict[str, dict] = {
    "/list session": {
        "desc": "列出当前工作区的所有会话（含最后活动时间与末条回复摘要）",
        "handler": _list_sessions,
    },
    "/new session": {"desc": "新建一个会话并切过去", "handler": _new_session},
    "/session": {"desc": "切换到指定会话：/session <短 id 或完整 id>", "handler": _switch_session},
}
