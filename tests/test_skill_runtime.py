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

import app.platform.mcp as mcp_module
import app.agent.tools as tools_module
import app.config as config
import app.platform.mcp as mcp  # noqa: E402
from app.resource import skills
from app.agent.nodes import LLMNode
from app.agent.tools import SessionToolset, drop_skill, get_skill
from app.schema.agent_schema import MCPToolSpec, SkillMeta, SkillPreflight

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
    """把"审批登记"落成各技能目录自己的 skill.json（2026-09-19 起声明随技能走）。

    mapping 保留旧形状 {工具名: "skills/<目录>"}（source 值不再参与判定，只用来定位目录），
    逐条写成 {"need_review": false}——要测 need_review:true / 形状错误，直接改那个目录的
    skill.json。monkeypatch 参数仅为调用点兼容保留，这里什么都不 patch（读盘不缓存，
    _check_skill_tools 每次加载都重新读文件）。
    """
    by_dir: dict[str, dict] = {}
    for name, source in mapping.items():
        by_dir.setdefault(source.split("/")[-1], {})[name] = {"need_review": False}
    for dir_name, tool_decls in by_dir.items():
        path = skills.SKILLS_DIR / dir_name / "skill.json"
        config = {}
        if path.is_file():
            config = json.loads(path.read_text(encoding="utf-8"))
        config["tools"] = tool_decls
        path.write_text(json.dumps(config), encoding="utf-8")


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


def test_unregister_drops_the_server_schema_cache(skills_dir, monkeypatch):
    """卸载时连该 server 的 schema 缓存一起丢。

    缓存是"列一次用一辈子"的，留着会让"改技能 → drop_skill 再 get_skill"拿到**新代码 + 旧工具表**
    （新增/改名的工具看不见，而运行体已经是新的了）。这里直接种一条缓存再卸载，验证它被清掉。
    """
    mcp._reset_pools_for_tests()
    _fake_settings(monkeypatch)
    key = (_WS, "skills/demo")
    mcp_module._SERVER_SPECS[key] = []

    asyncio.run(mcp.unregister_server(_WS, _SESSION, "skills/demo"))

    assert key not in mcp_module._SERVER_SPECS


def test_reload_reraises_missing_runtime(skills_dir, monkeypatch):
    """名字已在清单里、运行体却不在（对账重建失败被退避）→ get_skill 是**正常重试入口**。

    失败退避（见 `_register_failed`）把"每轮自动重试"换掉了，若这里也按"已加载无需重复"
    一挡，模型就再没有重试路径了：正文在、工具没有，照 SKILL.md 去调工具只会撞 unknown_tool，
    而那条回执恰恰指它来 get_skill。
    """
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo")
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo"})
    calls = _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)

    _load("demo")
    assert len(calls) == 1

    mcp._EXTRA_TOOLS.clear()  # 模拟"运行体没了"（重建失败被退避后的状态）
    out = _load("demo", ["demo"])

    assert len(calls) == 2  # 真的重新拉起
    assert "重新拉起" in out["messages"][0].content
    assert "demo_tool" in mcp.session_tool_names(_WS, _SESSION)


def test_reload_reports_reason_when_runtime_cannot_be_rebuilt(skills_dir, monkeypatch):
    """重试仍失败 → 回的是**可行动的原因**（工具没声明），不是干巴巴的"已在加载中"。"""
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo")
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo"})
    _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)
    _load("demo")

    mcp._EXTRA_TOOLS.clear()
    (skills_dir / "demo" / "skill.json").write_text("{}", encoding="utf-8")  # 声明被撤掉
    out = _load("demo", ["demo"])

    assert "skill.json" in out["messages"][0].content
    assert "重新拉起" not in out["messages"][0].content


# ---------------------- 依赖自检（skill.json 的 requirements） ----------------------


