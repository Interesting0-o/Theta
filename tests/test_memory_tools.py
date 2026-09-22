"""长期记忆工具 write_memory / read_memory 的单测（不经过 FastMCP / 模型）。

工具核心逻辑在 app/agent/tools.py：按 tests/test_dispatch.py 的方式用 `.coroutine` 直调
（跳过 schema / 注入管线），同样按它的方式驱动 OrchestrateNode，验证 workspace 由节点注入、
模型给的路径不算数。

记忆落盘一律 monkeypatch app.resource.paths.RESOURCE_ROOT 到 tmp，绝不碰真实 resource 目录。
import app.agent.* 经 __init__ 触发 get_settings()，需 .env 存在（CLAUDE.md 前提）。
"""
import asyncio
import json
from pathlib import Path

from langchain_core.messages import ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

import app.resource.memory as memory
import app.resource.paths as resource
from app.agent.nodes import OrchestrateNode, ReviewNode
from app.agent.tools import memory_tool, read_memory, write_memory

_TOOL_JSON = Path(__file__).resolve().parent.parent / "app" / "agent" / "tool.json"


def _workspace(tmp_path, monkeypatch):
    """把 resource 根指到 tmp，返回一个已播种模板的工作区路径。"""
    monkeypatch.setattr(resource, "RESOURCE_ROOT", tmp_path / "res")
    ws = str(tmp_path / "ws")
    memory.ensure_memory_template(ws)
    return ws


def _msg(out):
    return out["messages"][0].content


def _write(ws, type_, content, key=""):
    """直调 write_memory 的底层协程（type, content, workspace, tool_call_id, key）。

    两个记忆工具是 async（落盘走 asyncio.to_thread 以避开 blockbuster），故取 `.coroutine`
    ——与 tests/test_dispatch.py 的 async 工具用法一致。
    """
    return asyncio.run(write_memory.coroutine(type_, content, ws, "c1", key))


def _read(ws, key=""):
    """直调 read_memory 的底层协程（workspace, tool_call_id, key）。"""
    return asyncio.run(read_memory.coroutine(ws, "c1", key))


# ------------------------- 写入 -------------------------


def test_write_appends_and_reports_count(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)

    out = _write(ws, "decision", "子 agent 传输定案：stdio")

    assert isinstance(out["messages"][0], ToolMessage)
    assert "已写入 [m1]" in _msg(out)
    assert "decision" in _msg(out)
    assert "共 1 条" in _msg(out)
    entries = memory.read_entries(ws)
    assert [(e.key, e.type, e.content) for e in entries] == [
        ("m1", "decision", "子 agent 传输定案：stdio")
    ]


def test_write_twice_assigns_next_key(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)

    _write(ws, "user-preference", "输出用中文")
    out = _write(ws, "convention", "测试用 python -m pytest")

    assert "已写入 [m2]" in _msg(out)
    assert "共 2 条" in _msg(out)
    assert [e.key for e in memory.read_entries(ws)] == ["m1", "m2"]


def test_write_with_key_overwrites_entry(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)
    _write(ws, "decision", "旧结论")
    _write(ws, "decision", "第二条")
    first_date = memory.read_entries(ws)[0].date

    out = _write(ws, "user-preference", "新结论（已修正）", key="m1")

    assert "已覆写 [m1]" in _msg(out)
    assert "共 2 条" in _msg(out)
    entries = memory.read_entries(ws)
    assert (entries[0].key, entries[0].type, entries[0].content) == (
        "m1",
        "user-preference",
        "新结论（已修正）",
    )
    assert entries[0].date == first_date  # 覆写保留首次记入日期
    assert entries[1].content == "第二条"  # 其余条目不受影响


def test_write_unknown_key_reports_and_keeps_file(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)
    _write(ws, "decision", "唯一一条")
    before = memory.read_text(ws)

    out = _write(ws, "decision", "不该写进去", key="m9")

    assert "记忆 m9 不存在" in _msg(out)
    assert "m1" in _msg(out)  # 回执里列出现有编号，便于模型改用正确 key
    assert memory.read_text(ws) == before


