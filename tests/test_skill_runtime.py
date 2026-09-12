"""能力型技能：加载 → 工具出现 → 卸载 → 工具消失，以及加载期的四道校验与对账。

**分两层测**：
- 多数用例**在进程内**跑（假 schema 列器 + 假设置），不 spawn、不联网——快且稳；
- 最后一条**真 spawn** `tests/fixtures/skills_pkg/echo/server.py`，证明"技能自带的 MCP server
  能被真拉起、工具能真调用、卸载后子进程真被收掉"这条链路是通的（其余用例都绕开了它）。

注意：import app.agent.* 会触发 app.agent/__init__ → graph → model，model 顶层调用 get_settings()，
因此运行本文件要求 .env 存在（CLAUDE.md 前提）。
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import SecretStr

import app.agent.mcp as mcp_module
import app.agent.tools as tools_module
import app.config as config
from app.agent import mcp, skills
from app.agent.nodes import LLMNode
from app.agent.tools import SessionToolset, drop_skill, get_skill
from app.schema.agent_schema import MCPToolSpec

# 读侧（session_tools / names / version）按原样查键，**不归一化**——所以测试也要给归一化后的路径
# （生产里三个调用点拿到的都是 _resolve_workspace 的结果，天然一致，见 mcp.session_tools）
_WS = str((Path(tempfile.gettempdir()) / "theta-skill-test-ws").resolve())
_SESSION = "skill-session"


# ---------------------- 夹具 ----------------------


def _make_skill(root: Path, name: str, *, capability: bool = True, env: list[str] | None = None) -> Path:
    """在临时技能源里造一个技能目录（有 server.py 即能力型）。"""
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 测试技能 {name}\n---\n\n正文", encoding="utf-8"
    )
    if capability:
        (skill_dir / "server.py").write_text("# 占位：进程内用例不会真起它\n", encoding="utf-8")
    if env is not None:
        (skill_dir / "skill.json").write_text(json.dumps({"env": env}), encoding="utf-8")
    return skill_dir


@pytest.fixture
def skills_dir(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(skills, "SKILLS_DIR", root)
    return root


def _fake_cfg(monkeypatch, mapping: dict[str, str]) -> None:
    """把 tool.json 换成 {工具名: source} 的假表（校验缝见 tools._tool_config）。"""
    monkeypatch.setattr(
        tools_module,
        "_tool_config",
        lambda: {name: {"need_review": False, "source": src} for name, src in mapping.items()},
    )


def _fake_specs(monkeypatch, mapping: dict[str, list[str]]) -> list[str]:
    """假的 per-server schema 列器：返回每个 server 该暴露哪些工具名；返回调用记录。"""
    calls: list[str] = []

    async def fake(workspace: str, server: str, connection) -> list[MCPToolSpec]:
        calls.append(server)
        return [
            MCPToolSpec(server=server, name=n, description="d", args_schema={}, metadata=None)
            for n in mapping.get(server, [])
        ]

    monkeypatch.setattr(mcp_module, "_server_specs", fake)
    return calls


def _fake_settings(monkeypatch, *, github_token: str = "tok", tavily: str = "") -> None:
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(
            TAVILY_API_KEY=SecretStr(tavily), GITHUB_TOKEN=SecretStr(github_token)
        ),
    )


def _load(name: str, loaded: list[str] | None = None):
    return asyncio.run(
        get_skill.coroutine(
            name=name,
            state={"session_id": _SESSION, "loaded_skills": loaded or []},
            tool_call_id="c1",
            workspace=_WS,
        )
    )


def _drop(name: str, loaded: list[str]):
    return asyncio.run(
        drop_skill.coroutine(
            name=name,
            state={"session_id": _SESSION, "loaded_skills": loaded},
            tool_call_id="c2",
            workspace=_WS,
        )
    )


# ---------------------- 加载 / 卸载 ----------------------


def test_load_registers_tools_and_drop_removes_them(skills_dir, monkeypatch):
    """加载 → 工具进本会话 + 版本 +1；卸载 → 工具消失 + 版本再 +1（工具随加载出现、随卸载消失）。"""
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo")
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo"})
    _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)

    out = _load("demo")

    assert out["loaded_skills"] == ["demo"]
    assert "demo_tool" in mcp.session_tool_names(_WS, _SESSION)
    assert mcp.registered_servers(_WS, _SESSION) == {"skills/demo"}
    assert mcp.tools_version(_WS, _SESSION) == 1

    out = _drop("demo", ["demo"])

    assert out["loaded_skills"] == []
    assert "demo_tool" not in mcp.session_tool_names(_WS, _SESSION)
    assert mcp.registered_servers(_WS, _SESSION) == set()
    assert mcp.tools_version(_WS, _SESSION) == 2
    assert "它的工具也已从本会话移除" in out["messages"][0].content


def test_knowledge_skill_registers_nothing(skills_dir, monkeypatch):
    """知识型（没有 server.py）只写名字——不动工具、不动版本，也不需要凭证。"""
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "notes_only", capability=False)
    _fake_settings(monkeypatch, github_token="")

    out = _load("notes_only")

    assert out["loaded_skills"] == ["notes_only"]
    assert mcp.session_tool_names(_WS, _SESSION) == set()
    assert mcp.tools_version(_WS, _SESSION) == 0


# ---------------------- 加载期的四道校验 ----------------------


def test_unregistered_tool_is_rejected_with_actionable_receipt(skills_dir, monkeypatch):
    """工具没在 tool.json 登记（或 source 不对）→ **拒绝加载**，且不留半截状态。"""
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo")
    _fake_cfg(monkeypatch, {})  # 表里什么都没有
    _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)

    out = _load("demo")

    assert list(out) == ["messages"]  # 没有 loaded_skills
    content = out["messages"][0].content
    assert "demo_tool" in content and "skills/demo" in content and "tool.json" in content
    assert mcp.registered_servers(_WS, _SESSION) == set()  # 回滚干净：没留下工具
    assert mcp.session_tool_names(_WS, _SESSION) == set()
    # 版本会前进两格（注册 +1、回滚注销再 +1）——它只是"该重绑了"的信号，不参与任何判定，
    # 多推一格换来"回滚路径不搞特例"，这是划算的；这里不复位，就是为了把这条口径钉住。


def test_missing_env_is_rejected_before_any_spawn(skills_dir, monkeypatch):
    """声明的凭证为空 → 拒绝加载、点名缺哪个键，**且从未起过运行体**。"""
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo", env=["GITHUB_TOKEN"])
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo"})
    calls = _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch, github_token="")

    out = _load("demo")

    assert list(out) == ["messages"]
    assert "GITHUB_TOKEN" in out["messages"][0].content
    assert calls == []  # 连 schema 都没列过
    assert mcp._WORKERS == {}


def test_name_must_match_dir_for_capability(skills_dir, monkeypatch):
    """能力型要求 frontmatter 的 name == 目录名（启动模块与 source 都按目录名走）。"""
    mcp._reset_pools_for_tests()
    skill_dir = _make_skill(skills_dir, "demo")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-v2\ndescription: 名字与目录不一致\n---\n\n正文", encoding="utf-8"
    )
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo"})
    _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)

    out = _load("demo-v2")

    assert list(out) == ["messages"]
    assert "目录名" in out["messages"][0].content


def test_clashing_tool_names_are_rejected(skills_dir, monkeypatch):
    """与编排工具同名、或与已加载技能的工具同名 → 拒绝（同名让审批与模型都无法区分两者）。"""
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo")
    _fake_cfg(monkeypatch, {"get_skill": "orchestrate", "demo_tool": "skills/demo"})
    _fake_specs(monkeypatch, {"skills/demo": ["get_skill"]})
    _fake_settings(monkeypatch)

    out = _load("demo")
    assert "重名" in out["messages"][0].content

    # 再试：与已加载技能的另一工具同名
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "other")
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo", "other_tool": "skills/other"})
    _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"], "skills/other": ["demo_tool"]})
    _load("demo")
    out = _load("other")

    assert "重名" in out["messages"][0].content
    assert "other" not in (out.get("loaded_skills") or [])


# ---------------------- 对账 ----------------------


def test_reconcile_rebuilds_missing_runtime_once_and_is_idempotent(skills_dir, monkeypatch):
    """state 说加载了、运行体不在（进程重启/切会话回来）→ 重建一次；再对账是 no-op。"""
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo")
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo"})
    calls = _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)

    _load("demo")
    assert len(calls) == 1

    mcp._EXTRA_TOOLS.clear()  # 模拟"运行体没了"
    assert mcp.registered_servers(_WS, _SESSION) == set()

    toolset = SessionToolset(_WS, [])
    state = {"session_id": _SESSION, "loaded_skills": ["demo"]}
    asyncio.run(toolset.tools_and_version(state))
    assert len(calls) == 2  # 重建了一次
    assert "demo_tool" in mcp.session_tool_names(_WS, _SESSION)

    asyncio.run(toolset.tools_and_version(state))
    assert len(calls) == 2  # 幂等：快乐路径纯内存，不再列 schema


def test_reconcile_unregisters_runtime_no_longer_in_state(skills_dir, monkeypatch):
    """反向对账：运行体在、state 里没有 → 注销。

    防的是"工具在、纪律不在"——注册成功但回执还没写回 state（回合被取消），模型手里会有工具
    却没读过那个领域的约束。
    """
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo")
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo"})
    _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)
    _load("demo")
    assert mcp.registered_servers(_WS, _SESSION) == {"skills/demo"}

    toolset = SessionToolset(_WS, [])
    asyncio.run(toolset.tools_and_version({"session_id": _SESSION, "loaded_skills": []}))

    assert mcp.registered_servers(_WS, _SESSION) == set()
    assert "demo_tool" not in mcp.session_tool_names(_WS, _SESSION)


def test_reconcile_failure_does_not_blow_up_the_turn(skills_dir, monkeypatch):
    """对账炸了也不能炸整轮：沿用现状 + 告警。"""
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo")
    _fake_settings(monkeypatch)

    async def boom(workspace, server, connection):
        raise RuntimeError("起不来")

    monkeypatch.setattr(mcp_module, "_server_specs", boom)
    toolset = SessionToolset(_WS, [])

    tools, version = asyncio.run(
        toolset.tools_and_version({"session_id": _SESSION, "loaded_skills": ["demo"]})
    )

    assert tools == [] and version == 0  # 没炸，返回现状


# ---------------------- 动态 bind ----------------------


class _SpyModel:
    """记 bind_tools 次数与被绑的工具名；不依赖 langchain 的绑定机制。"""

    def __init__(self) -> None:
        self.binds = 0
        self.runs = 0
        self.bound_names: list[str] | None = None

    def bind_tools(self, tools):
        self.binds += 1
        self.bound_names = [getattr(t, "name", "?") for t in tools]
        return self

    async def ainvoke(self, messages):
        self.runs += 1
        return AIMessage(content="ok")


def test_llm_node_rebinds_only_when_version_changes(monkeypatch):
    """版本没变不重绑；版本变了才重绑——且 binding 与未绑定 model 分开存（RunnableBinding
    上不能再 bind_tools，覆盖回 self.model 会在第二次版本变化时炸）。"""
    mcp._reset_pools_for_tests()
    _fake_settings(monkeypatch)
    spy = _SpyModel()
    node = LLMNode(model=spy, workspace_path=_WS, toolset=SessionToolset(_WS, []))
    state = {"session_id": _SESSION, "messages": [], "loaded_skills": []}

    asyncio.run(node(state))
    asyncio.run(node(state))
    assert spy.binds == 1  # 版本没变 → 复用 binding

    mcp._VERSIONS[(_WS, _SESSION)] = 5  # 模拟"技能加载/卸载"改了版本
    asyncio.run(node(state))
    assert spy.binds == 2


def test_llm_node_without_toolset_never_binds(monkeypatch):
    """没有 toolset（worker / 单测）→ 一次都不调 bind_tools。

    这条护住 tests/test_memory_injection.py 那批只有 ainvoke 的假模型。
    """
    _fake_settings(monkeypatch)
    spy = _SpyModel()
    node = LLMNode(model=spy, workspace_path=_WS)
    state = {"session_id": _SESSION, "messages": [], "loaded_skills": []}

    asyncio.run(node(state))

    assert spy.binds == 0 and spy.runs == 1


def test_toolset_tools_include_static_and_skill_tools(monkeypatch):
    """动态工具表 = 静态工具（编排等，模型必须一直看得到）+ MCP 工具。

    少了静态那半，连 get_skill 自己都会从模型眼前消失。
    """
    mcp._reset_pools_for_tests()
    _fake_settings(monkeypatch)
    static = SimpleNamespace(name="get_skill")
    toolset = SessionToolset(_WS, [static])

    tools, _version = asyncio.run(
        toolset.tools_and_version({"session_id": _SESSION, "loaded_skills": []})
    )

    assert [t.name for t in tools] == ["get_skill"]


def test_main_graph_wires_dynamic_toolset_into_three_nodes(tmp_path, monkeypatch):
    """主图的确把 toolset 接给了节点，且**模型交给 LLMNode 去绑**（构图期不再 bind_tools）。

    这条是"接线"测试：主图此前没有任何测试构造它，改错线只会在人跑 TUI 时才暴露。
    这里把 `load_mcp_tool` 与模型都换掉（不 spawn、不花钱），跑通一轮 llm_node。
    """
    from app.agent import graph as graph_mod

    async def fake_load_mcp_tool(workspace, session_id=None):
        return []

    monkeypatch.setattr(graph_mod, "load_mcp_tool", fake_load_mcp_tool)
    spy = _SpyModel()
    monkeypatch.setattr(graph_mod, "get_main_chat_model", lambda: spy)

    async def main():
        graph = await graph_mod.get_main_agent_graph(str(tmp_path), "graph-session")
        compiled = graph.compile()
        out = await compiled.ainvoke(
            {
                "session_id": "graph-session",
                "messages": [HumanMessage(content="hi")],
                "loaded_skills": [],
            },
            {"configurable": {"thread_id": "graph-session"}},
        )
        return out

    out = asyncio.run(main())

    assert out["messages"][-1].content == "ok"
    assert spy.binds == 1  # 模型是被 LLMNode 绑的，不是构图期
    assert spy.bound_names is not None and "get_skill" in spy.bound_names  # 静态工具没被漏掉


# ---------------------- 真 E2E：真 spawn 一个技能 server ----------------------

_FIXTURE_ROOT = Path(__file__).parent / "fixtures"


def test_real_skill_server_spawns_serves_tools_and_is_reaped(tmp_path, monkeypatch):
    """走真实链路：加载 fixture 技能 → 真拉起 `python -m skills_pkg.echo.server` → 调它的工具
    → 卸载 → 工具消失、子进程被收掉。

    它是唯一一条证明"启动路径可用"的用例：其余用例都假掉了 schema 列器，绕开了 spawn。
    技能源与包名一起指到 tests/fixtures（SKILL_DESIGN §13 的测试缝），所以不碰仓库的 skills/。
    """
    mcp._reset_pools_for_tests()
    monkeypatch.setattr(skills, "SKILLS_DIR", _FIXTURE_ROOT / "skills_pkg")
    monkeypatch.setattr(skills, "SKILLS_PACKAGE", "skills_pkg")
    # 子进程靠 PYTHONPATH 找到包 → 把它指到 fixture 根（真环境里是项目根）
    _real_root = mcp_module.PROJECT_ROOT
    # 子进程的 PYTHONPATH 要同时含两个根：fixture 根（找 skills_pkg）+ 真实项目根（找 app/mcp_service）
    monkeypatch.setattr(mcp_module, "PROJECT_ROOT", f"{_FIXTURE_ROOT}{os.pathsep}{_real_root}")
    _fake_cfg(monkeypatch, {"demo_echo": "skills_pkg/echo"})
    _fake_settings(monkeypatch)

    workspace = str(tmp_path)

    async def main():
        await mcp.load_mcp_tool(workspace, _SESSION)  # 构图期该做的第一步
        state = {"session_id": _SESSION, "loaded_skills": []}
        out = await get_skill.coroutine(
            name="echo", state=state, tool_call_id="c1", workspace=workspace
        )
        assert out["loaded_skills"] == ["echo"]

        tools = {t.name: t for t in mcp.session_tools(workspace, _SESSION)}
        assert "demo_echo" in tools
        result = await tools["demo_echo"].ainvoke({"text": "hi"})
        assert "echo: hi" in str(result)
        key = (mcp._normalize_workspace(workspace), _SESSION, "skills_pkg/echo")
        assert key in mcp._WORKERS  # 真起了运行体（懒起：首次调用时才建）

        await drop_skill.coroutine(
            name="echo",
            state={"session_id": _SESSION, "loaded_skills": ["echo"]},
            tool_call_id="c2",
            workspace=workspace,
        )
        assert "demo_echo" not in mcp.session_tool_names(workspace, _SESSION)
        assert key not in mcp._WORKERS  # 运行体与子进程都被收掉

    asyncio.run(main())
