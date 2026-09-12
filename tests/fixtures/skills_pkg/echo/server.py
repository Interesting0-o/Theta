"""测试夹具技能的 MCP server（能力型的最小形态：一个工具、无 env、无网络）。

骨架与 `mcp_service/file_io.py` 一致：`@mcp.tool()` 在外、`@guard` 在内、返回 `ToolResult`、
末尾 `mcp.run(transport="stdio")`。真写一个技能时照这个抄即可。
"""
from mcp.server.fastmcp import FastMCP

from app.schema.agent_schema import ToolResult
from mcp_service.utils import guard

mcp = FastMCP("Echo")


@mcp.tool()
@guard
def demo_echo(text: str) -> ToolResult:
    """把传入的文本原样回显（夹具用）。"""
    return ToolResult(success=True, content=f"echo: {text}")


if __name__ == "__main__":
    mcp.run(transport="stdio")
