"""worker（子任务 agent MCP server）的 hermetic 单测。

不拉真实 MCP 孙进程、不碰真实 LLM：run_subtask_impl 接受注入的假 model / 假 tools，内部经
app/agent.graph::get_sub_agent_graph 组装出图并跑通"llm →(tool_calls)→ queue → review →
tool → llm → END"。另覆盖 worker 可用工具子集过滤与工作区 env 校验。

注意：worker_tools 现收在 app/agent/tools.py；调用 impl 内部懒加载会经
app/agent/__init__ → model 触发 get_settings()，因此运行本文件要求 .env 存在
（CLAUDE.md 前提）；不 import mcp_service.file_io，故无需 WORKSPACE_PATH。
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.tools import worker_tools
from app.exception import ConfigError, InvalidArgumentError
from app.schema.agent_schema import ToolResult
from mcp_service.sub_agent import _resolve_workspace, run_subtask_impl

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
        return self  # 工具存在时 get_sub_agent_graph 会 bind_tools；直接返回自身

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


def test_worker_tools_filter():
    """worker 子集 = 免审只读检索(file_io 读) ∪ 联网四件套(need_review:true)；其余剔除。

    git 源**已不在** `_WORKSPACE_SOURCES`（mcp_service/git.py 已删、git 走 run_command），
    所以哪怕 tool.json 里再冒出 need_review:false 的 git 条目，worker 也拿不到——下面放一行
    git 条目正是为了把这个边界钉住。
    """
    cfg = {
        "read_file": {"need_review": False, "source": "mcp_service/file_io"},
        "glob": {"need_review": False, "source": "mcp_service/file_io"},
        "search_content": {"need_review": False, "source": "mcp_service/file_io"},
        "list_repos": {"need_review": False, "source": "mcp_service/git"},
        "git_log": {"need_review": False, "source": "mcp_service/git"},
        "web_search": {"need_review": True, "source": "mcp_service/web_search"},
        "extract_urls": {"need_review": True, "source": "mcp_service/web_search"},
        "write_file": {"need_review": True, "source": "mcp_service/file_io"},
        "run_command": {"need_review": True, "source": "mcp_service/terminal"},
        "git_add": {"need_review": True, "source": "mcp_service/git"},
        "create_plan": {"need_review": False, "source": "plan"},
        "read_note": {"need_review": False, "source": "notes"},
        "dispatch_subtasks": {"need_review": False, "source": "mcp_service/dispatch"},
        "write_memory": {"need_review": False, "source": "memory"},
        "read_memory": {"need_review": False, "source": "memory"},
    }

    class _FakeTool:
        def __init__(self, name):
            self.name = name

    names_in = [
        "web_search",      # 联网 → 保留（need_review:true，会触发审批）
        "write_file",      # 工作区写 → 剔除
        "read_file",       # file_io 读 → 保留
        "run_command",     # 命令 → 剔除
        "glob",            # file_io 读 → 保留
        "git_add",         # git 写 → 剔除
        "search_content",  # file_io 读 → 保留
        "list_repos",      # git 读 → 剔除（git 源已不在 worker 白名单）
        "git_log",         # git 读 → 剔除（同上）
        "extract_urls",    # 联网 → 保留
        "create_plan",     # 编排 → 剔除
        "read_note",       # notes → 剔除
        "dispatch_subtasks",  # 派发 → 剔除
        "write_memory",    # 长期记忆 → 剔除（worker 不读写记忆、保持只读隔离）
        "read_memory",     # 长期记忆 → 剔除
    ]
    kept = worker_tools([_FakeTool(n) for n in names_in], cfg=cfg)
    assert [t.name for t in kept] == [
        "web_search",
        "read_file",
        "glob",
        "search_content",
        "extract_urls",
    ]


def test_worker_tools_honors_explicit_worker_allow():
    """终端下放靠 tool.json 的 `worker_allow` **逐条授权**。

    为什么不能沿用现有两条规则：它们都是"source 命中 ∧ need_review:false"，而 run_command
    必须保留 need_review:true（写命令要能触发审批）——按 source 放行会连 start_process 一起给，
    按 need_review 则一条终端工具都进不来。
    """
    cfg = {
        "run_command": {
            "need_review": True,
            "source": "mcp_service/terminal",
            "free_when": "readonly_shell",
            "worker_allow": True,
        },
        "process_wait": {"need_review": False, "source": "mcp_service/terminal", "worker_allow": True},
        "process_kill": {"need_review": True, "source": "mcp_service/terminal", "worker_allow": True},
        # 未标注的终端工具：即使 need_review:false 也不下放（worker 不该起常驻服务）
        "start_process": {"need_review": True, "source": "mcp_service/terminal"},
        "process_list_only": {"need_review": False, "source": "mcp_service/terminal"},
    }

    class _FakeTool:
        def __init__(self, name):
            self.name = name

    names_in = ["run_command", "process_wait", "process_kill", "start_process", "process_list_only"]
    kept = worker_tools([_FakeTool(n) for n in names_in], cfg=cfg)
    assert [t.name for t in kept] == ["run_command", "process_wait", "process_kill"]
