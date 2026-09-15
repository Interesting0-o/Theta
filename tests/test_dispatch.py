"""主 agent 派发编排工具 dispatch_subtasks 的 hermetic 单测。

不真 spawn worker（注入假 worker_runner），验证：
- 并发：N 个子任务各调一次 runner、并行执行、结果按输入顺序汇总；
- 失败隔离：单 worker 失败不拖垮整批；
- 接线：OrchestrateNode 注入 workspace 执行 dispatch；source=dispatch 走编排层。

注：import app.agent.* 经 __init__ 触发 get_settings()，需 .env 存在（CLAUDE.md 前提）。
"""
import asyncio
import json
from pathlib import Path

from langchain_core.messages import ToolMessage

import app.agent.tools as tools_mod
from app.agent.nodes import OrchestrateNode, ReviewNode
from app.agent.tools import dispatch_subtasks, dispatch_tool, run_subtask_batch

_TOOL_JSON = Path(__file__).resolve().parent.parent / "app" / "agent" / "tool.json"


def test_source_registered():
    """tool.json + ORCHESTRATE_SOURCES 都认 dispatch → 走编排层（非 tool_node）。"""
    cfg = json.loads(_TOOL_JSON.read_text(encoding="utf-8"))
    entry = cfg.get("dispatch_subtasks")
    assert entry == {"need_review": False, "source": "dispatch"}
    assert "dispatch" in ReviewNode.ORCHESTRATE_SOURCES
    assert dispatch_tool[0].name == "dispatch_subtasks"


def test_worker_child_env_forwards_inbox_url(monkeypatch):
    """worker 子进程 env 要带上主侧审批收件箱的**实际**地址（子进程 env 是替换制、不继承）。

    否则主侧一换端口（AGENT_INBOX_PORT），worker 还在往默认端口回传审批，链路静默断掉。
    """
    monkeypatch.setenv("AGENT_INBOX_URL", "http://127.0.0.1:25011")
    env = tools_mod._worker_child_env("/tmp/ws")
    assert env["AGENT_INBOX_URL"] == "http://127.0.0.1:25011"
    assert env["WORKSPACE_PATH"] == "/tmp/ws" and "PYTHONPATH" in env

    # 主侧没给地址时**不带该键**（带个空串会被 worker 当成地址用，而不是回退到默认）
    monkeypatch.delenv("AGENT_INBOX_URL", raising=False)
    assert "AGENT_INBOX_URL" not in tools_mod._worker_child_env("/tmp/ws")

    monkeypatch.setenv("AGENT_INBOX_URL", "   ")
    assert "AGENT_INBOX_URL" not in tools_mod._worker_child_env("/tmp/ws")


def test_dispatch_subtasks_concurrent_summary(monkeypatch):
    """每个子任务调一次 worker runner、并发执行、结果按输入顺序汇总成一条 ToolMessage。"""
    calls: list[tuple] = []
    counter = {"active": 0, "max": 0}

    async def fake_runner(task: str, workspace: str) -> str:
        calls.append((task, workspace))
        counter["active"] += 1
        counter["max"] = max(counter["max"], counter["active"])
        await asyncio.sleep(0.02)
        counter["active"] -= 1
        return f"结论：{task}"

    monkeypatch.setattr(tools_mod, "worker_runner", fake_runner)

    async def go():
        return await dispatch_subtasks.coroutine(
            sub_tasks=["调研A", "调研B", "调研C"],
            workspace="WS",
            tool_call_id="c1",
        )

    result = asyncio.run(go())
    assert counter["max"] >= 3  # gather 真并发，而非串行
    assert set(calls) == {("调研A", "WS"), ("调研B", "WS"), ("调研C", "WS")}

    messages = result["messages"]
    assert len(messages) == 1 and isinstance(messages[0], ToolMessage)
    content = messages[0].content
    # 顺序保持输入序；每条子任务都有结论
    assert content.index("调研A") < content.index("调研B") < content.index("调研C")
    assert "结论：调研A" in content and "结论：调研C" in content


def test_worker_failure_isolated(monkeypatch):
    """单个 worker 失败记为条目，不拖垮整批，其余照常。"""

    async def flaky_runner(task: str, workspace: str) -> str:
        if task.startswith("bad"):
            raise RuntimeError("worker 崩了")
        return f"结论:{task}"

    monkeypatch.setattr(tools_mod, "worker_runner", flaky_runner)
    results = asyncio.run(run_subtask_batch(["good1", "bad2", "good3"], "WS"))
    assert [r["ok"] for r in results] == [True, False, True]
    assert "崩了" in results[1]["content"]
    assert results[0]["content"] == "结论:good1"


def test_orchestrate_node_injects_workspace(monkeypatch):
    """OrchestrateNode 拿到 workspace 并注入 dispatch（InjectedWorkspace），执行后清空队列。"""
    seen: list[str] = []

    async def fake_runner(task: str, workspace: str) -> str:
        seen.append(workspace)
        return f"结论:{task}"

    monkeypatch.setattr(tools_mod, "worker_runner", fake_runner)

    node = OrchestrateNode([dispatch_subtasks], workspace_path="WS_NODE")
    state = {
        "session_id": "s",
        "messages": [],
        "approved_orchestrate_calls": [
            {"name": "dispatch_subtasks", "args": {"sub_tasks": ["查X"]}, "id": "cc1"}
        ],
        "current_plan": [],
    }

    async def go():
        return await node(dict(state))

    out = asyncio.run(go())
    assert seen == ["WS_NODE"]  # workspace 由 node 注入，不是模型给的
    assert out["approved_orchestrate_calls"] == []
    assert len(out["messages"]) == 1
    assert "结论:查X" in out["messages"][0].content
