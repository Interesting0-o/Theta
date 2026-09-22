"""LLMNode 的**会话级上下文**注入：项目画像（AGENT.md）+ 长期记忆（docs/LONG_TERM_MEMORY.md §4）。

- 主 agent：工作区有画像/记忆 → 系统消息尾部按序追加 `# 项目画像…` → `# 长期记忆…`，
  且不污染 state["messages"]（系统消息不渗入历史）；
- 画像**会话首启读入**（实例内 memo，不每轮重读）；记忆每轮实时读盘；
- worker：`inject_session_context=False` → 两样都不注入（临时资料收集器，只读隔离）。

落盘一律 monkeypatch app.resource.paths.RESOURCE_ROOT 到 tmp，不碰真实 resource 目录。
import app.agent.* 经 __init__ 触发 get_settings()，需 .env 存在（CLAUDE.md 前提）。
"""
import asyncio
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import app.agent.graph as graph_mod
import app.resource.memory as memory
import app.resource.profile as profile_mod
import app.resource.skills as skills_mod
import app.resource.paths as resource
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


def _write_profile(ws: str, text: str) -> None:
    """在工作区根写下 AGENT.md（工作区目录本身此前通常并不存在）。"""
    path = Path(ws) / "AGENT.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_skill(skills_root: Path, name: str, body: str) -> None:
    """在技能源里写一个知识型技能。"""
    path = skills_root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: 测试技能\n---\n\n{body}\n", encoding="utf-8"
    )


@pytest.fixture
def skills_dir(tmp_path, monkeypatch) -> Path:
    """技能源指到 tmp（不扫仓库里真实的 skills/）。"""
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", root)
    return root


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


def test_llm_node_skips_session_context_when_disabled(tmp_path, monkeypatch):
    """worker 走 inject_session_context=False：工作区即使有画像与记忆，两样都不注入。"""
    ws = _seed(tmp_path, monkeypatch)
    _write_profile(ws, "# 项目\n\n画像正文")
    model = _CaptureModel()
    node = LLMNode(model=model, workspace_path=ws, inject_session_context=False)

    asyncio.run(node({"messages": [HumanMessage(content="hi")]}))

    contents = _system_contents(model)
    assert not any(c.startswith("# 长期记忆") for c in contents)
    assert not any(c.startswith("# 项目画像") for c in contents)


def test_llm_node_skips_memory_when_no_entries(tmp_path, monkeypatch):
    """只有模板、没有条目 → 不追加空的记忆块。"""
    ws = _seed(tmp_path, monkeypatch, with_entry=False)
    model = _CaptureModel()

    asyncio.run(LLMNode(model=model, workspace_path=ws)({"messages": [HumanMessage(content="hi")]}))

    assert not any(c.startswith("# 长期记忆") for c in _system_contents(model))


def test_main_llm_node_injects_profile_before_memory(tmp_path, monkeypatch):
    """项目画像拼在记忆**之前**（§4 顺序：… → 计划 → 项目画像 → 长期记忆）。"""
    ws = _seed(tmp_path, monkeypatch)
    _write_profile(ws, "# Theta\n\nLangGraph 编码助手，工作区沙箱。")
    model = _CaptureModel()

    asyncio.run(LLMNode(model=model, workspace_path=ws)({"messages": [HumanMessage(content="hi")]}))

    contents = _system_contents(model)
    assert contents[-2].startswith("# 项目画像")  # 画像在前
    assert "LangGraph 编码助手" in contents[-2]
    assert contents[-1].startswith("# 长期记忆")  # 记忆在后