def test_missing_requirement_is_refused_before_any_spawn(skills_dir, monkeypatch):
    """声明了装不上的依赖 → 加载前就拒（**一次 schema 都没列过**），回执给 uv sync 出路。

    这条之所以能零成本：技能依赖装在主 venv，技能 server 与 host 是同一个解释器，
    所以 host 侧 `find_spec`（只定位、不执行）的结论就是子进程的结论。
    """
    mcp._reset_pools_for_tests()
    skill_dir = _make_skill(skills_dir, "demo")
    (skill_dir / "skill.json").write_text(
        json.dumps({"requirements": ["这个包肯定不存在_xyz"]}), encoding="utf-8"
    )
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo"})
    calls = _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)

    out = _load("demo")

    assert list(out) == ["messages"]  # 没写 loaded_skills
    content = out["messages"][0].content
    assert "这个包肯定不存在_xyz" in content
    assert "uv sync" in content and "重新 get_skill" in content
    assert calls == []  # 连 schema 都没列过 → 更没有起进程
    assert mcp._WORKERS == {}


def test_installed_requirement_does_not_block(skills_dir, monkeypatch):
    """声明了**确实装着**的依赖（`mcp` 是第三方包，技能自己在用）→ 不拦，照常加载。"""
    mcp._reset_pools_for_tests()
    skill_dir = _make_skill(skills_dir, "demo")
    (skill_dir / "skill.json").write_text(json.dumps({"requirements": ["mcp"]}), encoding="utf-8")
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo"})
    _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)

    out = _load("demo")

    assert out["loaded_skills"] == ["demo"]


# ---------------------- 起不来时的回执 ----------------------


def test_startup_failure_receipt_carries_child_traceback(monkeypatch, tmp_path):
    """起不来时回执要带**子进程的真 traceback**，而不是 `ExceptionGroup` 黑箱。

    真 spawn `python -m skills_pkg.broken.server`（fixture 在 import 期 import 一个不存在的模块）：
    适配器只把 `ExceptionGroup → McpError: Connection closed` 交给 host，真正的原因只在子进程的
    stderr 上——所以加载失败后要**重跑一次 import** 把它捞回来，否则模型拿着"配置问题请告知用户"
    却说不出来是什么问题。
    """
    mcp._reset_pools_for_tests()
    monkeypatch.setattr(skills, "SKILLS_DIR", _FIXTURE_ROOT / "skills_pkg")
    monkeypatch.setattr(skills, "SKILLS_PACKAGE", "skills_pkg")
    monkeypatch.setattr(
        mcp_module, "PROJECT_ROOT", f"{_FIXTURE_ROOT}{os.pathsep}{mcp_module.PROJECT_ROOT}"
    )
    _fake_settings(monkeypatch)

    out = asyncio.run(
        get_skill.coroutine(
            name="broken",
            state={"session_id": _SESSION, "loaded_skills": []},
            tool_call_id="c1",
            workspace=str(tmp_path),
        )
    )

    content = out["messages"][0].content
    assert "起不来" in content
    assert "imaginary_dependency_that_does_not_exist" in content  # ← 子进程的真原因
    assert "告知用户" in content
    assert "loaded_skills" not in out  # 失败即不写清单


def test_startup_failure_falls_back_when_probe_says_fine(skills_dir, monkeypatch):
    """探针说"import 没问题"时，回退到**展平后的异常**——不拿一段无关 traceback 误导模型。

    （import 能过却起不来 = 协议层/超时那类，与"缺依赖、语法错"无关。）
    """
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo")
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo"})
    _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)
    monkeypatch.setattr(tools_module, "_probe_skill_startup", lambda *a, **k: None)

    async def boom(*args, **kwargs):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("Connection closed")])

    monkeypatch.setattr(mcp_module, "register_server", boom)

    out = _load("demo")

    content = out["messages"][0].content
    assert "起不来" in content
    assert "Connection closed" in content  # 展平后看得见，不再是"1 sub-exception"
    assert "1 sub-exception" not in content


