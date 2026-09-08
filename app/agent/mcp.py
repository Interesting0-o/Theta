"""MCP 工具加载：以 stdio 子进程拉起 file_io / terminal / git / web_search 四个 MCP server。

工作区 WORKSPACE_PATH 由 get_main_agent_graph 解析后传入；这里在拉起子进程前先做前置校验
（未提供 / 目录不存在 → ConfigError），避免把坏配置传进子进程再等它 import 时失败。
子进程约定：
- env 注入 WORKSPACE_PATH（mcp_service.file_io / mcp_service.git 在 import 时校验）
  与 PYTHONPATH=项目根；
- 四个 server 的 cwd 都设为工作区根目录，使 terminal.run_command 不传 cwd 时默认
  在工作区里执行（而非 CodingAgent 仓库根），降低误删自身源码的风险；
- web_search 额外注入 TAVILY_API_KEY（mcp_service/web_search 在 import 时校验）。
  密钥为空（.env 里留空）时跳过该 server 并告警——联网检索是可选能力，
  不阻塞主流程；文件/终端/git 能力不受影响。
"""
import asyncio
import logging
import sys
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import Connection, StdioConnection

from app.config import get_settings
from app.exception import ConfigError

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent


def _validate_workspace(workspace_path) -> str:
    """校验并归一化工作区路径；未设置或目录不存在抛 ConfigError。"""
    if not workspace_path or not isinstance(workspace_path, str) or not workspace_path.strip():
        raise ConfigError("WORKSPACE_PATH 未设置")
    path = Path(workspace_path).expanduser().resolve()
    if not path.is_dir():
        raise ConfigError(f"agent工作区路径{path} 不存在")
    return str(path)


def _build_servers(workspace_path: str) -> dict[str, Connection]:
    """按当前配置构造要拉起的 MCP server 连接集合（不实际拉起子进程，便于单测）。

    - file_io / terminal / git：必选，cwd 设为工作区根；
    - web_search：TAVILY_API_KEY 为空则跳过并告警（可选能力不阻塞主流程）。
    """
    env = {
        "WORKSPACE_PATH": workspace_path,
        # 子进程 cwd 已切到工作区，靠 PYTHONPATH 补回项目根，保证
        # `python -m mcp_service.*` 仍能 import 到 app/mcp_service 包。
        "PYTHONPATH": str(PROJECT_ROOT),
    }

    # 各 server 都以工作区为启动目录（而非项目根）：
    # - file_io / git 本身按 WORKSPACE_PATH 解析路径，cwd 与解析无关，切到工作区保持一致；
    # - terminal 的 run_command 不显式传 cwd 时就在该进程 cwd 里执行，
    #   切到工作区可避免命令默认落在 CodingAgent 仓库根、误伤自身源码；
    # - web_search 不碰文件系统，切到工作区仅保持约定一致。
    file_io_connection = StdioConnection(
        transport="stdio",
        command=sys.executable,
        args=["-m", "mcp_service.file_io"],
        cwd=workspace_path,
        env=env,
    )
    terminal_connection = StdioConnection(
        transport="stdio",
        command=sys.executable,
        args=["-m", "mcp_service.terminal"],
        cwd=workspace_path,
        env=env,
    )
    git_connection = StdioConnection(
        transport="stdio",
        command=sys.executable,
        args=["-m", "mcp_service.git"],
        cwd=workspace_path,
        env=env,
    )

    servers: dict[str, Connection] = {
        "file_io": file_io_connection,
        "terminal": terminal_connection,
        "git": git_connection,
    }

    tavily_key = get_settings().TAVILY_API_KEY.get_secret_value()
    if not tavily_key:
        logger.warning("TAVILY_API_KEY 为空，跳过 web_search MCP server（联网检索不可用）")
        return servers

    web_search_connection = StdioConnection(
        transport="stdio",
        command=sys.executable,
        args=["-m", "mcp_service.web_search"],
        cwd=workspace_path,
        env={**env, "TAVILY_API_KEY": tavily_key},
    )
    servers["web_search"] = web_search_connection
    return servers


# 进程级 MCP 工具缓存：按工作区键复用，避免 langgraph dev 每次访问 get_main_agent_graph
# 都重新拉起 MCP 子进程并握手（那会让 schema/run 请求慢到秒级）。
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

        client = MultiServerMCPClient(_build_servers(workspace_path))
        tools = await client.get_tools()
        _MCP_TOOLS_CACHE[workspace_path] = tools
        return tools
