"""app/platform/commands/session.py：会话枚举、checkpoint 摘要读取、切换目标解析。

用**真的 sqlite checkpoint 库**（AsyncSqliteSaver + 最小 StateGraph + 局部 TypedDict state）——
不 import app.agent，故本文件**无需 .env**、也无需 WORKSPACE_PATH。
落盘一律 monkeypatch app.resource.RESOURCE_ROOT 到 tmp。
"""
import asyncio
import time
from typing import Annotated, TypedDict

import aiosqlite
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

import app.resource as resource
from app.platform.commands.session import list_sessions, resolve_session_id


class _State(TypedDict):
    """最小 state：只要有 messages 就够造出真 checkpoint（不牵扯 AgentState/.env）。"""

    messages: Annotated[list, add_messages]


def _make_session(workspace: str, session_id: str, replies: list[str]) -> None:
    """在工作区里造一个真会话：跑 `len(replies)` 轮，每轮落一条 AI 回复。"""

    async def go():
        db_path = resource.session_db_path(workspace, session_id)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(db_path))
        try:
            saver = AsyncSqliteSaver(conn)
            await saver.setup()
            pending = list(replies)

            def reply_node(state):
                return {"messages": [AIMessage(content=pending.pop(0))]}

            graph = StateGraph(_State)
            graph.add_node("reply", reply_node)
            graph.add_edge(START, "reply")
            graph.add_edge("reply", END)
            app = graph.compile(checkpointer=saver)
            config = {"configurable": {"thread_id": session_id}}
            for _ in replies:
                await app.ainvoke({"messages": [HumanMessage(content="问题")]}, config)
        finally:
            await conn.close()

    asyncio.run(go())


def _workspace(tmp_path, monkeypatch) -> str:
    monkeypatch.setattr(resource, "RESOURCE_ROOT", tmp_path / "res")
    return str(tmp_path / "ws")


def test_list_sessions_reads_summary_and_count(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)
    _make_session(ws, "aaaa1111", replies=["第一轮结论"])
    time.sleep(0.02)  # 让两次活动的文件时间可区分（顺序断言用）
    _make_session(ws, "bbbb2222", replies=["旧结论", "新结论"])

    infos = asyncio.run(list_sessions(ws))
    by_id = {info.session_id: info for info in infos}

    assert set(by_id) == {"aaaa1111", "bbbb2222"}
    assert by_id["aaaa1111"].message_count == 2  # Human + AI
    assert by_id["aaaa1111"].summary == "第一轮结论"
    assert by_id["bbbb2222"].summary == "新结论"  # 取**末条** AI 正文
    assert by_id["bbbb2222"].message_count == 4
    assert all(info.updated_at for info in infos)  # 时间不会是空串

    assert [info.session_id for info in infos] == ["bbbb2222", "aaaa1111"]  # 最近活动在前


def test_list_sessions_handles_never_used_session(tmp_path, monkeypatch):
    """建了库但一句话没说：条数 0、摘要空（不是"读不到"）。"""
    ws = _workspace(tmp_path, monkeypatch)
    _make_session(ws, "cccc3333", replies=[])

    infos = asyncio.run(list_sessions(ws))

    assert len(infos) == 1
    assert infos[0].message_count == 0 and infos[0].summary == ""


def test_list_sessions_empty_workspace(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)

    assert asyncio.run(list_sessions(ws)) == []


def test_list_sessions_respects_summarize_limit(tmp_path, monkeypatch):
    """超过上限的旧会话不读 checkpoint（条数 None = 未读），省得开一堆库。"""
    ws = _workspace(tmp_path, monkeypatch)
    _make_session(ws, "old11111", replies=["旧"])
    time.sleep(0.02)
    _make_session(ws, "new22222", replies=["新"])

    infos = asyncio.run(list_sessions(ws, summarize_limit=1))

    assert [info.session_id for info in infos] == ["new22222", "old11111"]
    assert infos[0].message_count == 2  # 最近的那个读了
    assert infos[1].message_count is None and infos[1].summary == ""  # 更早的没读


def test_resolve_session_id_full_prefix_and_ambiguous(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)
    _make_session(ws, "aaaa111122223333", replies=["x"])
    _make_session(ws, "aaaa999988887777", replies=["y"])
    _make_session(ws, "bbbb000011112222", replies=["z"])

    # 完整 id 命中
    assert resolve_session_id(ws, "aaaa111122223333") == ("aaaa111122223333", [])
    # 唯一前缀命中
    assert resolve_session_id(ws, "bbbb") == ("bbbb000011112222", [])
    assert resolve_session_id(ws, "aaaa1111") == ("aaaa111122223333", [])
    # 前缀撞多个 → 不命中、给候选（调用方据此提示"请多给几位"）
    target, candidates = resolve_session_id(ws, "aaaa")
    assert target is None
    assert sorted(candidates) == ["aaaa111122223333", "aaaa999988887777"]
    # 完全没匹配
    assert resolve_session_id(ws, "zzzz") == (None, [])
    # 空输入不误命中
    assert resolve_session_id(ws, "   ") == (None, [])
