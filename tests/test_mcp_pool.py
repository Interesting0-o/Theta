"""MCP 运行体常驻化：owner task、按 (工作区, 会话) 隔离、关闭与自愈。

**不拉真实 MCP server**：全部经 `_ServerWorker` 的注入口（`session_factory` / `tools_loader`）
在进程内跑。假会话的 `__aexit__` 会断言"关闭发生在创建它的那个 Task 里"——那正是
`app/agent/mcp.py` 文件头记的承重约束（anyio cancel scope 与 Task 绑定），谁把关闭挪出 owner,
这里立刻变红。

最后三条用例走真链路（真 adapter / 真 tool.json），锁住"改造后模型可见的东西不变"这一契约。

注意：import app.agent.mcp 会触发 app.agent/__init__ → graph → model，model 顶层调用
get_settings()，因此运行本文件要求 .env 存在（CLAUDE.md 前提）。
"""
import asyncio
import functools
import json
from types import SimpleNamespace

import anyio
import pytest
from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool
from pydantic import SecretStr

import app.agent.mcp as mcp_module
from app.agent.mcp import (
    _ServerWorker,
    _make_shim,
    _normalize_workspace,
    _reset_pools_for_tests,
    close_session_pool,
    get_worker,
    load_mcp_tool,
)
from app.agent.tools import worker_tools
from app.agent.utils import format_tool_result
from app.schema.agent_schema import MCPToolSpec

# 假连接：注入假会话工厂后它从不被使用，只为构造 _ServerWorker 占位
_CONN = {"transport": "stdio", "command": "python", "args": []}
_OK_BLOCKS = [{"type": "text", "text": "ok"}]


# ---------------------- 假件 ----------------------


class FakeTool:
    """假工具：记调用次数；`error` 注入后**只抛一次**（用于验证"重建后重试"）。"""

    def __init__(self, name: str, error: Exception | None = None) -> None:
        self.name = name
        self.calls = 0
        self._error = error

    async def ainvoke(self, args: dict):
        self.calls += 1
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        return _OK_BLOCKS


class FakeSession:
    """假会话；`__aexit__` 就是那条承重约束的断言点。"""

    def __init__(self, tools: dict) -> None:
        self.tools = tools
        self.created_in = asyncio.current_task()
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        assert asyncio.current_task() is self.created_in, "cancel scope 不允许跨 Task 退出"
        self.closed = True
        return False


class FakeRuntime:
    """一组假 server 运行时：会话工厂 + 工具装载，注入给 `_ServerWorker`。"""

    def __init__(self, tool_names=("read_file",), error: Exception | None = None) -> None:
        self.sessions: list[FakeSession] = []
        self.tools = {name: FakeTool(name, error) for name in tool_names}

    def session_factory(self, server: str) -> FakeSession:
        session = FakeSession(self.tools)
        self.sessions.append(session)
        return session

    async def tools_loader(self, session: FakeSession) -> list:
        return list(session.tools.values())


def _patch_worker(monkeypatch, runtime: FakeRuntime) -> None:
    """让 `get_worker` 造出的运行体用假会话/假工具（仍走真注册表与真关闭路径）。"""
    monkeypatch.setattr(
        mcp_module,
        "_ServerWorker",
        functools.partial(
            _ServerWorker,
            session_factory=runtime.session_factory,
            tools_loader=runtime.tools_loader,
        ),
    )


def _fake_settings(key: str = "tavily-key"):
    return SimpleNamespace(TAVILY_API_KEY=SecretStr(key))


def _spec(server: str, name: str) -> MCPToolSpec:
    return MCPToolSpec(server=server, name=name, description="d", args_schema={}, metadata=None)


# ---------------------- 运行体：复用 / 关闭 / 隔离 / 自愈 ----------------------


def test_reuses_one_session_across_calls(tmp_path, monkeypatch):
    """同一运行体连续两次调用只建一条会话——这就是本次改造要的"不再每次重起子进程"。"""

    async def main():
        _reset_pools_for_tests()
        runtime = FakeRuntime()
        _patch_worker(monkeypatch, runtime)
        worker = get_worker(str(tmp_path), "s1", "file_io", _CONN)

        assert await worker.call("read_file", {"path": "a"}) == _OK_BLOCKS
        assert await worker.call("read_file", {"path": "b"}) == _OK_BLOCKS

        assert len(runtime.sessions) == 1
        assert runtime.tools["read_file"].calls == 2
        assert not runtime.sessions[0].closed
        await close_session_pool(str(tmp_path), "s1")

    asyncio.run(main())


