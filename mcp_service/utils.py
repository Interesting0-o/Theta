"""mcp_service 共享工具函数。

- `guard`：工具异常安全网（唯一的分类点）。把工具抛出的异常收口成 `ToolResult`，
  **保证异常绝不漏出工具边界**——否则异常会一路冒到 FastMCP，导致 MCP 子进程崩溃。
  同步与异步工具函数都支持（`inspect.iscoroutinefunction` 分派）。
  分类规则（EXCEPTION_DESIGN.md §4/§7）：
  - `AgentError` 及其子类（可预期的业务失败）→ 按类名转成 `error_type` 标记
    （例：InvalidArgumentError → "invalid_argument"、WorkspaceViolationError →
    "workspace_violation"），正文携带事实，供大模型分析、调整；
  - 其余 `Exception`（内部 bug）→ `logger.exception` 留全 traceback 给开发者，
    返回 `error_type="internal_error"`，明示"非输入问题"，绝不让 bug 伪装成工具失败。
- `_translate_upstream`：Tavily 系 wrapper 的上游失败形态是**返回值而不是异常**（成功 dict /
  {"error": ...} / 空结果的错误字符串），guard 拦不到，故在此显式分类成 upstream_error /
  no_results / 成功正文。

渲染原则（很重要）：
- **不截断工具结果**。截断会静默丢失信息且不可恢复（模型会把残缺当完整来推理）；结果的大小
  应由**工具参数**控制（模型自己选 max_results / URL 粒度等），渲染层只做保真呈现。
- 成功 dict 的正文由调用方给的 `render_success` 渲染成字符串——ToolResult.content 的类型是
  str，直接塞 dict 会在构造时抛 ValidationError。
- `_render_results`（search）剔除 score/raw_content 等是**有意的字段选择**：那是可选附料，
  不是被请求的交付物；模型若要整页正文应组合调用 extract_urls。extract/crawl/research 的
  特制渲染（`_render_extract` / `_render_crawl` / `_render_research`）遵循同一原则：
  只渲染交付物（提取正文 / 页面正文 / 报告），附料（response_time/usage 等）一律不渲染。

异常替代约定：参数非法**不要 raise 裸 ValueError**（guard 会把它当 internal_error，
模型读到"非输入问题、无需重试"而无法修正），应 raise `app.exception.InvalidArgumentError`；
而"上游返回数据 / 空结果"这类失败不走异常，由 `_translate_upstream` return 带 error_type 的
ToolResult。
"""
from __future__ import annotations

import functools
import inspect
import logging
import re
from typing import Any, Callable, Dict, List

from app.exception import AgentError
from app.schema.agent_schema import ToolResult

logger = logging.getLogger(__name__)


def _type_token(exc_type: type[BaseException]) -> str:
    """把异常类型名转成小写下划线的语义标记。

    例：WorkspaceViolationError → "workspace_violation"。
    """
    name = exc_type.__name__
    for suffix in ("Error", "Exception"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _error_result(fn_name: str, exc: BaseException) -> ToolResult:
    """把一次已捕获的工具异常分类成 `ToolResult`。

    必须在 `except` 块内调用，`logger.exception` 才能取到当前异常的 traceback。
    分类依据（EXCEPTION_DESIGN.md §4/§7）：
    - `AgentError` 及其子类（可预期的业务失败，如路径越界、参数非法）→
      转成 `error_type=语义标记` 的 `ToolResult`，正文携带事实，供大模型分析、调整；
    - 其余 `Exception`（内部 bug）→ `logger.exception` 留全 traceback 给开发者，
      返回 `error_type="internal_error"`，明示"非输入问题"，绝不让 bug 伪装成工具失败
      误导大模型去重试。
    """
    if isinstance(exc, AgentError):
        return ToolResult(
            success=False,
            error_type=_type_token(type(exc)),
            content=str(exc),
        )
    # 内部 bug → 开发者来修，大模型别管
    logger.exception("工具 %s 发生未预期异常", fn_name)
    return ToolResult(
        success=False,
        error_type="internal_error",
        content="工具内部错误（非输入问题），无需重试。",
    )


def guard(fn):
    """工具的异常安全网：把异常分类后转成 `ToolResult`，只 return 不 raise。

    用法：作为工具函数最内层装饰器（`@mcp.tool()` 在外、`@guard` 在内），
    保证 FastMCP 永远只收到 `ToolResult`，异常不可能漏出去。

    对异步工具函数（`async def`），`@guard` 会返回 async 包装并 `await` 原函数，
    因此协程体内的异常同样能被捕获收口（FastMCP 据此判断该工具需异步调用）。
    """
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def _async_wrapped(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 —— 覆盖 AgentError 与其内部 bug
                return _error_result(fn.__name__, exc)

        return _async_wrapped

    @functools.wraps(fn)
    def _sync_wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 —— 有意兜底全部异常的最后一层网
            return _error_result(fn.__name__, exc)

    return _sync_wrapped


# ---------------- Tavily 上游返回翻译（web_search server 的四个工具共用） ----------------

def _translate_upstream(raw: Any, *, render_success: Callable[[Dict[str, Any]], str]) -> ToolResult:
    """把 Tavily 系 wrapper 的返回统一翻译成 ToolResult。

    Tavily 各 wrapper（Search / Extract / Crawl / Research）把上游失败吞成**返回值**、从不
    raise：空结果抛 ToolException 被 handle_tool_error 渲染成字符串，其余异常返回
     {"error": ...}。guard 拦不到这些失败，因此在这里显式分类，否则"错误"会以 success=True
    喂给模型。四类形态：
    - dict 且含顶层 "error" → upstream_error（key 无效 / 限流 / 网络等）
    - dict 正常 → success，正文由 `render_success` 渲染（各工具返回结构不同，各自提供）
    - str → no_results（Tavily 的空结果/无内容错误串，含改进建议，模型可据此调整参数）
    - 其它 → upstream_error（如 stream=True 时 research 返回的字节流对象）
    """
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
