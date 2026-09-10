"""LLMNode 的长期记忆注入（docs/LONG_TERM_MEMORY.md §4）。

- 主 agent：工作区有记忆条目 → 每轮在系统消息**末尾**追加一条 `# 长期记忆…`，
  且不污染 state["messages"]（系统消息不渗入历史）；
- worker：`inject_memory=False` → 不注入（临时资料收集器，只读隔离）。

落盘一律 monkeypatch app.resource.RESOURCE_ROOT 到 tmp，不碰真实 resource 目录。
import app.agent.* 经 __init__ 触发 get_settings()，需 .env 存在（CLAUDE.md 前提）。
"""
import asyncio

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import app.agent.graph as graph_mod
import app.agent.memory as memory
import app.resource as resource
from app.agent.nodes import LLMNode


class _CaptureModel:
    """假模型：记下收到的消息列表，直接回一条 AIMessage（不调工具）。"""

    def __init__(self):
        self.seen: list = []
        self.reply = AIMessage(content="ok")

    async def ainvoke(self, messages):
        self.seen = list(messages)
        return self.reply


def _seed(tmp_path, monkeypatch, *, with_entry: bool = True):
    """把 resource 根指到 tmp，返回 (工作区路径, 已播种模板)。"""
    monkeypatch.setattr(resource, "RESOURCE_ROOT", tmp_path / "res")
    ws = str(tmp_path / "ws")
    memory.ensure_memory_template(ws)
    if with_entry:
        memory.append_entry(ws, "decision", "子 agent 传输定案 stdio", "2026-09-10")
    return ws


def _system_contents(model) -> list[str]:
    return [m.content for m in model.seen if isinstance(m, SystemMessage)]


def test_main_llm_node_injects_memory_last(tmp_path, monkeypatch):
    ws = _seed(tmp_path, monkeypatch)
    model = _CaptureModel()
    node = LLMNode(model=model, workspace_path=ws)
    state = {"messages": [HumanMessage(content="接着干")]}

    out = asyncio.run(node(state))

    assert out["messages"] == [model.reply]  # 只把模型回复写回 state
    assert [type(m) for m in model.seen].count(SystemMessage) >= 3  # 契约 + 工作区 + 记忆
    assert isinstance(model.seen[-1], HumanMessage)  # 系统消息只在头部，用户话在最后
    contents = _system_contents(model)
    # 记忆块带内容、且拼在系统消息最后（§4 的拼装顺序）
    assert contents[-1].startswith("# 长期记忆") and "传输定案 stdio" in contents[-1]
    assert len(state["messages"]) == 1  # state 未被注入的系统消息污染


def test_llm_node_skips_memory_when_disabled(tmp_path, monkeypatch):
    """worker 走 inject_memory=False：即使工作区有记忆，也不注入。"""
    ws = _seed(tmp_path, monkeypatch)
    model = _CaptureModel()
    node = LLMNode(model=model, workspace_path=ws, inject_memory=False)

    asyncio.run(node({"messages": [HumanMessage(content="hi")]}))

    assert not any(c.startswith("# 长期记忆") for c in _system_contents(model))


def test_llm_node_skips_memory_when_no_entries(tmp_path, monkeypatch):
    """只有模板、没有条目 → 不追加空的记忆块。"""
    ws = _seed(tmp_path, monkeypatch, with_entry=False)
    model = _CaptureModel()

    asyncio.run(LLMNode(model=model, workspace_path=ws)({"messages": [HumanMessage(content="hi")]}))

    assert not any(c.startswith("# 长期记忆") for c in _system_contents(model))


def test_worker_graph_builds_llm_node_without_memory(monkeypatch):
    """worker 子图（get_sub_agent_graph）确实把 inject_memory 关掉，且用的是 worker 提示词。"""
    captured: dict = {}
    real_llm_node = graph_mod.LLMNode

    class _Spy(real_llm_node):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(graph_mod, "LLMNode", _Spy)
    graph_mod.get_sub_agent_graph(read_tools=[], workspace_path="/tmp", model=_CaptureModel())

    assert captured["inject_memory"] is False
    assert captured["system_prompt"].startswith("你是主 agent（Theta）派出来")
