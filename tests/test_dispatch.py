"""派发 MCP server（`mcp_service/dispatch.py::dispatch_subtasks`）的 hermetic 单测。

不真 spawn worker（注入假 `worker_runner`），验证：
- 并发：N 个子任务各调一次 runner、并行执行、结果按输入顺序汇总成一条 ToolResult；
- 失败隔离：单 worker 失败不拖垮整批；
- 空列表被拒（guard 收成 invalid_argument）；
- 接线：`tool.json` 的 source 指向 `mcp_service/dispatch`、**不再**命中 ORCHESTRATE_SOURCES，
  且不进 worker 可用子集（那正是**递归派发**的防线）。

本模块（2026-09-21 从 app/agent/tools.py 搬来）import 不碰 app.agent、也不需要 WORKSPACE_PATH
——工作区在**调用期**才从 env 读并校验（与 file_io 的 import 期校验不同），所以下面用
monkeypatch 给每次调用一个真实存在的工作区目录。
"""
import asyncio
import json
from pathlib import Path

import pytest

import mcp_service.dispatch as dispatch_mod
from app.agent.nodes import ReviewNode
from app.agent.tools import worker_tools
from app.schema.agent_schema import ToolResult

_TOOL_JSON = Path(__file__).resolve().parent.parent / "app" / "agent" / "tool.json"


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


def test_source_registered_and_stays_out_of_orchestrate_and_worker():
    """三处接线一次钉住：tool.json 登记、不进编排队列、不进 worker 可用集。"""
    cfg = json.loads(_TOOL_JSON.read_text(encoding="utf-8"))
    assert cfg["dispatch_subtasks"] == {"need_review": False, "source": "mcp_service/dispatch"}
    # 编排队列不再收它：它是普通 MCP 工具，走 ToolNode（派生独立进程 = 四问的第四问）
    assert "dispatch" not in ReviewNode.ORCHESTRATE_SOURCES
    # 也不进 worker：否则 worker 能再派 worker（递归）。防线是"source 不命中 + 无 worker_allow"。
    assert worker_tools([_FakeTool("dispatch_subtasks")]) == []


def test_worker_child_env_forwards_inbox_url(monkeypatch):
    """worker 子进程 env 要带上主侧审批收件箱的**实际**地址（子进程 env 是替换制、不继承）。

    否则主侧一换端口（AGENT_INBOX_PORT），worker 还在往默认端口回传审批，链路静默断掉。
    地址由 `_build_servers` 透传进本 server 的 env（app/platform/mcp.py::_inbox_env）。
    """
    monkeypatch.setenv("AGENT_INBOX_URL", "http://127.0.0.1:25011")
    env = dispatch_mod._worker_child_env("/tmp/ws")
    assert env["AGENT_INBOX_URL"] == "http://127.0.0.1:25011"
    assert env["WORKSPACE_PATH"] == "/tmp/ws" and "PYTHONPATH" in env

    # 主侧没给地址时**不带该键**（带个空串会被 worker 当成地址用，而不是回退到默认）
    monkeypatch.delenv("AGENT_INBOX_URL", raising=False)
    assert "AGENT_INBOX_URL" not in dispatch_mod._worker_child_env("/tmp/ws")

    monkeypatch.setenv("AGENT_INBOX_URL", "   ")
    assert "AGENT_INBOX_URL" not in dispatch_mod._worker_child_env("/tmp/ws")


def test_workspace_missing_env_is_config_error(monkeypatch):
    """没工作区就不派——错在启动配置，guard 按类名归成 `config`
    （`ConfigError` → 去 Error 后缀 → snake_case，见 mcp_service/utils.py::_type_token）。"""
    monkeypatch.delenv("WORKSPACE_PATH", raising=False)
    result = asyncio.run(dispatch_mod.dispatch_subtasks(["查 X"]))
    assert isinstance(result, ToolResult) and not result.success
    assert result.error_type == "config"


def test_dispatch_subtasks_concurrent_summary(tmp_path, monkeypatch):
    """每个子任务调一次 worker runner、并发执行、结果按输入顺序汇总成一条正文。"""
    calls: list[tuple] = []
    counter = {"active": 0, "max": 0}

    async def fake_runner(task: str, workspace: str) -> str:
        calls.append((task, workspace))
        counter["active"] += 1
        counter["max"] = max(counter["max"], counter["active"])
        await asyncio.sleep(0.02)
        counter["active"] -= 1
        return f"结论：{task}"

    monkeypatch.setattr(dispatch_mod, "worker_runner", fake_runner)
    monkeypatch.setenv("WORKSPACE_PATH", str(tmp_path))

    result = asyncio.run(dispatch_mod.dispatch_subtasks(["调研A", "调研B", "调研C"]))
    assert counter["max"] >= 3  # gather 真并发，而非串行
    assert set(calls) == {(t, str(tmp_path)) for t in ("调研A", "调研B", "调研C")}

    assert isinstance(result, ToolResult) and result.success
    content = result.content
    # 顺序保持输入序；每条子任务都有结论；工作区来自启动 env、不是调用参数
    assert content.index("调研A") < content.index("调研B") < content.index("调研C")
    assert "结论：调研A" in content and "结论：调研C" in content


def test_worker_failure_isolated(monkeypatch):
    """单个 worker 失败记为条目，不拖垮整批，其余照常。"""

    async def flaky_runner(task: str, workspace: str) -> str:
        if task.startswith("bad"):
            raise RuntimeError("worker 崩了")
        return f"结论:{task}"

    monkeypatch.setattr(dispatch_mod, "worker_runner", flaky_runner)
    results = asyncio.run(dispatch_mod.run_subtask_batch(["good1", "bad2", "good3"], "WS"))
    assert [r["ok"] for r in results] == [True, False, True]
    assert "崩了" in results[1]["content"]
    assert results[0]["content"] == "结论:good1"


@pytest.mark.parametrize("empty", [[], ["", "   "]])
def test_empty_sub_tasks_rejected(empty):
    """空列表 = 误用（不是"派 0 个"）：回 invalid_argument，别让模型以为查过了。"""
    result = asyncio.run(dispatch_mod.dispatch_subtasks(empty))
    assert isinstance(result, ToolResult) and not result.success
    assert result.error_type == "invalid_argument"
