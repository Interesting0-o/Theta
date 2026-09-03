from typing import List, Mapping
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import (
    Connection,
    StdioConnection,
    SSEConnection,
    StreamableHttpConnection,
)
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).parent.parent.parent

async def load_mcp_tool(workspace_path: str):

    file_io_connection = StdioConnection(
        transport="stdio",
        command=sys.executable,
        args=["-m", "mcp_service.file_io"],
        cwd=PROJECT_ROOT,
        env={"WORKSPACE_PATH": workspace_path},
    )
    terminal_connection = StdioConnection(
        transport="stdio",
        command=sys.executable,
        args=["-m", "mcp_service.terminal"],
        cwd=PROJECT_ROOT,
        env={"WORKSPACE_PATH": workspace_path},
    )

    servers: dict[str, Connection] = {
        "file_io": file_io_connection,
        "terminal": terminal_connection,
    }

    client = MultiServerMCPClient(servers)
    tools = await client.get_tools()
    return tools
