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

注意：Tavily 系 wrapper 的上游失败形态是**返回值而不是异常**（guard 拦不到），其翻译
（`_translate_upstream` 与特制渲染）只被 web_search server 使用，已随单一职责移到
`mcp_service/web_search.py`，见该文件。

异常替代约定：参数非法**不要 raise 裸 ValueError**（guard 会把它当 internal_error，
模型读到"非输入问题、无需重试"而无法修正），应 raise `app.exception.InvalidArgumentError`。
"""
from __future__ import annotations

import functools
import inspect
import logging
import re

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
