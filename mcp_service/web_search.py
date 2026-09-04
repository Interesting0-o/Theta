"""web_search MCP server：把 Tavily 搜索能力封装成 async 工具，返回统一 ToolResult。

错误处理约定（与 EXCEPTION_DESIGN.md 对齐，区别于文件工具处"抛 typed 异常"的做法——
见下）：
- 参数非法：**raise `InvalidArgumentError`（AgentError 子类）**，替代裸 ValueError / 就地
  return。guard 按类名归成 error_type="invalid_argument"，正文携带合法取值，模型可修正重试；
  裸 ValueError 会被 guard 当内部 bug（internal_error，"非输入问题，无需重试"），语义相反。
- 上游失败 / 空结果：形态是**返回值而不是异常**（TavilySearch._arun 吞异常后返回
  {"error": ...}；空结果抛 ToolException 被 handle_tool_error 转成字符串），guard 拦不到，
  因此由 `_search_result_to_text` 显式翻译成失败 ToolResult，否则"错误"会以 success=True
  喂给模型。
- 其余真 bug / 上游真异常继续靠 guard 收口成 internal_error（留 traceback，不冒充工具失败）。

返回内容一律是**字符串**：ToolResult.content 的类型是 str，把 Tavily 返回的 dict 直接塞进
content 会在构造时抛 ValidationError（曾导致该工具必然 internal_error）。参数类型收窄成
Literal 后 FastMCP 会在 JSON Schema 里生成 enum，模型只看到合法取值。

注：extract_urls / crawl_website / deep_research 尚未接入 mcp.py 拉起，不影响运行；
参数校验用 InvalidArgumentError、返回结果与 web_search 一样经 _translate_upstream 翻译成
失败 ToolResult / 特制渲染的字符串正文（content 恒为 str，不违反 ToolResult schema）。
"""
import os
from typing import Any, Dict, List, Literal, Optional

from langchain_tavily import TavilyCrawl, TavilyExtract, TavilyResearch, TavilySearch
from mcp.server.fastmcp import FastMCP

from app.exception import ConfigError, InvalidArgumentError
from app.schema.agent_schema import ToolResult
from mcp_service.utils import (
    guard,
    _crawl_result_to_text,
    _extract_result_to_text,
    _research_result_to_text,
    _search_result_to_text,
)

# 枚举与上游 TavilySearch 的合法取值保持一致
SearchDepth = Literal["basic", "advanced", "fast", "ultra-fast"]
SearchTopic = Literal["general", "news", "finance"]
SearchTimeRange = Literal["day", "week", "month", "year"]

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
if not TAVILY_API_KEY:
    raise ConfigError("TAVILY_API_KEY未设置")

mcp = FastMCP("WebSearch")


@mcp.tool()
@guard
async def web_search(
    query: str,
    search_depth: SearchDepth = "basic",
    topic: SearchTopic = "general",
    time_range: Optional[SearchTimeRange] = None,
    max_results: int = 10,
) -> ToolResult:
    """联网搜索指定关键词，返回清洗后的检索结果（标题 + URL + 内容片段）。

    Args:
        query: 搜索关键词（必填）。
        search_depth: 搜索深度。basic=常规、advanced=更彻底（复杂/冷门话题用）、
            fast/ultra-fast=低延迟优先。默认 "basic"。
        topic: 结果分类。general=通用（默认）、news=新闻资讯、finance=财经。
        time_range: 时间范围，day/week/month/year。默认不限制；只在你确实需要
            限定新旧（如"最近的"）时显式传值，避免默认过滤掉有价值的旧资料。
        max_results: 返回结果条数，1~20，默认 10。
    """
    # 枚举本应在 schema 层被拦；这里再兜底一次，只 raise InvalidArgumentError（不 raise
    # 裸 ValueError，否则 guard 会归成 internal_error），由 guard 归成 invalid_argument。
    if search_depth not in ("basic", "advanced", "fast", "ultra-fast"):
        raise InvalidArgumentError(
            "search_depth 必须是 basic / advanced / fast / ultra-fast 之一"
        )
    if topic not in ("general", "news", "finance"):
        raise InvalidArgumentError("topic 必须是 general / news / finance 之一")
    if time_range is not None and time_range not in ("day", "week", "month", "year"):
        raise InvalidArgumentError("time_range 必须是 day / week / month / year 或省略")
    if not 1 <= max_results <= 20:
        raise InvalidArgumentError("max_results 须在 1~20 之间")

    # search_depth/time_range/topic 既可实例化也可调用时传；但 max_results 上游
    # 只能实例化时设，因此按调用参数每次现建实例（而非模块级单例）。
    search_tool = TavilySearch(
        tavily_api_key=TAVILY_API_KEY,
        search_depth=search_depth,
        topic=topic,
        time_range=time_range,
        max_results=max_results,
    )
    raw = await search_tool.ainvoke(query)
    return _search_result_to_text(raw)