def test_write_rejects_empty_content_or_type(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)

    assert "内容为空" in _msg(_write(ws, "decision", "   "))
    assert "类别为空" in _msg(_write(ws, "  ", "有正文"))

    assert memory.read_entries(ws) == []  # 空参数一条都不写


# ------------------------- 读取 -------------------------


def test_read_full_and_single(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)
    _write(ws, "decision", "第一条结论")
    _write(ws, "convention", "第二条约定")

    full = _msg(_read(ws))
    assert "共 2 条" in full
    assert "第一条结论" in full and "第二条约定" in full
    assert "<!--" not in full  # 给模型看的是剥掉注释的可见文本

    single = _msg(_read(ws, "m2"))
    assert "### [m2] convention" in single
    assert "第二条约定" in single
    assert "第一条结论" not in single


def test_read_unknown_key_lists_existing(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)
    _write(ws, "decision", "唯一一条")

    out = _msg(_read(ws, "m9"))

    assert "记忆 m9 不存在" in out
    assert "m1" in out


def test_read_missing_file_reports_and_does_not_create_it(tmp_path, monkeypatch):
    """读取是只读：工作区没播过种也不该因为 read_memory 而凭空建文件。"""
    monkeypatch.setattr(resource, "RESOURCE_ROOT", tmp_path / "res")
    ws = str(tmp_path / "never_seeded")

    out = _msg(_read(ws))

    assert "还没有长期记忆" in out
    assert not memory.memory_path(ws).exists()


def test_read_empty_template_reports_no_entries(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)  # 只有模板、没有真实条目

    assert "还没有长期记忆条目" in _msg(_read(ws))


# ------------------------- 接线（tool.json / source / 注入） -------------------------


def test_source_registered():
    """tool.json + ORCHESTRATE_SOURCES 都认 memory → 走编排层（非 tool_node）。"""
    cfg = json.loads(_TOOL_JSON.read_text(encoding="utf-8"))
    assert cfg.get("write_memory") == {"need_review": False, "source": "memory"}
    assert cfg.get("read_memory") == {"need_review": False, "source": "memory"}
    assert "memory" in ReviewNode.ORCHESTRATE_SOURCES
    assert sorted(t.name for t in memory_tool) == ["read_memory", "write_memory"]


def test_model_visible_schema_hides_injected_args():
    """模型只该看到 type/content/key；workspace 与 tool_call_id 是注入参数，不进 schema。"""
    params = convert_to_openai_tool(write_memory)["function"]["parameters"]
    assert sorted(params["properties"]) == ["content", "key", "type"]
    assert params["required"] == ["type", "content"]  # key 可省略 = 追加


def test_orchestrate_node_injects_workspace(tmp_path, monkeypatch):
    """OrchestrateNode 把工作区注入 write_memory；模型（伪造）给的 workspace 被覆盖。"""
    ws = _workspace(tmp_path, monkeypatch)
    node = OrchestrateNode(list(memory_tool), workspace_path=ws)
    state = {
        "session_id": "s",
        "messages": [],
        "approved_orchestrate_calls": [
            {
                "name": "write_memory",
                "args": {
                    "type": "decision",
                    "content": "经节点写入的结论",
                    "workspace": str(tmp_path / "evil"),  # 模型不可见注入参数，若被采信就写错地方
                },
                "id": "cc1",
            }
        ],
        "current_plan": [],
    }

    async def go():
        return await node(dict(state))

    out = asyncio.run(go())

    assert out["approved_orchestrate_calls"] == []
    assert "已写入 [m1]" in _msg(out)
    assert [e.content for e in memory.read_entries(ws)] == ["经节点写入的结论"]
    assert not (tmp_path / "evil").exists()
