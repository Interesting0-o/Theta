from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Weather")

@mcp.tool()
def get_weather(location:str)->str:
    """获取某地的天气"""
    return f"{location} is sunny"