def test_close_happens_in_owner_task(tmp_path, monkeypatch):
    """关闭必须发生在 owner 的 Task 里（假会话的 __aexit__ 会断言）——承重约束。"""

    async def main():
        _reset_pools_for_tests()
        runtime = FakeRuntime()
        _patch_worker(monkeypatch, runtime)
        worker = get_worker(str(tmp_path), "s1", "file_io", _CONN)
        await worker.call("read_file", {})

        # 从**测试自己的** Task 发起关闭：真正的 aclose 由 owner 执行，故断言成立。
        await close_session_pool(str(tmp_path), "s1")

        assert runtime.sessions[0].closed
        assert mcp_module._WORKERS == {}

    asyncio.run(main())


def test_pools_isolated_by_session(tmp_path, monkeypatch):
    """同工作区的两个会话各持一组运行体，互不可见——terminal 的进程表才不会跨会话泄漏。"""

    async def main():
        _reset_pools_for_tests()
        ws = str(tmp_path)
        runtime_a, runtime_b = FakeRuntime(), FakeRuntime()
        # 两个运行体用各自的假件：分别打补丁后各装一个（注册表按 (工作区, 会话, server) 分隔）
        _patch_worker(monkeypatch, runtime_a)
        worker_a = get_worker(ws, "session-a", "terminal", _CONN)
        _patch_worker(monkeypatch, runtime_b)
        worker_b = get_worker(ws, "session-b", "terminal", _CONN)

        await worker_a.call("read_file", {})
        await worker_b.call("read_file", {})
        assert len(runtime_a.sessions) == 1 and len(runtime_b.sessions) == 1

        await close_session_pool(ws, "session-a")
        assert runtime_a.sessions[0].closed
        assert not runtime_b.sessions[0].closed  # 关 A 不碰 B

        await worker_b.call("read_file", {})  # B 仍复用原会话
        assert len(runtime_b.sessions) == 1
        await close_session_pool(ws, "session-b")

    asyncio.run(main())


def test_call_after_close_rebuilds(tmp_path, monkeypatch):
    """关闭只是"丢掉运行体"，不是"报废会话"——再调用透明重建（shim 不绑会话，故无需重载工具）。"""

    async def main():
        _reset_pools_for_tests()
        runtime = FakeRuntime()
        _patch_worker(monkeypatch, runtime)
        ws = str(tmp_path)

        await get_worker(ws, "s1", "file_io", _CONN).call("read_file", {})
        assert len(runtime.sessions) == 1

        await close_session_pool(ws, "s1")
        assert runtime.sessions[0].closed

        await get_worker(ws, "s1", "file_io", _CONN).call("read_file", {})
        assert len(runtime.sessions) == 2  # 重建了一条新会话
        await close_session_pool(ws, "s1")

    asyncio.run(main())


def test_transport_failure_rebuilds_and_retries_once(tmp_path, monkeypatch):
    """传输类异常 → 关掉重建 → 重试一次。常驻后若不做这条，坏会话会让该会话永久残废。"""

    async def main():
        _reset_pools_for_tests()
        runtime = FakeRuntime(error=anyio.ClosedResourceError())
        _patch_worker(monkeypatch, runtime)
        ws = str(tmp_path)

        result = await get_worker(ws, "s1", "file_io", _CONN).call("read_file", {})

        assert result == _OK_BLOCKS  # 重试成功
        assert len(runtime.sessions) == 2
        assert runtime.sessions[0].closed
        await close_session_pool(ws, "s1")

    asyncio.run(main())


def test_worker_survives_error_when_caller_is_cancelled(tmp_path, monkeypatch):
    """调用方被取消（turn 取消）不该打死 owner：后续调用照常。"""

    async def main():
        _reset_pools_for_tests()
        runtime = FakeRuntime()
        _patch_worker(monkeypatch, runtime)
        ws = str(tmp_path)
        worker = get_worker(ws, "s1", "file_io", _CONN)

        task = asyncio.create_task(worker.call("read_file", {}))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await worker.call("read_file", {}) == _OK_BLOCKS
        await close_session_pool(ws, "s1")

    asyncio.run(main())


# ---------------------- shim：与真 adapter 同形（契约用例） ----------------------


class _StubWorker:
    """把调用直接转给某个真 adapter 工具——用来隔离测 shim 的返回值形状。"""

    def __init__(self, inner) -> None:
        self._inner = inner

    async def call(self, tool_name: str, args: dict):
        return await self._inner.ainvoke(args)


class _FakeClientSession:
    """给真 adapter 用的最小会话（只被 call_tool 调用）。"""

    def __init__(self, result: CallToolResult) -> None:
        self._result = result

    async def call_tool(self, name, args, progress_callback=None):
        return self._result


def _adapter_tool(result: CallToolResult):
    tool = MCPTool(name="read_file", description="d", inputSchema={"type": "object", "properties": {}})
    return convert_mcp_tool_to_langchain_tool(_FakeClientSession(result), tool)


