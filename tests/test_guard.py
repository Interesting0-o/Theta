"""guard 装饰器 + 领域异常体系的测试（对应 docs/EXCEPTION_DESIGN.md §10 的 L1/L3）。

直接调用原始函数、不经过 FastMCP，与 tests/test_file_io.py 一致。
本文件不 import mcp_service.file_io，因此**不要求设置 WORKSPACE_PATH**，可独立运行：

    uv run python -m pytest tests/test_guard.py

覆盖要点：
- L1 异常体系：WorkspaceViolationError 挂在共享根 AgentError 下、携带结构化字段
- L3 边界分类：guard 把"越界 / 内部 bug"分别转成 error_type="workspace_violation" /
  "internal_error"；内部 bug 绝不伪装成工具失败、绝不 re-raise
- 正文：模型只读到已分类的一句话，读不到原始 traceback
"""
import asyncio
import logging

from app.exception import AgentError, ConfigError, InvalidArgumentError, WorkspaceViolationError
from app.schema.agent_schema import ToolResult
from mcp_service.utils import guard


# ---------------- L1 异常体系 ----------------

def test_domain_exceptions_root_on_agent_error():
    assert issubclass(AgentError, Exception)
    for cls in (ConfigError, InvalidArgumentError, WorkspaceViolationError):
        assert issubclass(cls, AgentError)


def test_workspace_violation_carries_structured_fields():
    err = WorkspaceViolationError("/etc/passwd", "/home/ws")
    assert err.path == "/etc/passwd"
    assert err.workspace == "/home/ws"
    assert str(err) == "路径 /etc/passwd 不在工作区/home/ws内"


# ---------------- L3 边界分类 ----------------

def test_guard_passes_success_through():
    @guard
    def ok() -> ToolResult:
        return ToolResult(success=True, content="done")

    result = ok()
    assert result.success is True
    assert result.content == "done"


def test_guard_converts_domain_error_to_workspace_violation():
    @guard
    def outside() -> ToolResult:
        raise WorkspaceViolationError("/etc/passwd", "/home/ws")

    result = outside()
    assert result.success is False
    assert result.error_type == "workspace_violation"
    assert result.content == "路径 /etc/passwd 不在工作区/home/ws内"


def test_guard_converts_invalid_argument_not_internal_error():
    # 参数非法是"模型可修正的输入问题"，必须是 invalid_argument，绝不冒充 internal_error
    @guard
    def bad_arg() -> ToolResult:
        raise InvalidArgumentError("search_depth 必须是 basic / advanced 之一")

    result = bad_arg()
    assert result.success is False
    assert result.error_type == "invalid_argument"
    assert "search_depth" in result.content


def test_guard_marks_bug_as_internal_error_without_leaking(caplog):
    @guard
    def bug() -> ToolResult:
        return None.split()  # AttributeError：内部 bug，不是输入问题

    with caplog.at_level(logging.ERROR, logger="mcp_service.utils"):
        result = bug()  # 只 return，绝不 re-raise

    assert result.success is False
    assert result.error_type == "internal_error"
    assert "无需重试" in result.content  # 明示模型别去重试
    assert "AttributeError" not in result.content  # 原始 bug 细节不喂给模型
    assert any(rec.exc_info for rec in caplog.records)  # traceback 留在开发者日志


# ---------------- 边界/元信息 ----------------

def test_guard_preserves_tool_metadata():
    @guard
    def my_tool(path: str) -> ToolResult:
        """写文件的工具。"""
        return ToolResult(success=True, content=path)

    assert my_tool.__name__ == "my_tool"
    assert my_tool.__doc__ == "写文件的工具。"


def test_tool_result_error_type_defaults_to_none():
    result = ToolResult(success=True, content="ok")
    assert result.error_type is None


# ---------------- L3 边界分类（异步工具） ----------------

def test_guard_async_passes_success_through():
    @guard
    async def ok() -> ToolResult:
        return ToolResult(success=True, content="done")

    result = asyncio.run(ok())
    assert result.success is True
    assert result.content == "done"


def test_guard_async_converts_domain_error_to_workspace_violation():
    @guard
    async def outside() -> ToolResult:
        raise WorkspaceViolationError("/etc/passwd", "/home/ws")

    result = asyncio.run(outside())
    assert result.success is False
    assert result.error_type == "workspace_violation"
    assert result.content == "路径 /etc/passwd 不在工作区/home/ws内"


def test_guard_async_converts_invalid_argument_not_internal_error():
    @guard
    async def bad_arg() -> ToolResult:
        raise InvalidArgumentError("max_results 须在 1~20 之间")

    result = asyncio.run(bad_arg())
    assert result.success is False
    assert result.error_type == "invalid_argument"
    assert "max_results" in result.content


def test_guard_async_marks_bug_as_internal_error_without_leaking(caplog):
    @guard
    async def bug() -> ToolResult:
        await asyncio.sleep(0)
        return None.split()  # AttributeError：发生在 await 之后，同样要收口

    with caplog.at_level(logging.ERROR, logger="mcp_service.utils"):
        result = asyncio.run(bug())

    assert result.success is False
    assert result.error_type == "internal_error"
    assert "无需重试" in result.content
    assert "AttributeError" not in result.content
    assert any(rec.exc_info for rec in caplog.records)


def test_guard_async_preserves_metadata_and_remains_coroutine():
    @guard
    async def my_tool(query: str) -> ToolResult:
        """搜索工具。"""
        return ToolResult(success=True, content=query)

    # FastMCP 靠 iscoroutinefunction 判断是否异步调用，包装后必须仍是协程函数
    assert asyncio.iscoroutinefunction(my_tool)
    assert my_tool.__name__ == "my_tool"
    assert my_tool.__doc__ == "搜索工具。"
    assert asyncio.run(my_tool("q")).content == "q"
