"""技能工具（`get_skill` / `drop_skill`）：审批登记、路由、行为，以及 OrchestrateNode 的切片合并。

技能源用 monkeypatch `skills.SKILLS_DIR` 指到 tmp，不碰仓库里真实的 `skills/`。
import app.agent.* 经 __init__ 触发 get_settings()，需 .env 存在（CLAUDE.md 前提）。
"""
import asyncio
import json
from pathlib import Path

import pytest

from app.resource import skills
from app.agent.nodes import OrchestrateNode, ReviewNode
from app.agent.tools import drop_skill, get_skill, orchestrate_tool, skill_tool, skill_tools_for

_TOOL_JSON = Path(__file__).resolve().parent.parent / "app" / "agent" / "tool.json"


def _make_skill(root: Path, name: str, *, description: str = "干某件事的技能", body: str = "正文") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.fixture
def skills_dir(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(skills, "SKILLS_DIR", root)
    return root


# 技能工具现在都注入 workspace（能力型的 server 在工作区里运行，见 SKILL_DESIGN §13）
_WORKSPACE = "/tmp/does-not-need-to-exist"


def _get_skill(name: str, loaded: list[str] | None = None):
    return asyncio.run(
        get_skill.coroutine(
            name=name,
            state={"loaded_skills": loaded or []},
            tool_call_id="c1",
            workspace=_WORKSPACE,
        )
    )


def _drop_skill(name: str, loaded: list[str] | None = None):
    """drop_skill 是 async（要 await 关技能运行体）。"""
    return asyncio.run(
        drop_skill.coroutine(
            name=name,
            state={"loaded_skills": loaded or []},
            tool_call_id="c1",
            workspace=_WORKSPACE,
        )
    )


# ---------------------- 登记与路由 ----------------------


def test_source_registered():
    """照 tests/test_dispatch.py::test_source_registered 的写法：登记 + 路由层一起断言。"""
    cfg = json.loads(_TOOL_JSON.read_text(encoding="utf-8"))

    for name in ("get_skill", "drop_skill"):
        assert cfg[name]["source"] == "skill"
        assert cfg[name]["need_review"] is False, "加载技能自己不动手，动手的是它带来的工具"

    assert "skill" in ReviewNode.ORCHESTRATE_SOURCES
    assert {t.name for t in skill_tool} == {"get_skill", "drop_skill"}


def test_skill_tools_for_bakes_catalog_without_mutating_module_tool(skills_dir):
    """目录烤进副本；模块级的 get_skill 保持干净——否则会污染同进程里的其它图。"""
    _make_skill(skills_dir, "github", description="GitHub 平台操作")

    tools = skill_tools_for()

    assert "- github: GitHub 平台操作" in tools[0].description
    assert tools[0] is not get_skill
    assert tools[0].name == "get_skill"
    assert "- github:" not in get_skill.description


def test_skill_tools_for_without_skills_keeps_module_tools(skills_dir):
    assert skill_tools_for()[0] is get_skill


def test_skill_tools_inject_workspace():
    """两工具都注入 InjectedWorkspace（能力型技能要按工作区造 server 连接：cwd + WORKSPACE_PATH）。

    ⚠️ 这条契约在能力型二期改过一次：一期"技能源在项目根、不需要工作区"只对**知识型**成立。
    模型仍然看不到它（InjectedWorkspace 会从可见 schema 里剔除）。
    """
    from inspect import signature

    for tool_obj in skill_tool:
        fn = getattr(tool_obj, "coroutine", None) or tool_obj.func
        assert "workspace" in signature(fn).parameters


# ---------------------- get_skill ----------------------


def test_get_skill_loads_and_receipt_carries_no_body(skills_dir):
    """**铁律**（§3.5）：回执只回话、不带正文——正文只有一个家（系统提示）。

    否则正文既进 history 又进系统提示，就是两份拷贝，而淘汰只能抹掉注入那份。
    """
    _make_skill(skills_dir, "github", body="推送前先开分支")

    out = _get_skill("github")

    assert out["loaded_skills"] == ["github"]
    receipt = out["messages"][0].content
    assert "已加载技能 github" in receipt
    assert "推送前先开分支" not in receipt  # 正文没泄进回执
    assert len(receipt) < 300  # 回执就该是一句话，不是一份文档


def test_get_skill_unknown_lists_available(skills_dir):
    _make_skill(skills_dir, "github")

    out = _get_skill("nope")

    assert list(out) == ["messages"]  # 不写 state 切片
    content = out["messages"][0].content
    assert "不存在" in content and "github" in content


def test_get_skill_idempotent(skills_dir):
    _make_skill(skills_dir, "github")

    out = _get_skill("github", loaded=["github"])

    assert "已在本会话加载中" in out["messages"][0].content
    assert list(out) == ["messages"]  # 没有变更就不回切片


def test_get_skill_refuses_over_budget_and_lists_loaded(skills_dir, monkeypatch):
    """预算满 → **拒绝**并要模型先卸一个（§3.5）：不静默淘汰。"""
    _make_skill(skills_dir, "github", body="x" * 500)
    _make_skill(skills_dir, "other", body="y" * 500)
    monkeypatch.setattr(skills, "SKILL_BODY_BUDGET", 600)

    out = _get_skill("other", loaded=["github"])

    assert list(out) == ["messages"]  # 被拒 → 不改 state
    content = out["messages"][0].content
    assert "预算已满" in content
    assert "当前已加载：github" in content  # 给模型判断依据
    assert "drop_skill" in content  # 响亮且可行动的要求


# ---------------------- drop_skill ----------------------


def test_drop_skill_removes_and_returns_slice():
    out = _drop_skill("github", ["github", "x"])

    assert out["loaded_skills"] == ["x"]
    assert "已卸下技能 github" in out["messages"][0].content


def test_drop_skill_not_loaded():
    out = _drop_skill("ghost", ["github"])

    assert list(out) == ["messages"]
    assert "不在已加载清单" in out["messages"][0].content


# ---------------------- OrchestrateNode 的切片合并（2026-09-12 泛化） ----------------------


def _orch_state(calls, loaded=None):
    return {
        "messages": [],
        "approved_orchestrate_calls": calls,
        "current_plan": [],
        "loaded_skills": loaded if loaded is not None else [],
    }


def test_orchestrate_node_merges_loaded_skills_slice(skills_dir):
    _make_skill(skills_dir, "github")
    node = OrchestrateNode(skill_tool, workspace_path=_WORKSPACE)

    out = asyncio.run(
        node(_orch_state([{"name": "get_skill", "args": {"name": "github"}, "id": "c1"}]))
    )

    assert out["loaded_skills"] == ["github"]
    assert out["approved_orchestrate_calls"] == []
    assert out["current_plan"] == []  # 既有行为不变：current_plan 照旧恒带回


def test_orchestrate_node_chains_loaded_skills_within_turn(skills_dir):
    """同回合多次编排调用链式传递：后一条看得到前一条写的 loaded_skills。"""
    _make_skill(skills_dir, "github")
    node = OrchestrateNode(skill_tool, workspace_path=_WORKSPACE)

    out = asyncio.run(
        node(
            _orch_state(
                [
                    {"name": "get_skill", "args": {"name": "github"}, "id": "c1"},
                    {"name": "drop_skill", "args": {"name": "github"}, "id": "c2"},
                ]
            )
        )
    )

    assert out["loaded_skills"] == []  # drop 看得到 get 的结果
    assert len(out["messages"]) == 2


def test_orchestrate_node_omits_slice_when_untouched():
    """计划工具不碰 loaded_skills → 输出里不该凭空多出这个键。"""
    node = OrchestrateNode(orchestrate_tool)

    out = asyncio.run(
        node(_orch_state([{"name": "create_plan", "args": {"steps": ["做 A"]}, "id": "c1"}]))
    )

    assert "loaded_skills" not in out
    assert len(out["current_plan"]) == 1