def test_profile_read_once_per_node_instance(tmp_path, monkeypatch):
    """画像 = 会话首启读入（实例内 memo）：同一 LLMNode 多轮只读一次盘。"""
    ws = _seed(tmp_path, monkeypatch)
    calls: list[str] = []
    real_agent_md_block = profile_mod.agent_md_block

    def counting(workspace, *args, **kwargs):
        calls.append(workspace)
        return real_agent_md_block(workspace, *args, **kwargs)

    # 打桩在资源层模块上（2026-09-21 起画像读盘在 app/resource/profile.py::agent_md_block，
    # LLMNode._read_profile 只做实例内 memo、经模块属性查表调用）。
    monkeypatch.setattr(profile_mod, "agent_md_block", counting)
    node = LLMNode(model=_CaptureModel(), workspace_path=ws)

    asyncio.run(node({"messages": [HumanMessage(content="第一轮")]}))
    asyncio.run(node({"messages": [HumanMessage(content="第二轮")]}))

    assert calls == [ws]  # 只读了一次


def test_worker_graph_builds_llm_node_without_context(monkeypatch):
    """worker 子图（get_sub_agent_graph）确实把 inject_session_context 关掉，且用的是 worker 提示词。"""
    captured: dict = {}
    real_llm_node = graph_mod.LLMNode

    class _Spy(real_llm_node):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(graph_mod, "LLMNode", _Spy)
    graph_mod.get_sub_agent_graph(read_tools=[], workspace_path="/tmp", model=_CaptureModel())

    assert captured["inject_session_context"] is False
    assert captured["system_prompt"].startswith("你是主 agent（Theta）派出来")


# ---------------------- 技能正文注入（docs/SKILL_DESIGN.md §3.5） ----------------------


def test_main_llm_node_injects_loaded_skills_last(tmp_path, monkeypatch, skills_dir):
    """已加载技能的正文进系统提示，拼在画像/记忆**之后**，抬头列出已加载清单。"""
    ws = _seed(tmp_path, monkeypatch)
    _write_skill(skills_dir, "demo", "纪律正文：推送前先开分支")
    model = _CaptureModel()

    asyncio.run(
        LLMNode(model=model, workspace_path=ws)(
            {"messages": [HumanMessage(content="hi")], "loaded_skills": ["demo"]}
        )
    )

    contents = _system_contents(model)
    assert contents[-1].startswith("# 已加载技能")  # 拼在最后
    assert "推送前先开分支" in contents[-1]
    assert "当前已加载：demo" in contents[-1]  # 抬头——模型据此决定卸哪个（§3.5）


def test_llm_node_skips_skills_when_none_loaded(tmp_path, monkeypatch, skills_dir):
    """没加载任何技能 → 不追加空块（否则每轮白塞一条系统消息）。"""
    ws = _seed(tmp_path, monkeypatch)
    _write_skill(skills_dir, "demo", "正文")
    model = _CaptureModel()

    asyncio.run(LLMNode(model=model, workspace_path=ws)({"messages": [HumanMessage(content="hi")]}))

    assert not any(c.startswith("# 已加载技能") for c in _system_contents(model))


def test_skill_block_is_independent_of_session_context(tmp_path, monkeypatch, skills_dir):
    """技能块**不受** inject_session_context 影响（2026-09-12 的决定）。

    它不是"这个项目/用户是谁"那类会话背景，而是模型自己按需取来的领域纪律——所以画像/记忆
    关掉时技能照旧注入。（worker 实际不会触发：它没有 get_skill，loaded_skills 天然为空。）
    """
    ws = _seed(tmp_path, monkeypatch)
    _write_profile(ws, "# 项目\n\n画像正文")
    _write_skill(skills_dir, "demo", "纪律正文")
    model = _CaptureModel()

    asyncio.run(
        LLMNode(model=model, workspace_path=ws, inject_session_context=False)(
            {"messages": [HumanMessage(content="hi")], "loaded_skills": ["demo"]}
        )
    )

    contents = _system_contents(model)
    assert not any(c.startswith("# 项目画像") for c in contents)  # 画像照旧不注入
    assert any(c.startswith("# 已加载技能") for c in contents)  # 技能照旧注入
