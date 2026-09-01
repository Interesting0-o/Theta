from langchain_core.tools import tool,BaseTool
from langchain_tavily import TavilySearch
from app.config import get_settings
from app.schema.agent_schema import ToolResult
from pathlib import Path
from typing import List
settings = get_settings()

web_search= TavilySearch(
    tavily_api_key = settings.TAVILY_API_KEY
)

core_tools:List[BaseTool]= [
    web_search
]
