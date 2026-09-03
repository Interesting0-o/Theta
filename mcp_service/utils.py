"""mcp_service 共享工具函数。

当前只包含 `guard` 装饰器：把工具函数抛出的异常统一收口成 `ToolResult`，
**保证异常绝不漏出工具边界**——否则异常会一路冒到 FastMCP，导致 MCP 子进程崩溃。
"""
from __future__ import annotations

import functools
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


def guard(fn):
    """工具的异常安全网：把异常分类后转成 `ToolResult`，只 return 不 raise。

    分类依据（EXCEPTION_DESIGN.md §4/§7）：
    - `AgentError` 及其子类（可预期的业务失败，如路径越界）→
      转成 `error_type=语义标记` 的 `ToolResult`，正文携带事实，供大模型分析、调整。
    - 其余 `Exception`（内部 bug）→ `logger.exception` 留全 traceback 给开发者，
      返回 `error_type="internal_error"`，明示"非输入问题"，绝不让 bug 伪装成工具失败
      误导大模型去重试。

    用法：作为工具函数最内层装饰器（`@mcp.tool()` 在外、`@guard` 在内），
    保证 FastMCP 永远只收到 `ToolResult`，异常不可能漏出去。
    """
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AgentError as e:
            # 可预期业务失败 → 大模型该分析、该调整
            return ToolResult(
                success=False,
                error_type=_type_token(type(e)),
                content=str(e),
            )
        except Exception:  # noqa: BLE001 —— guard 是有意兜底全部异常的最后一层网
            # 内部 bug → 开发者来修，大模型别管
            logger.exception("工具 %s 发生未预期异常", fn.__name__)
            return ToolResult(
                success=False,
                error_type="internal_error",
                content="工具内部错误（非输入问题），无需重试。",
            )

    return wrapped
