"""worker（子任务 agent MCP server）的 hermetic 单测。

不拉真实 MCP 孙进程、不碰真实 LLM：run_subtask_impl 接受注入的假 model / 假 tools，
build_subtask_graph 用它组装出图并跑通"llm →(tool_calls)→ queue → tool → llm → END"。
另覆盖只读 allowlist 过滤与工作区 env 校验。

注意：read_only_tools 现收在 mcp_service/sub_agent.py；调用 impl 内部懒加载会经
app/agent/__init__ → model 触发 get_settings()，因此运行本文件要求 .env 存在
（CLAUDE.md 前提）；不 import mcp_service.file_io，故无需 WORKSPACE_PATH。
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.exception import ConfigError, InvalidArgumentError
from app.schema.agent_schema import ToolResult
from mcp_service.sub_agent import _resolve_workspace, read_only_tools, run_subtask_impl

FINAL = "结论：子任务已完成"


class FakeReadTool:
    """假只读工具：有 name + async ainvoke，记录被调用次数。"""

    name = "read_file"

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, args: dict):
        self.calls += 1
        return ToolResult(success=True, content="文件内容 abc")


class FinalModel:
    """假模型：永远直接给最终答复（不调工具）。"""

    async def ainvoke(self, messages):
        return AIMessage(content=FINAL)


class ToolThenFinalModel:
    """假模型：尚未看到 ToolMessage → 发 read_file 调用；已看到 → 给最终答复。"""

    def __init__(self, tool: FakeReadTool) -> None:
        self.tool = tool

    def bind_tools(self, tools):
        return self  # 只读工具存在时 build_subtask_graph 会调用；直接返回自身

    async def ainvoke(self, messages):
        if any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content=FINAL)
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": self.tool.name,
                    "args": {"path": "a.txt"},
                    "id": "call_1",
                    "type": "tool_call",
                }
            ],
        )


def _run(coro):
    return asyncio.run(coro)


def test_run_subtask_no_tool_direct_answer(tmp_path):
    """无只读工具、模型直接作答：返回结论正文，success=True。"""
    result = _run(run_subtask_impl("读一下项目结构", str(tmp_path), model=FinalModel(), tools=[]))
    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.error_type is None
    assert result.content == FINAL


def test_run_subtask_tool_roundtrip(tmp_path):
    """模型先发 read_file 调用 → 图走 queue→tool→llm，假工具被调一次，最终回结论。"""
    stub = FakeReadTool()
    result = _run(
        run_subtask_impl("读 a.txt", str(tmp_path), model=ToolThenFinalModel(stub), tools=[stub])
    )
    assert result.success is True
    assert stub.calls == 1  # 假只读工具真的被 ToolNode 执行了一次
    assert result.content == FINAL


def test_run_subtask_blank_task_rejected(tmp_path):
    """空白 task → InvalidArgumentError（guard 会归成 invalid_argument）。"""
    with pytest.raises(InvalidArgumentError):
        _run(run_subtask_impl("   ", str(tmp_path), model=FinalModel(), tools=[]))


def test_resolve_workspace_missing_env_raises(monkeypatch):
    """env WORKSPACE_PATH 缺失 → ConfigError。"""
    monkeypatch.delenv("WORKSPACE_PATH", raising=False)
    with pytest.raises(ConfigError, match="WORKSPACE_PATH 未设置"):
        _resolve_workspace()


def test_resolve_workspace_nonexistent_dir_raises(monkeypatch, tmp_path):
    """env WORKSPACE_PATH 指向不存在目录 → ConfigError。"""
    absent = tmp_path / "no_such_dir"
    monkeypatch.setenv("WORKSPACE_PATH", str(absent))
    with pytest.raises(ConfigError, match="不存在"):
        _resolve_workspace()


def test_read_only_tools_filter():
    """只保留 tool.json 中 need_review:false 且 source∈{file_io,git} 的只读检索工具。"""
    cfg = {
        "read_file": {"need_review": False, "source": "mcp_service/file_io"},
        "glob": {"need_review": False, "source": "mcp_service/file_io"},
        "search_content": {"need_review": False, "source": "mcp_service/file_io"},
        "list_repos": {"need_review": False, "source": "mcp_service/git"},
        "git_log": {"need_review": False, "source": "mcp_service/git"},
        "write_file": {"need_review": True, "source": "mcp_service/file_io"},
        "run_command": {"need_review": True, "source": "mcp_service/terminal"},
        "web_search": {"need_review": True, "source": "mcp_service/web_search"},
        "create_plan": {"need_review": False, "source": "plan"},
        "read_note": {"need_review": False, "source": "notes"},
    }

    class _FakeTool:
        def __init__(self, name):
            self.name = name

    names_in = [
        "web_search",
        "write_file",
        "read_file",
        "run_command",
        "glob",
        "create_plan",
        "read_note",
        "search_content",
        "list_repos",
        "git_log",
    ]
    kept = read_only_tools([_FakeTool(n) for n in names_in], cfg=cfg)
    assert [t.name for t in kept] == [
        "read_file",
        "glob",
        "search_content",
        "list_repos",
        "git_log",
    ]
