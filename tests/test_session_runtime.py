"""app/platform/runtime.py::build_session_runtime 的资源生命周期。

只守一件事：**建图失败时那个 aiosqlite 连接必须被关掉**。它此刻还没返回给调用方，而调用方
（AgentPlatform）的 finally 只关自己拿到的那个——不在这里收就没人收（2026-09-15 基座审计
发现的异常路径泄漏）。

需要 .env：本用例会 import app.agent.graph 以 monkeypatch 建图入口。
"""
import asyncio

import pytest

from app.platform import runtime as runtime_module


def test_build_session_runtime_closes_connection_when_graph_build_fails(tmp_path, monkeypatch):
    """建图抛错 → 连接被关闭，且异常照常向上冒泡（不是被吞掉）。"""
    import aiosqlite

    from app.agent import graph as graph_module

    closed: list[bool] = []
    real_connect = aiosqlite.connect

    async def spy_connect(path):
        connection = await real_connect(path)
        real_close = connection.close

        async def close_and_record():
            closed.append(True)
            await real_close()

        connection.close = close_and_record  # type: ignore[method-assign]
        return connection

    async def boom(*args, **kwargs):
        raise RuntimeError("建图失败（模拟缺 .env / MCP 起不来）")

    monkeypatch.setattr(aiosqlite, "connect", spy_connect)
    monkeypatch.setattr(graph_module, "get_main_agent_graph", boom)

    async def go():
        with pytest.raises(RuntimeError, match="建图失败"):
            await runtime_module.build_session_runtime(str(tmp_path), "sess-1")
        assert closed == [True]

    asyncio.run(go())
