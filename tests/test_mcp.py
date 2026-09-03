"""app/agent/mcp.py::load_mcp_tool 对工作区两类配置错误的处理。

两个用例只命中 load_mcp_tool 开头的 _validate_workspace（前置拦截），
不会真正拉起 MCP 子进程。

注意：import app.agent.mcp 会触发 app.agent/__init__ → graph → model，
model 顶层调用 get_settings()，因此运行本文件要求 .env 存在（CLAUDE.md 前提）。
"""
import asyncio

import pytest

from app.agent.mcp import load_mcp_tool
from app.exception import ConfigError


def test_load_mcp_tool_rejects_missing_workspace():
    with pytest.raises(ConfigError, match="WORKSPACE_PATH 未设置"):
        asyncio.run(load_mcp_tool(""))


def test_load_mcp_tool_rejects_nonexistent_workspace(tmp_path):
    absent = tmp_path / "no_such_workspace_dir"
    with pytest.raises(ConfigError, match="不存在"):
        asyncio.run(load_mcp_tool(str(absent)))
