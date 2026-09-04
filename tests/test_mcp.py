"""app/agent/mcp.py::load_mcp_tool 对工作区两类配置错误的处理。

两个用例只命中 load_mcp_tool 开头的 _validate_workspace（前置拦截），
不会真正拉起 MCP 子进程。

注意：import app.agent.mcp 会触发 app.agent/__init__ → graph → model，
model 顶层调用 get_settings()，因此运行本文件要求 .env 存在（CLAUDE.md 前提）。
"""
import asyncio
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

import app.agent.mcp as mcp_module
from app.agent.mcp import load_mcp_tool
from app.exception import ConfigError


def _fake_settings(tavily_key: str):
    return SimpleNamespace(TAVILY_API_KEY=SecretStr(tavily_key))


def test_load_mcp_tool_rejects_missing_workspace():
    with pytest.raises(ConfigError, match="WORKSPACE_PATH 未设置"):
        asyncio.run(load_mcp_tool(""))


def test_load_mcp_tool_rejects_nonexistent_workspace(tmp_path):
    absent = tmp_path / "no_such_workspace_dir"
    with pytest.raises(ConfigError, match="不存在"):
        asyncio.run(load_mcp_tool(str(absent)))


def test_build_servers_always_includes_file_io_terminal_and_git(tmp_path, monkeypatch):
    # 密钥为空时也只跳过 web_search，文件/终端/git 能力必须保留
    monkeypatch.setattr(mcp_module, "get_settings", lambda: _fake_settings(""))
    servers = mcp_module._build_servers(str(tmp_path))
    assert set(servers) == {"file_io", "terminal", "git"}


def test_build_servers_skips_web_search_when_key_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "get_settings", lambda: _fake_settings(""))
    servers = mcp_module._build_servers(str(tmp_path))
    assert "web_search" not in servers


def test_build_servers_includes_web_search_when_key_present(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "get_settings", lambda: _fake_settings("tavily-key"))
    servers = mcp_module._build_servers(str(tmp_path))
    assert set(servers) == {"file_io", "terminal", "git", "web_search"}


def test_build_servers_web_search_env_carries_key_and_pythonpath(tmp_path, monkeypatch):
    # 子进程 cwd 在工作区，须有 PYTHONPATH 才能 import 到 app/mcp_service；
    # TAVILY_API_KEY 必须是明文 str（SecretStr 直接塞 env 会让子进程 import 失败）
    monkeypatch.setattr(mcp_module, "get_settings", lambda: _fake_settings("tavily-key"))
    servers = mcp_module._build_servers(str(tmp_path))
    env = servers["web_search"]["env"]
    assert env["TAVILY_API_KEY"] == "tavily-key"
    assert isinstance(env["TAVILY_API_KEY"], str)
    assert env["PYTHONPATH"] == str(mcp_module.PROJECT_ROOT)
    assert env["WORKSPACE_PATH"] == str(tmp_path)
