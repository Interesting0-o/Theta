"""web_search MCP server：把 Tavily 搜索能力封装成 async 工具，返回统一 ToolResult。

错误处理约定（与 docs/EXCEPTION_DESIGN.md 对齐，区别于文件工具处"抛 typed 异常"的做法——
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
from typing import Any, Callable, Dict, List, Literal, Optional

from langchain_tavily import TavilyCrawl, TavilyExtract, TavilyResearch, TavilySearch
from mcp.server.fastmcp import FastMCP

from app.exception import ConfigError, InvalidArgumentError
from app.schema.agent_schema import ToolResult
from mcp_service.utils import guard

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


# ---------------- Tavily 上游返回翻译（四个工具共用，只被本模块使用） ----------------
#
# Tavily 各 wrapper（Search / Extract / Crawl / Research）把上游失败吞成**返回值**、从不
# raise：空结果抛 ToolException 被 handle_tool_error 渲染成字符串，其余异常返回
#  {"error": ...}。guard 拦不到这些失败，因此在这里显式分类，否则"错误"会以 success=True
# 喂给模型。四类形态：
# - dict 且含顶层 "error" → upstream_error（key 无效 / 限流 / 网络等）
# - dict 正常 → success，正文由各工具的 render 函数渲染（各工具返回结构不同，各自提供）
# - str → no_results（Tavily 的空结果/无内容错误串，含改进建议，模型可据此调整参数）
# - 其它 → upstream_error（如 stream=True 时 research 返回的字节流对象）
#
# 渲染原则：
# - **不截断工具结果**。截断会静默丢失信息且不可恢复（模型会把残缺当完整来推理）；结果
#   大小应由**工具参数**控制（模型自己选 max_results / URL 粒度等），渲染层只做保真呈现。
# - 成功 dict 的正文由 render_success 渲染成字符串——ToolResult.content 的类型是 str，
#   直接塞 dict 会在构造时抛 ValidationError。
# - search 渲染剔除 score/raw_content 等是**有意的字段选择**：那是可选附料，不是被请求
#   的交付物；模型若要整页正文应组合调用 extract_urls。extract/crawl/research 的特制渲染
#   遵循同一原则：只渲染交付物（提取正文 / 页面正文 / 报告），附料（response_time/usage
#   等）一律不渲染。

def _translate_upstream(raw: Any, *, render_success: Callable[[Dict[str, Any]], str]) -> ToolResult:
    """把 Tavily 系 wrapper 的返回统一翻译成 ToolResult。"""
    if isinstance(raw, dict):
        if "error" in raw:
            return ToolResult(
                success=False,
                error_type="upstream_error",
                content=f"上游返回错误：{raw['error']}",
            )
        return ToolResult(success=True, content=render_success(raw))
    if isinstance(raw, str):
        return ToolResult(success=False, error_type="no_results", content=raw)
    # 无法识别的类型（如 stream=True 时的字节流）不是交付物，repr 只截前 500 字符防止
    # 巨量垃圾灌入上下文；与"不截断工具结果"原则不冲突——那一条只针对成功交付物。
    return ToolResult(
        success=False,
        error_type="upstream_error",
        content=f"返回了无法识别的数据：{str(raw)[:500]}",
    )


def _render_results(raw: Dict[str, Any]) -> str:
    """把 TavilySearch 成功返回的 results 列表压成模型友好的 markdown 文本。

    按"交付物"取舍：保留 AI 摘要（若有）+ 每条结果的标题/URL/content 片段；剔除
    score / follow_up_questions / response_time 与可选的大字段 raw_content——那是附料，
    模型若要整页正文应改用 extract_urls 定向提取。
    """
    lines: List[str] = []
    answer = raw.get("answer")
    if isinstance(answer, str) and answer.strip():
        lines.append(f"AI 摘要：{answer}\n")

    items = raw.get("results") or []
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            lines.append(f"{i}. {item}")
            continue
        title = str(item.get("title") or "无标题")
        url = item.get("url")
        content = str(item.get("content") or "").strip()
        lines.append(f"{i}. {title}")
        if url:
            lines.append(f"   {url}")
        if content:
            lines.append(f"   {content}")

    if not lines:
        return f"未找到与 {raw.get('query', '')!r} 相关的结果。"
    return "\n".join(lines)


def _search_result_to_text(raw: Any) -> ToolResult:
    """web_search 的返回翻译：成功 dict 用 _render_results 渲染，失败形态走 _translate_upstream。"""
    return _translate_upstream(raw, render_success=_render_results)


def _render_failed(raw: Dict[str, Any], label: str, lines: List[str]) -> None:
    """把 Tavily 返回里的 failed_results（未取到的 URL 列表）显式追加进渲染行。"""
    failed = raw.get("failed_results") or []
    if failed:
        lines.append(f"{label}: {', '.join(str(u) for u in failed)}")


def _render_extract(raw: Dict[str, Any]) -> str:
    """把 TavilyExtract 成功返回渲染成文本：每条结果 = 编号 + URL + 提取正文。

    提取正文（raw_content）就是本工具的交付物，不截断；附料（response_time/usage 等）
    不渲染。失败的 URL 单独列出，让模型知道哪些源没取到。
    """
    lines: List[str] = []
    items = raw.get("results") or []
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            lines.append(f"{i}. {item}")
            continue
        url = item.get("url")
        lines.append(f"{i}. {url or '(无 URL)'}")
        content = item.get("raw_content")
        if content:
            lines.append(str(content).strip())
        images = item.get("images")
        if isinstance(images, list) and images:
            lines.append(f"   图片: {', '.join(str(u) for u in images)}")

    _render_failed(raw, "提取失败的 URL", lines)

    if not lines:
        return "未从指定 URL 提取到内容。"
    return "\n".join(lines)


def _render_crawl(raw: Dict[str, Any]) -> str:
    """把 TavilyCrawl 成功返回渲染成文本：每条结果 = 编号 + 标题 + URL + 页面正文。"""
    lines: List[str] = []
    items = raw.get("results") or []
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            lines.append(f"{i}. {item}")
            continue
        title = item.get("title") or "(无标题)"
        url = item.get("url")
        content = item.get("content") or item.get("raw_content") or ""
        lines.append(f"{i}. {title}")
        if url:
            lines.append(f"   {url}")
        if content:
            lines.append(str(content).strip())

    _render_failed(raw, "爬取失败的 URL", lines)

    if not lines:
        return "未爬取到任何页面内容。"
    return "\n".join(lines)


def _render_research(raw: Dict[str, Any]) -> str:
    """把 TavilyResearch 成功返回渲染成文本。

    创建接口只回任务回执（status/request_id/input/model）；若返回里带报告正文
    （content）与引用来源（sources，如已完成的检索结果），一并渲染。
    """
    lines: List[str] = [
        f"研究任务状态: {raw.get('status', '未知')}",
        f"request_id: {raw.get('request_id', '未知')}",
        f"研究课题: {raw.get('input', '未知')}",
    ]
    model = raw.get("model")
    if model:
        lines.append(f"使用模型: {model}")
    created = raw.get("created_at")
    if created:
        lines.append(f"创建时间: {created}")

    content = raw.get("content")
    if content:
        lines.append(str(content))
    sources = raw.get("sources") or []
    if sources:
        lines.append("引用来源:")
        for src in sources:
            if isinstance(src, dict):
                title = src.get("title")
                url = src.get("url")
                if title and url:
                    lines.append(f"- {title}  {url}")
                else:
                    lines.append(f"- {url or title or src}")
            else:
                lines.append(f"- {src}")
    return "\n".join(lines)


def _extract_result_to_text(raw: Any) -> ToolResult:
    """extract_urls 的返回翻译：成功 dict 用 _render_extract 渲染，失败形态走 _translate_upstream。"""
    return _translate_upstream(raw, render_success=_render_extract)


def _crawl_result_to_text(raw: Any) -> ToolResult:
    """crawl_website 的返回翻译：成功 dict 用 _render_crawl 渲染，失败形态走 _translate_upstream。"""
    return _translate_upstream(raw, render_success=_render_crawl)


def _research_result_to_text(raw: Any) -> ToolResult:
    """deep_research 的返回翻译：成功 dict 用 _render_research 渲染，失败形态走 _translate_upstream。"""
    return _translate_upstream(raw, render_success=_render_research)


if __name__ == "__main__":
    mcp.run(transport="stdio")