# ---------------------- 加载时体检 ----------------------


def _fake_preflight(monkeypatch, *, ok: bool, detail: str = "HTTP 200") -> list[str]:
    """把体检端点与探针都换成假的；返回探针调用记录（只在声明了 preflight 的技能上才会被调）。"""
    monkeypatch.setattr(
        skills,
        "read_preflight",
        lambda meta: SkillPreflight(url="https://example.test/user", bearer_env="GITHUB_TOKEN"),
    )
    calls: list[str] = []

    def probe(url: str, token: str):
        calls.append(url)
        return ok, detail

    monkeypatch.setattr(tools_module, "_probe_skill_credential", probe)
    return calls


def test_preflight_line_lands_in_the_receipt(skills_dir, monkeypatch):
    """体检结论随 `get_skill` 的回执给模型——这是它替掉 github_auth_status 那个工具的理由。"""
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo", env=["GITHUB_TOKEN"])
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo"})
    _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)
    calls = _fake_preflight(monkeypatch, ok=True)

    out = _load("demo")

    content = out["messages"][0].content
    assert "凭证体检：GITHUB_TOKEN 可用" in content
    assert calls == ["https://example.test/user"]  # 真去打了一次


def test_probe_credential_catches_read_phase_oserror(monkeypatch):
    """读阶段的**裸 OSError** 必须被接住——它是"体检失败不阻断加载"这条契约的一部分。

    回归：urlopen 只在 connect/send 阶段把 OSError 包成 URLError；读阶段的超时
    （TimeoutError）、连接被重置（ConnectionResetError）是裸 OSError。漏接就会逃出
    get_skill——而那时 register_server 已经跑过，等于注册了却没写回 loaded_skills。
    """
    def _boom(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(tools_module.urllib.request, "urlopen", _boom)

    ok, detail = tools_module._probe_skill_credential("https://example.test/user", "token")

    assert ok is False
    assert "连不上" in detail


def test_preflight_failure_does_not_block_loading(skills_dir, monkeypatch):
    """体检失败**不阻断加载**：正文/纪律在拿不到远端时照样有用，工具也照常注册。

    但结论要醒目地报关（并点明"告知用户"）——否则模型会带着一个坏凭证一头撞进工具调用。
    """
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo", env=["GITHUB_TOKEN"])
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo"})
    _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)
    _fake_preflight(monkeypatch, ok=False, detail="被拒（HTTP 401：令牌无效/过期，或权限不足）")

    out = _load("demo")

    assert out["loaded_skills"] == ["demo"]  # 仍然加载
    assert mcp.registered_servers(_WS, _SESSION) == {"skills/demo"}  # 工具照常注册
    content = out["messages"][0].content
    assert "⚠️ 凭证体检失败" in content and "告知用户" in content
    assert "HTTP 401" in content


def test_preflight_not_run_for_knowledge_skill(skills_dir, monkeypatch):
    """知识型既不起进程、也不该去打体检（它没有凭证那回事）。"""
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "know", capability=False)
    _fake_settings(monkeypatch)
    calls = _fake_preflight(monkeypatch, ok=True)

    out = _load("know")

    assert out["loaded_skills"] == ["know"]
    assert calls == []
    assert "体检" not in out["messages"][0].content


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
    """工具没在技能自己的 skill.json 里声明（或形状不对）→ **拒绝加载**，且不留半截状态。"""
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo")
    _fake_cfg(monkeypatch, {})  # 什么都不声明
    _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)

    out = _load("demo")

    assert list(out) == ["messages"]  # 没有 loaded_skills
    content = out["messages"][0].content
    assert "demo_tool" in content and "skill.json" in content
    assert mcp.registered_servers(_WS, _SESSION) == set()  # 回滚干净：没留下工具
    assert mcp.session_tool_names(_WS, _SESSION) == set()
    # 版本会前进两格（注册 +1、回滚注销再 +1）——它只是"该重绑了"的信号，不参与任何判定，
    # 多推一格换来"回滚路径不搞特例"，这是划算的；这里不复位，就是为了把这条口径钉住。


