"""确定性 event log + 三个写入点（ToolNode / ReviewNode / OrchestrateNode）+ 查询工具。

落盘一律 monkeypatch `app.resource.paths.RESOURCE_ROOT` 到 tmp，不碰真实 resource 目录。
"""
import asyncio
import json

import pytest
from langchain_core.messages import ToolMessage

import app.agent.nodes as nodes_module
import app.resource.paths as resource
from app.agent.nodes import OrchestrateNode, ReviewNode, ToolNode
from app.agent.tools import create_plan, read_event_log
from app.resource import event_log
from app.schema.agent_schema import ToolResult


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(resource, "RESOURCE_ROOT", tmp_path / "res")
    return str(tmp_path / "ws")


def _append(ws, **overrides):
    kwargs = dict(
        tool="read_file",
        tool_call_id="c1",
        args={"path": "src/app.py"},
        status="ok",
        approval="not_required",
        content_chars=123,
    )
    kwargs.update(overrides)
    return event_log.append_event(ws, "s1", **kwargs)


# ------------------------- writer / reader -------------------------


def test_append_writes_one_jsonl_line(ws):
    record = _append(ws)

    path = resource.session_event_log_path(ws, "s1")
    assert path.is_file()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed == record
    assert parsed["tool"] == "read_file"
    assert parsed["target"] == "src/app.py"
    assert parsed["args_hash"].startswith("sha256:")
    assert parsed["status"] == "ok"
    assert parsed["approval"] == "not_required"
    assert parsed["content_chars"] == 123


def test_command_is_stored_as_hash_not_plaintext(ws):
    _append(ws, tool="run_command", args={"command": "curl -H 'token: secret' example.com"})

    line = resource.session_event_log_path(ws, "s1").read_text(encoding="utf-8")
    assert "secret" not in line
    record = json.loads(line)
    assert record["target"].startswith("command:")
    assert record["args_hash"].startswith("sha256:")


def test_missing_session_is_noop(ws):
    assert event_log.append_event(ws, None, tool="read_file", tool_call_id="c", args={}, status="ok") is None
    assert event_log.read_events(ws, None) == []


def test_read_events_limit_and_filter(ws):
    _append(ws, tool="read_file", tool_call_id="c1", args={"path": "a.py"})
    _append(ws, tool="run_command", tool_call_id="c2", args={"command": "pytest"})
    _append(ws, tool="read_file", tool_call_id="c3", args={"path": "b.py"})

    assert [e["tool_call_id"] for e in event_log.read_events(ws, "s1", limit=2)] == ["c2", "c3"]
    assert [e["tool_call_id"] for e in event_log.read_events(ws, "s1", tool="read_file")] == [
        "c1",
        "c3",
    ]
    assert len(event_log.read_events(ws, "s1", limit=None)) == 3


def test_bad_line_is_skipped(ws):
    _append(ws)
    path = resource.session_event_log_path(ws, "s1")
    with path.open("a", encoding="utf-8") as file:
        file.write("{not json}\n")
    _append(ws, tool_call_id="c2")

    assert [e["tool_call_id"] for e in event_log.read_events(ws, "s1")] == ["c1", "c2"]


# ------------------------- ToolNode 写入 -------------------------


class _FakeTool:
    def __init__(self, result):
        self.result = result

    async def ainvoke(self, args):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _FakeToolset:
    def __init__(self, ws, tool):
        self.workspace = ws
        self.tool = tool

    async def tools_map(self, state):
        return {"read_file": self.tool}

    def known_names(self, state):
        return {"read_file"}


def _tool_state(**overrides):
    state = {
        "session_id": "s1",
        "approved_tool_calls": [
            {
                "name": "read_file",
                "args": {"path": "a.py"},
                "id": "c1",
                "_approval": "not_required",
            }
        ],
    }
    state.update(overrides)
    return state