@mcp.tool()
@guard
async def extract_urls(
    urls: List[str],
    extract_depth: str = "basic",
    include_images: bool = False,
    format: str = "markdown",
    query: Optional[str] = None,
) -> ToolResult:
    """
    从一个或多个指定的 URL 中，提取并清洗出结构化的网页内容。
    非常适合用来获取特定页面的详细、干净文本。
    Args:
        urls (List[str]): 要提取的 URL 列表
        extract_depth (str): 提取深度,只允许"basic", "advanced"默认"basic"
        include_images (bool): 是否包含图片,默认为False
        format (str): 输出格式,"markdown"或"text",默认为"markdown"
        query (str): 一个查询字符串，用于在提取时过滤和优先处理与查询相关的内容,默认为None
    """
    # 参数校验
    if extract_depth not in ["basic", "advanced"]:
        raise InvalidArgumentError("extract_depth must be 'basic' or 'advanced'")
    # ... 其他校验 ...

    # 每次调用动态创建工具实例
    tool = TavilyExtract(
        api_key=TAVILY_API_KEY,
        extract_depth=extract_depth,
        include_images=include_images,
        format=format,
    )

    # 构建调用参数
    invoke_args: Dict[str, Any] = {"urls": urls}
    if query:
        invoke_args["query"] = query

    raw_result = await tool.ainvoke(invoke_args)

    return _extract_result_to_text(raw_result)


@mcp.tool()
@guard
async def crawl_website(
    url: str,
    max_depth: int = 1,
    max_breadth: int = 20,
    limit: int = 50,
    extract_depth: str = "basic",
    instructions: Optional[str] = None,
) -> ToolResult:
    """
    从一个起始 URL 开始，结构化地爬取整个网站。
    Args:
        url (str): 要爬取的起始 URL
        max_depth (int): 最大爬取深度(跳数),默认为1
        max_breadth (int): 最大爬取广度即每一层级最多爬取的页面数量,默认为20
        limit (int): 最大爬取网页数,默认为50
        extract_depth (str): 提取深度,只允许"basic", "advanced"默认"basic"
        instructions (str): 爬取指令,默认为None
    """
    # 参数校验
    if max_depth < 1:
        raise InvalidArgumentError("max_depth must be at least 1")

    tool = TavilyCrawl(
        api_key=TAVILY_API_KEY,
        max_depth=max_depth,
        max_breadth=max_breadth,
        limit=limit,
        extract_depth=extract_depth,
    )

    invoke_args = {"url": url}
    if instructions:
        invoke_args["instructions"] = instructions

    raw_result = await tool.ainvoke(invoke_args)
    return _crawl_result_to_text(raw_result)


@mcp.tool()
@guard
async def deep_research(
    query: str,
    model: str = "auto",
    stream: bool = False,
    citation_format: str = "numbered",
    output_schema: Optional[Dict[str, Any]] = None,
) -> ToolResult:
    """
    执行深度研究，返回一份综合性的研究报告。

    Args:
        query (str): 需要深度研究的问题或主题。
        model (str): 使用的模型，可选 "mini", "pro", "auto"。默认为 "auto"。
        stream (bool): 是否流式返回进度。默认为 False。
        citation_format (str): 引用格式，如 "numbered", "mla", "apa", "chicago"。
        output_schema (Optional[Dict]): 自定义的 JSON Schema，用于结构化输出。

    Returns:
        ToolResult: 包含研究报告内容和引用来源的结果对象。
    """
    # 1. 参数校验
    if model not in ["mini", "pro", "auto"]:
        raise InvalidArgumentError("model 必须为 'mini', 'pro' 或 'auto'")
    tool = TavilyResearch(
        api_key=TAVILY_API_KEY,
        model=model,
        stream=stream,
        citation_format=citation_format,
        # 如果提供了 output_schema，则传入
        **( {"output_schema": output_schema} if output_schema else {} )
    )

    raw_result = await tool.ainvoke({"input": query})
    return _research_result_to_text(raw_result)


if __name__ == "__main__":
    mcp.run(transport="stdio")