def test_declared_but_unexposed_tool_is_rejected(skills_dir, monkeypatch):
    """声明了 server 没暴露的工具 → 拒绝（拼错名字的静默形态：政策看似有、工具实不存在）。"""
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo")
    (skills_dir / "demo" / "skill.json").write_text(
        json.dumps(
            {
                "tools": {
                    "demo_tool": {"need_review": False},
                    "ghost_tool": {"need_review": False},
                }
            }
        ),
        encoding="utf-8",
    )
    _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)

    out = _load("demo")

    content = out["messages"][0].content
    assert "ghost_tool" in content and "没有暴露" in content
    assert "loaded_skills" not in out


def test_bad_shape_declaration_is_rejected(skills_dir, monkeypatch):
    """need_review 不是布尔 → 拒绝（fail-closed：宁可不加载，不留一个政策含糊的工具）。"""
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo")
    (skills_dir / "demo" / "skill.json").write_text(
        json.dumps({"tools": {"demo_tool": {"need_review": "yes"}}}),
        encoding="utf-8",
    )
    _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)

    out = _load("demo")

    content = out["messages"][0].content
    assert "形状不对" in content and "demo_tool" in content
    assert "loaded_skills" not in out


def test_review_node_resolves_skill_declaration(skills_dir, monkeypatch):
    """ReviewNode 两级查询：技能声明生效、内置表优先、"在表里却两头无政策"仍 fail-closed。"""
    from app.agent.nodes import ReviewNode

    class _StubToolset:
        def known_names(self, state):
            return {"demo_tool", "walled_tool", "mystery_tool"}

    _make_skill(skills_dir, "demo")
    _make_skill(skills_dir, "walled")
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo", "walled_tool": "skills/walled"})
    # 把 walled 的声明改成 need_review:true（_fake_cfg 只会写 false）
    walled_json = skills_dir / "walled" / "skill.json"
    walled_json.write_text(
        json.dumps({"tools": {"walled_tool": {"need_review": True}}}), encoding="utf-8"
    )

    node = ReviewNode(toolset=_StubToolset())
    demo_state = {"loaded_skills": ["demo"]}

    # 技能声明 need_review:false → free；need_review:true → review
    assert node._decide("demo_tool", node._tool_cfg("demo_tool", demo_state), demo_state) == "free"
    walled_state = {"loaded_skills": ["walled"]}
    assert (
        node._decide("walled_tool", node._tool_cfg("walled_tool", walled_state), walled_state)
        == "review"
    )
    # 在工具表里、却两头都没有政策 → 第二道防线兜住（review，不静默放行）
    assert node._decide("mystery_tool", node._tool_cfg("mystery_tool", demo_state), demo_state) == "review"

    # 内置表优先：同名条目以 tool.json 为准（加载期重名已拒载，这是防御性口径）
    node.tool_review = {"demo_tool": {"need_review": True, "source": "mcp_service/x"}}
    assert node._tool_cfg("demo_tool", demo_state) == {
        "need_review": True,
        "source": "mcp_service/x",
    }


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
    """name 与目录名不一致的能力型：扫盘期就降级为知识型 → 按知识型加载（正文可用、工具不拉起）。

    降级落在 `scan_skills`（见 tests/test_skills.py），所以模型看到的目录与加载行为一致：
    **标 [带工具] 的技能一定加载得上**。加载期那道校验因此退化成安全网，由下一条用例直接
    喂它一个构造出来的 meta 来覆盖。
    """
    mcp._reset_pools_for_tests()
    skill_dir = _make_skill(skills_dir, "demo")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-v2\ndescription: 名字与目录不一致\n---\n\n正文", encoding="utf-8"
    )
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo"})
    calls = _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)

    out = _load("demo-v2")

    assert out["loaded_skills"] == ["demo-v2"]  # 按知识型加载成功
    assert calls == []  # 一次 schema 都没列过（没起过它的 server）