def test_tool_node_logs_success(ws):
    node = ToolNode(toolset=_FakeToolset(ws, _FakeTool(ToolResult(success=True, content="hello"))))
    asyncio.run(node(_tool_state()))

    [record] = event_log.read_events(ws, "s1")
    assert record["tool"] == "read_file"
    assert record["status"] == "ok"
    assert record["approval"] == "not_required"
    assert record["target"] == "a.py"
    assert record["content_chars"] == len("hello")


def test_tool_node_logs_failure(ws):
    node = ToolNode(
        toolset=_FakeToolset(
            ws, _FakeTool(ToolResult(success=False, content="boom", error_type="io_error"))
        )
    )
    asyncio.run(node(_tool_state()))

    [record] = event_log.read_events(ws, "s1")
    assert record["status"] == "failed"
    assert record["error_type"] == "io_error"


def test_tool_node_logs_exception(ws):
    node = ToolNode(toolset=_FakeToolset(ws, _FakeTool(RuntimeError("boom"))))
    asyncio.run(node(_tool_state()))

    [record] = event_log.read_events(ws, "s1")
    assert record["status"] == "failed"
    assert record["error_type"] == "tool_error"


# ------------------------- ReviewNode 写入（denied） -------------------------


def test_review_node_logs_denied(ws, monkeypatch):
    class _Toolset:
        workspace = ws

        def known_names(self, state):
            return {"write_file"}

    monkeypatch.setattr(
        "app.agent.nodes.review.interrupt", lambda payload: {"approved": False}
    )
    node = ReviewNode(toolset=_Toolset(), log=False)
    state = {
        "session_id": "s1",
        "pending_tool_calls": [
            {"name": "write_file", "args": {"path": "a.py", "content": "x"}, "id": "c1"}
        ],
        "approved_tool_calls": [],
        "approved_orchestrate_calls": [],
    }

    out = asyncio.run(node(state))

    assert isinstance(out["messages"][0], ToolMessage)
    assert "[approval_denied]" in out["messages"][0].content
    [record] = event_log.read_events(ws, "s1")
    assert record["status"] == "denied"
    assert record["approval"] == "denied"
    assert record["target"] == "a.py"


# ------------------------- OrchestrateNode 写入 -------------------------


def test_orchestrate_node_logs_event(ws):
    node = OrchestrateNode([create_plan], workspace_path=ws)
    state = {
        "session_id": "s1",
        "current_plan": [],
        "approved_orchestrate_calls": [
            {
                "name": "create_plan",
                "args": {"steps": ["读 A", "改 B"]},
                "id": "c1",
                "_approval": "not_required",
            }
        ],
    }

    asyncio.run(node(state))

    [record] = event_log.read_events(ws, "s1")
    assert record["tool"] == "create_plan"
    assert record["status"] == "ok"
    assert record["approval"] == "not_required"
    assert record["target"] == "plan"
    assert record["content_chars"] > 0


# ------------------------- read_event_log 工具 -------------------------


def test_read_event_log_tool_renders_recent(ws):
    _append(ws, tool="read_file", tool_call_id="c1", args={"path": "a.py"})
    _append(ws, tool="run_command", tool_call_id="c2", args={"command": "pytest"})

    out = asyncio.run(
        read_event_log.coroutine(
            {"session_id": "s1"}, "c9", ws, tool="read_file", limit=10
        )
    )
    text = out["messages"][0].content

    assert "最近 1 条工具事件" in text
    assert "tool=read_file" in text and "target=a.py" in text
    assert "run_command" not in text


def test_read_event_log_tool_without_session(ws):
    out = asyncio.run(read_event_log.coroutine({}, "c9", ws))
    assert "没有 session_id" in out["messages"][0].content


def test_read_event_log_tool_empty(ws):
    out = asyncio.run(
        read_event_log.coroutine({"session_id": "s1"}, "c9", ws, limit=10)
    )
    assert "还没有工具事件记录" in out["messages"][0].content
