"""MCP 工具加载：以 stdio 子进程拉起 file_io / terminal 两个 MCP server。

工作区 WORKSPACE_PATH 由 get_graph 解析后传入；这里在拉起子进程前先做前置校验
（未提供 / 目录不存在 → ConfigError），避免把坏配置传进子进程再等它 import 时失败。
子进程通过 stdio 连接的 env 拿到 WORKSPACE_PATH（mcp_service.file_io 在 import 时校验）。
"""
import asyncio
import sys
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import Connection, StdioConnection

from app.exception import ConfigError

PROJECT_ROOT = Path(__file__).parent.parent.parent


def _validate_workspace(workspace_path) -> str:
    """校验并归一化工作区路径；未设置或目录不存在抛 ConfigError。"""
    if not workspace_path or not isinstance(workspace_path, str) or not workspace_path.strip():
        raise ConfigError("WORKSPACE_PATH 未设置")
    path = Path(workspace_path).expanduser().resolve()
    if not path.is_dir():
        raise ConfigError(f"agent工作区路径{path} 不存在")
    return str(path)


# 进程级 MCP 工具缓存：按工作区键复用，避免 langgraph dev 每次访问 get_graph
# 都重新拉起 file_io/terminal 子进程并握手（那会让 schema/run 请求慢到秒级）。
_MCP_TOOLS_CACHE: dict[str, list] = {}
# 单飞锁：同一工作区的并发冷加载只真正拉起一次，其余等锁后直接复用缓存。
_MCP_LOADING_LOCKS: dict[str, asyncio.Lock] = {}


async def load_mcp_tool(workspace_path: str):
    # 文件系统 stat 属阻塞调用，langgraph dev 的 blockbuster 会在事件循环里拦截，
    # 因此放到线程里执行校验，再拉起子进程。
    workspace_path = await asyncio.to_thread(_validate_workspace, workspace_path)
    cached = _MCP_TOOLS_CACHE.get(workspace_path)
    if cached is not None:
        return cached

    lock = _MCP_LOADING_LOCKS.setdefault(workspace_path, asyncio.Lock())
    async with lock:
        # 双检：等待锁的并发调用者直接复用已加载的结果，不重复拉起子进程
        cached = _MCP_TOOLS_CACHE.get(workspace_path)
        if cached is not None:
            return cached

        env = {"WORKSPACE_PATH": workspace_path}

        file_io_connection = StdioConnection(
            transport="stdio",
            command=sys.executable,
            args=["-m", "mcp_service.file_io"],
            cwd=PROJECT_ROOT,
            env=env,
        )
        terminal_connection = StdioConnection(
            transport="stdio",
            command=sys.executable,
            args=["-m", "mcp_service.terminal"],
            cwd=PROJECT_ROOT,
            env=env,
        )

        servers: dict[str, Connection] = {
            "file_io": file_io_connection,
            "terminal": terminal_connection,
        }

        client = MultiServerMCPClient(servers)
        tools = await client.get_tools()
        _MCP_TOOLS_CACHE[workspace_path] = tools
        return tools