def test_capability_meta_with_mismatched_dir_is_refused_by_loader(skills_dir, monkeypatch):
    """安全网：即便拿到一个 capability=True 且 name != 目录名的 meta，加载期也必须拒绝。

    正常路径上这种 meta 到不了这里（扫盘期就降级了，见上一条），所以用例直接把假扫描结果
    喂进 `scan_skills`——守的是"校验即使退化也不许被删掉"。
    """
    mcp._reset_pools_for_tests()
    skill_dir = _make_skill(skills_dir, "demo")
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo"})
    calls = _fake_specs(monkeypatch, {"skills/demo": ["demo_tool"]})
    _fake_settings(monkeypatch)
    meta = SkillMeta(
        name="demo-v2",
        description="名字与目录不一致",
        dir_name="demo",
        capability=True,
        path=str(skill_dir / "SKILL.md"),
    )
    monkeypatch.setattr(skills, "scan_skills", lambda: {"demo-v2": meta})

    out = _load("demo-v2")

    assert list(out) == ["messages"]
    assert "目录名" in out["messages"][0].content
    assert calls == []


def test_clashing_tool_names_are_rejected(skills_dir, monkeypatch):
    """与编排工具同名、或与已加载技能的工具同名 → 拒绝（同名让审批与模型都无法区分两者）。"""
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo")
    _fake_cfg(monkeypatch, {"demo_tool": "skills/demo"})
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


def test_reconcile_fast_path_never_rescans_for_knowledge_skill(skills_dir, monkeypatch):
    """知识型技能在 loaded_skills 里时，对账仍必须是纯内存的（不许每轮重扫技能源）。

    回归防线：`registered` 只含能力型运行体、`loaded` 含两型名字，两边**直接比较永远不相等**
    ——若不把查实过的知识型记进 `SessionToolset._knowledge_only`，每个知识型技能都会让快路径
    失效：LLMNode 与 ToolNode 每轮各扫一遍技能源（iterdir + 逐个读 SKILL.md）。
    """
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "know", capability=False)
    scans: list[int] = []
    real = skills.scan_skills

    def counting():
        scans.append(1)
        return real()

    monkeypatch.setattr(skills, "scan_skills", counting)

    toolset = SessionToolset(_WS, [])
    state = {"session_id": _SESSION, "loaded_skills": ["know"]}

    asyncio.run(toolset.tools_and_version(state))
    after_first = len(scans)
    assert after_first == 1  # 第一次得读盘才知道它是知识型

    asyncio.run(toolset.tools_and_version(state))  # 下一轮 LLMNode
    asyncio.run(toolset.tools_map(state))  # 再过一次 ToolNode
    assert len(scans) == after_first  # 之后一次盘都没再读


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
    """对账炸了也不能炸整轮：沿用现状 + 告警（且失败要退避，见下一条）。"""
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo")
    _fake_settings(monkeypatch)
    attempts: list[str] = []

    async def boom(workspace, server, connection):
        attempts.append(server)
        raise RuntimeError("起不来")

    monkeypatch.setattr(mcp_module, "_server_specs", boom)
    toolset = SessionToolset(_WS, [])
    state = {"session_id": _SESSION, "loaded_skills": ["demo"]}

    tools, version = asyncio.run(toolset.tools_and_version(state))

    assert tools == [] and version == 0  # 没炸，返回现状
    assert len(attempts) == 1

    asyncio.run(toolset.tools_and_version(state))  # 下一轮不许再试（server 起不来这种失败要退避）
    asyncio.run(toolset.tools_map(state))
    assert len(attempts) == 1


