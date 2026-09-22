"""app/platform/mcp.py::load_mcp_tool 对工作区两类配置错误的处理。

两个用例只命中 load_mcp_tool 开头的 _validate_workspace（前置拦截），
不会真正拉起 MCP 子进程。

注意：`app.platform.mcp`（宿主侧 MCP 边界，2026-09-21 从 app/agent 搬来）不 import
app.agent，故本文件**本身不需要 .env / WORKSPACE_PATH**（导入依赖 langchain_mcp_adapters）。
"""
import asyncio
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

import app.platform.mcp as mcp_module
from app.platform.mcp import load_mcp_tool
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


def test_build_servers_always_includes_file_io_terminal_and_dispatch(tmp_path, monkeypatch):
    # 密钥为空时也只跳过 web_search，文件 / 终端 / 派发三样必须保留
    monkeypatch.setattr(mcp_module, "get_settings", lambda: _fake_settings(""))
    servers = mcp_module._build_servers(str(tmp_path))
    assert set(servers) == {"file_io", "terminal", "dispatch"}


def test_build_servers_skips_web_search_when_key_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "get_settings", lambda: _fake_settings(""))
    servers = mcp_module._build_servers(str(tmp_path))
    assert "web_search" not in servers


def test_build_servers_includes_web_search_when_key_present(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_module, "get_settings", lambda: _fake_settings("tavily-key"))
    servers = mcp_module._build_servers(str(tmp_path))
    assert set(servers) == {"file_io", "terminal", "dispatch", "web_search"}


def test_dispatch_server_env_forwards_inbox_url_only_when_present(tmp_path, monkeypatch):
    """派发 server 的 env 要带上主侧收件箱的**实际**地址。

    这条转发链是 `主进程 env →（_build_servers）→ dispatch server env →（它的
    _worker_child_env）→ worker`：漏掉本处这一跳不会报错，只会静默发往默认端口——
    主侧一换端口（AGENT_INBOX_PORT），worker 的审批请求就无人应答。
    """
    monkeypatch.setattr(mcp_module, "get_settings", lambda: _fake_settings(""))

    monkeypatch.setenv("AGENT_INBOX_URL", "http://127.0.0.1:25011")
    env = mcp_module._build_servers(str(tmp_path))["dispatch"]["env"]
    assert env["AGENT_INBOX_URL"] == "http://127.0.0.1:25011"
    assert env["WORKSPACE_PATH"] == str(tmp_path)

    # 没起收件箱的宿主（evaluation / langgraph dev）不带该键 → worker 回退默认端口
    monkeypatch.delenv("AGENT_INBOX_URL", raising=False)
    assert "AGENT_INBOX_URL" not in mcp_module._build_servers(str(tmp_path))["dispatch"]["env"]

    monkeypatch.setenv("AGENT_INBOX_URL", "   ")
    assert "AGENT_INBOX_URL" not in mcp_module._build_servers(str(tmp_path))["dispatch"]["env"]


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
