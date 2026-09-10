"""会话域纯数据结构：一个会话（= 一份 checkpoint 库）在列表里的样子。

放 app/schema 的理由同 approval_schema.py / ui_schema.py：**只有属性、可序列化、跨模块**
（`app/platform/commands/session.py` 产出 → 同包 `__init__.py` 渲染成给用户的文本）。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionInfo:
    """一个会话的摘要信息（对应 `resource/<ws_key>/sessions/<session_id>/agent.db`）。

    - `session_id`：目录名，同时也是 LangGraph 的 thread_id；
    - `updated_at`：最后活动时间，ISO 字符串（优先取 checkpoint 的 ts，读不到就退化成本地
      文件 mtime）——**不会是空串**，方便列表统一渲染；
    - `message_count`：消息条数；**None = 没读 checkpoint**（旧会话按上限省读，或读取失败），
      与"读了但确实是 0 条"（空会话）区分开；
    - `summary`：末条 AI 正文首行（截断）；没读到/没有则空串。
    """

    session_id: str
    updated_at: str
    message_count: int | None = None
    summary: str = ""