def test_reconcile_failed_rebuild_backs_off(skills_dir, monkeypatch, caplog):
    """重建失败必须**退避**：记进 `SessionToolset._register_failed` 后，后续轮次一次都不再试。

    回归防线：失败不记的话，名字会一直留在 `missing` 里——LLMNode 与 ToolNode 每轮各重来一次，
    而重试要重新列 schema（回滚路径已把该 server 的 schema 缓存丢掉）= **每轮真起两个临时会话**。
    一个坏技能（server 起不来 / 新加的工具没在 tool.json 登记）会让整个会话每轮多起两个子进程，
    日志也每轮响两遍。

    "重开会话"是重试路径：备忘按实例存活，新实例会再试一次（`get_skill` 则从不查它）。
    """
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo")
    # 技能没写 skill.json 的 tools 声明（_make_skill 默认）→ 校验必失败
    calls: list[str] = []

    async def fake(workspace, server, connection):
        calls.append(server)
        return [
            MCPToolSpec(server=server, name="demo_tool", description="d", args_schema={}, metadata=None)
        ]

    monkeypatch.setattr(mcp_module, "_server_specs", fake)
    _fake_settings(monkeypatch)

    toolset = SessionToolset(_WS, [])
    state = {"session_id": _SESSION, "loaded_skills": ["demo"]}

    with caplog.at_level("WARNING"):
        asyncio.run(toolset.tools_and_version(state))  # 第 1 轮：试一次、失败、记住
        for _ in range(3):  # 之后 3 轮
            asyncio.run(toolset.tools_and_version(state))  # LLMNode
            asyncio.run(toolset.tools_map(state))  # ToolNode

    assert calls == ["skills/demo"]  # 只试过一次
    assert caplog.text.count("重建失败") == 1  # 也只响过一次

    fresh = SessionToolset(_WS, [])  # 重开会话 = 新实例
    asyncio.run(fresh.tools_and_version(state))
    assert len(calls) == 2  # 新会话真的重试


def test_reconcile_failed_backoff_is_per_session(skills_dir, monkeypatch):
    """失败退避必须**按会话**记：A 会话的失败不许让 B 会话整轮不再尝试重建。

    回归：旧实现只按名字记（`_register_failed: set[str]`），但同一个实例会服务多个会话
    （langgraph dev 一图多 thread）。更关键的是失败原因里有两条是**会话相关**的——"当前会话
    没有会话标识"、"与**本会话**已有工具重名"——B 会话本该能成，却会被 A 的失败挡住，且
    只表现为"正文在、工具没有"，没有任何回执说明为什么。
    """
    mcp._reset_pools_for_tests()
    _make_skill(skills_dir, "demo")
    # 技能没写 skill.json 的 tools 声明（_make_skill 默认）→ 校验必失败
    calls: list[str] = []

    async def fake(workspace, server, connection):
        calls.append(server)
        return [
            MCPToolSpec(server=server, name="demo_tool", description="d", args_schema={}, metadata=None)
        ]

    monkeypatch.setattr(mcp_module, "_server_specs", fake)
    _fake_settings(monkeypatch)

    toolset = SessionToolset(_WS, [])
    state_a = {"session_id": "sess-a", "loaded_skills": ["demo"]}
    state_b = {"session_id": "sess-b", "loaded_skills": ["demo"]}

    asyncio.run(toolset.tools_and_version(state_a))  # A：试一次、失败、记住
    assert len(calls) == 1
    asyncio.run(toolset.tools_and_version(state_a))  # A 后续：退避
    assert len(calls) == 1

    asyncio.run(toolset.tools_and_version(state_b))  # B 必须**自己**试一次
    assert len(calls) == 2


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
    # 默认模型的构造已从 app/agent/model.py 并入 LLMNode（2026-09-21）：打类上的静态方法
    from app.agent.nodes import LLMNode

    monkeypatch.setattr(LLMNode, "main_chat_model", staticmethod(lambda: spy))

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
    # 审批声明在 fixture 自己的 skill.json 里（tests/fixtures/skills_pkg/echo/skill.json），不在这写
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