def _strip_ids(value):
    """adapter 给每个 content block 附一个随机 `id`，两边各自随机——比对时按结构比对，忽略它。

    真正有意义的是 `format_tool_result` 的产出（它本来就丢弃 id，见 app/agent/utils.py）。
    """
    if isinstance(value, list):
        return [{k: v for k, v in block.items() if k != "id"} for block in value]
    return value


_PAYLOAD = json.dumps({"success": True, "content": "文件内容 abc", "error_type": None})

_SHAPE_CASES = {
    "普通 content-block": CallToolResult(content=[TextContent(type="text", text=_PAYLOAD)]),
    "structuredContent 非 None": CallToolResult(
        content=[TextContent(type="text", text=_PAYLOAD)], structuredContent={"k": 1}
    ),
    "isError=True": CallToolResult(
        content=[TextContent(type="text", text="出错了")], isError=True
    ),
    "空 content": CallToolResult(content=[]),
}


@pytest.mark.parametrize("case", list(_SHAPE_CASES))
def test_shim_returns_same_shape_as_adapter_tool(case, monkeypatch):
    """shim 的返回值与真 adapter 工具逐字节同形——模型可见文本因此不变。

    这是本次改造最要紧的契约：`app/agent/utils.py::format_tool_result` 只认 content-block 列表，
    所以 shim **不能**给内层传 tool_call_id/config（那会让 _format_output 提前返回 ToolMessage）。
    """

    async def main():
        monkeypatch.setattr(
            mcp_module, "get_worker", lambda *a, **k: _StubWorker(_adapter_tool(_SHAPE_CASES[case]))
        )
        shim = _make_shim(workspace="ws", session="s", spec=_spec("file_io", "read_file"), connection=_CONN)
        adapter = _adapter_tool(_SHAPE_CASES[case])

        assert _strip_ids(await shim.ainvoke({})) == _strip_ids(await adapter.ainvoke({}))
        # 真正承重的那条：模型可见文本逐字节相同
        assert format_tool_result(await shim.ainvoke({})) == format_tool_result(
            await adapter.ainvoke({})
        )

    asyncio.run(main())


def test_shim_keeps_names_order_and_worker_filter(tmp_path, monkeypatch):
    """保名、保序（server 顺序 × server 内顺序）、且 worker 的 tool.json 过滤照旧生效。"""

    async def main():
        _reset_pools_for_tests()
        monkeypatch.setattr(mcp_module, "get_settings", lambda: _fake_settings())
        specs = [
            _spec("file_io", "read_file"),
            _spec("file_io", "run_command"),
            _spec("web_search", "web_search"),  # 工具名与 server 名同名：归属必须来自连接表
        ]

        async def fake_specs(workspace, connections):
            return specs

        monkeypatch.setattr(mcp_module, "_tool_specs", fake_specs)

        tools = await load_mcp_tool(str(tmp_path), "s1")
        assert [tool.name for tool in tools] == ["read_file", "run_command", "web_search"]
        assert all(tool.response_format == "content_and_artifact" for tool in tools)

        # worker 子集的过滤按 name 查 tool.json：shim 保名即可，无需任何改动
        assert [tool.name for tool in worker_tools(tools)] == ["read_file", "web_search"]

        # 同一 (工作区, 会话) 第二次加载命中缓存（不重新拉 schema）
        assert await load_mcp_tool(str(tmp_path), "s1") is tools

    asyncio.run(main())


def test_shim_cache_is_per_session(tmp_path, monkeypatch):
    """shim 缓存按 (工作区, 会话) 分键：换会话必须拿到各自的工具对象。"""

    async def main():
        _reset_pools_for_tests()
        monkeypatch.setattr(mcp_module, "get_settings", lambda: _fake_settings())
        specs = [_spec("file_io", "read_file")]

        async def fake_specs(workspace, connections):
            return specs

        monkeypatch.setattr(mcp_module, "_tool_specs", fake_specs)

        first = await load_mcp_tool(str(tmp_path), "session-a")
        second = await load_mcp_tool(str(tmp_path), "session-b")

        assert first is not second
        assert [tool.name for tool in first] == [tool.name for tool in second]

    asyncio.run(main())


def test_close_session_pool_normalizes_workspace(tmp_path):
    """调用方给的可能是未归一化路径；close 必须归一化后匹配（否则切会话关不掉运行体）。"""

    async def main():
        _reset_pools_for_tests()
        worker = _ServerWorker(
            workspace=_normalize_workspace(str(tmp_path)),
            session="s1",
            server="file_io",
            connection={"transport": "stdio"},
        )
        mcp_module._WORKERS[(worker.workspace, "s1", "file_io")] = worker
        await close_session_pool(str(tmp_path / ".." / tmp_path.name), "s1")
        assert mcp_module._WORKERS == {}

    asyncio.run(main())
