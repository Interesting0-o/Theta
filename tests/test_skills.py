"""app/agent/skills.py：技能源的扫描 / 解析 / 渲染（单源 = 项目根 skills/）。

源目录用 monkeypatch `skills.SKILLS_DIR` 指到 tmp——不碰仓库里真实的 `skills/`
（否则加一个技能就会弄坏这些用例）。
import app.agent.* 经 __init__ 触发 get_settings()，需 .env 存在（CLAUDE.md 前提）。
"""
import json
from pathlib import Path

import pytest

from app.agent import skills
from app.schema.agent_schema import SkillPreflight


def _make_skill(root: Path, name: str, *, description: str = "干某件事的技能", body: str = "正文") -> Path:
    """在技能源下造一个技能目录，返回它。"""
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.fixture
def skills_dir(tmp_path, monkeypatch) -> Path:
    """技能源指到 tmp（不扫仓库里真实的 skills/）。"""
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(skills, "SKILLS_DIR", root)
    return root


# ---------------------- 解析（frontmatter） ----------------------


def test_split_frontmatter_reads_single_line_keys():
    meta, body = skills._split_frontmatter("---\nname: github\ndescription: 平台操作\n---\n\n正文\n")

    assert meta == {"name": "github", "description": "平台操作"}
    assert body == "正文"


def test_split_frontmatter_without_frontmatter_returns_text_as_is():
    meta, body = skills._split_frontmatter("# 没有 frontmatter\n\n正文\n")

    assert meta == {}
    assert body == "# 没有 frontmatter\n\n正文\n"


def test_split_frontmatter_unclosed_returns_text_as_is():
    """`---` 没闭合 → 当没有 frontmatter（不猜、不崩）。"""
    text = "---\nname: x\n\n正文"

    meta, body = skills._split_frontmatter(text)

    assert meta == {}
    assert body == text


# ---------------------- 扫描 ----------------------


def test_scan_reads_name_and_description(skills_dir):
    _make_skill(skills_dir, "github", description="GitHub 平台操作")

    metas = skills.scan_skills()

    assert set(metas) == {"github"}
    assert metas["github"].description == "GitHub 平台操作"


def test_scan_skips_skill_without_description(skills_dir, caplog):
    """缺 description → 不进目录并告警（§9：没有它这条目录对模型毫无信息量）。"""
    d = skills_dir / "mute"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: mute\n---\n\n正文\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        metas = skills.scan_skills()

    assert metas == {}
    assert "缺 description" in caplog.text


def test_scan_skips_directory_without_skill_md(skills_dir):
    """没有 SKILL.md 的目录不是技能（可能是杂物）——静默跳过，不算坏。"""
    (skills_dir / "not-a-skill").mkdir()

    assert skills.scan_skills() == {}


def test_scan_name_falls_back_to_dirname(skills_dir):
    """frontmatter 没写 name → 用目录名（name 本来就可以不解析）。"""
    d = skills_dir / "from-dir"
    d.mkdir()
    (d / "SKILL.md").write_text("---\ndescription: 只有描述\n---\n\n正文\n", encoding="utf-8")

    assert set(skills.scan_skills()) == {"from-dir"}


def test_scan_missing_source_dir_returns_empty(tmp_path, monkeypatch):
    """源目录不存在 → 空表，不是异常（配 Skills 未安装 / 项目根没有 skills/）。"""
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "nope")

    assert skills.scan_skills() == {}


def test_scan_skips_duplicate_skill_name(skills_dir, caplog):
    """两个目录声明同一个 name → 响亮跳过后者。

    静默让后者覆盖前者是"装了却不可发现"（§3.6 里最坏的失败形态）：模型看到的目录与
    get_skill 查到的元信息会指向两个不同目录，其中一个再也取不到正文。
    """
    _make_skill(skills_dir, "alpha", description="先扫到的")
    dup = skills_dir / "beta"
    dup.mkdir()
    (dup / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: 重名的那个\n---\n\n正文\n", encoding="utf-8"
    )

    with caplog.at_level("WARNING"):
        metas = skills.scan_skills()

    assert set(metas) == {"alpha"}  # 目录序在前者胜出，且是确定性的
    assert metas["alpha"].dir_name == "alpha"
    assert "重复" in caplog.text and "beta" in caplog.text


def _make_capability(skills_dir: Path, name: str, *, frontmatter_name: str | None = None) -> Path:
    """造一个带 server.py 的技能目录（frontmatter 的 name 可与目录名不一致）。"""
    d = _make_skill(skills_dir, name)
    (d / "server.py").write_text("# 占位：扫描用例不会真起它\n", encoding="utf-8")
    if frontmatter_name is not None:
        (d / "SKILL.md").write_text(
            f"---\nname: {frontmatter_name}\ndescription: 干某件事的技能\n---\n\n正文\n",
            encoding="utf-8",
        )
    return d


def test_scan_downgrades_capability_when_name_differs_from_dir(skills_dir, caplog):
    """有 server.py 但 name != 目录名 → 降级为知识型。

    能力型的启动模块与 tool.json 的 source 都按**目录名**走，不一致的技能**永远过不了加载
    校验**——留在目录里标 [带工具] 就是谎报能力，模型会照着去加载、每次被拒。降级（而不是
    跳过整个技能）与"目录名不是 identifier"那条同一取舍：正文仍然是可用的那一半。
    """
    _make_capability(skills_dir, "demo", frontmatter_name="demo-v2")

    with caplog.at_level("WARNING"):
        metas = skills.scan_skills()

    assert metas["demo-v2"].capability is False
    assert "不一致" in caplog.text
    catalog = skills.catalog_block()
    assert "- demo-v2:" in catalog  # 仍在目录里（正文可用）
    assert "- demo-v2 [带工具]" not in catalog  # 但不再谎报能力（抬头那句说明里也有这个词）


def test_scan_keeps_capability_when_name_matches_dir(skills_dir):
    """对齐的那一半：name == 目录名且有 server.py → 仍是能力型（降级不许误伤正常技能）。"""
    _make_capability(skills_dir, "demo")

    metas = skills.scan_skills()

    assert metas["demo"].capability is True
    assert "[带工具]" in skills.catalog_block()


def test_scan_downgrades_capability_with_unusable_dirname(skills_dir, caplog):
    """目录名不是合法 identifier → 拼不出 `skills.<dir>.server`，同样降级（既有行为，一并钉住）。"""
    _make_capability(skills_dir, "not-an-identifier")

    with caplog.at_level("WARNING"):
        metas = skills.scan_skills()

    assert metas["not-an-identifier"].capability is False
    assert "identifier" in caplog.text


# ---------------------- 读取（白名单，不拼路径） ----------------------


def test_read_body_strips_frontmatter(skills_dir):
    _make_skill(skills_dir, "github", body="这里是正文")

    body = skills.read_body("github")

    assert body is not None
    assert "这里是正文" in body
    assert "description:" not in body  # frontmatter 已剥掉


def test_read_body_rejects_path_traversal(skills_dir):
    """**安全纪律**：name 来自模型，只用它查扫描结果、绝不拼路径。

    技能目录在项目根、**不在工作区内**，file_io 的沙箱管不到这里，必须自查。
    """
    _make_skill(skills_dir, "github")
    outside = skills_dir.parent / "secret.md"
    outside.write_text("SECRET", encoding="utf-8")

    for evil in (
        "../secret.md",
        "../../secret.md",
        str(outside),
        "./github",
        "github/../../secret.md",
        "",
    ):
        assert skills.read_body(evil) is None, evil


def test_read_body_unknown_returns_none(skills_dir):
    _make_skill(skills_dir, "github")

    assert skills.read_body("nope") is None


# ---------------------- 渲染 ----------------------


def test_catalog_block_empty_when_no_skills(skills_dir):
    assert skills.catalog_block() == ""


def test_catalog_block_lists_name_and_description(skills_dir):
    _make_skill(skills_dir, "github", description="GitHub 平台操作")

    text = skills.catalog_block()

    assert "- github: GitHub 平台操作" in text
    assert "get_skill" in text


def test_catalog_block_truncates_over_cap_and_warns(skills_dir, caplog):
    """目录不可淘汰、溢出只能截断——而截断 = 某些技能装了却不可发现，所以必须告警（§3.6）。"""
    for i in range(4):
        _make_skill(skills_dir, f"s{i}", description=f"第 {i} 个")

    with caplog.at_level("WARNING"):
        text = skills.catalog_block(cap=2)

    assert "- s0:" in text and "- s1:" in text  # 前两个作为条目列出
    assert "- s2:" not in text and "- s3:" not in text  # 后两个不列条目……
    assert "因目录上限未列出" in text and "s2" in text  # ……但要在告警行里点名（绝不静默丢）
    assert "不可发现" in caplog.text


def test_skills_block_empty_when_nothing_loaded(skills_dir):
    _make_skill(skills_dir, "github")

    assert skills.skills_block([]) == ""


def test_skills_block_headers_loaded_names_and_inlines_bodies(skills_dir):
    _make_skill(skills_dir, "github", body="推送前先开分支")

    text = skills.skills_block(["github"])

    # 抬头列出已加载技能——模型据此决定卸哪个、也是"某技能不在列表里了"的卸载信号（§3.5）
    assert text.startswith("# 已加载技能")
    assert "当前已加载：github" in text
    assert "drop_skill" in text
    assert "推送前先开分支" in text


def test_skills_block_names_unreadable_names_as_unavailable(skills_dir):
    """清单里有个已失效的名字（技能被删了）→ **显式点名告警**，不是静默丢掉。

    静默丢 = 模型以为纪律还在（它是照 loaded_skills 里的名字行事），而正文与工具都已经是空的
    ——正是 §3.5 要避免的影子态。点名叫它 drop_skill 是可行动的最小补救。
    """
    _make_skill(skills_dir, "github", body="正文")

    text = skills.skills_block(["ghost", "github"])

    assert "当前已加载：github" in text
    assert "## github" in text  # 存活的那个照常内联
    assert "已不可用" in text and "ghost" in text  # 失效的那个被点名
    assert "drop_skill" in text  # 且给出补救动作


def test_bodies_size_sums_loaded_bodies(skills_dir):
    _make_skill(skills_dir, "a", body="x" * 100)
    _make_skill(skills_dir, "b", body="y" * 300)

    size_a = len(skills.read_body("a"))

    assert skills.bodies_size(["a", "b"]) > size_a  # 两个都算进去了
    assert skills.bodies_size([]) == 0
    assert skills.bodies_size(["ghost"]) == 0  # 读不到的按 0 计


# ---------------------- 依赖声明（skill.json 的 requirements） ----------------------


def test_read_requirements_reads_declaration(skills_dir):
    """合法声明 → 顶层包名列表（去空白、丢空项）。"""
    skill_dir = _make_skill(skills_dir, "demo")
    _with_config(skill_dir, {"requirements": ["mcp", " PyGithub ", ""]})

    assert skills.read_requirements(skills.get_meta("demo")) == ["mcp", "PyGithub"]


def test_read_requirements_absent_or_broken_is_empty(skills_dir, caplog):
    """没声明 / 形状不对 → 空表（少一层保护，但不会因此让技能加载不了）。"""
    plain = _make_skill(skills_dir, "plain")
    broken = _make_skill(skills_dir, "broken")
    _with_config(broken, {"requirements": "mcp"})

    assert skills.read_requirements(skills.get_meta("plain")) == []
    with caplog.at_level("WARNING"):
        assert skills.read_requirements(skills.get_meta("broken")) == []
    assert "requirements" in caplog.text


# ---------------------- 加载时体检（skill.json 的 preflight） ----------------------


def _with_config(skill_dir: Path, config: dict) -> None:
    (skill_dir / "skill.json").write_text(json.dumps(config), encoding="utf-8")


def test_read_preflight_reads_declaration(skills_dir):
    """合法声明 → 值对象；`env` 与 `preflight` 共用同一份 skill.json，互不影响。"""
    skill_dir = _make_skill(skills_dir, "demo")
    _with_config(
        skill_dir,
        {
            "env": ["GITHUB_TOKEN"],
            "preflight": {"url": "https://api.example.com/user", "bearer": "GITHUB_TOKEN"},
        },
    )
    meta = skills.get_meta("demo")

    assert skills.read_skill_env(meta) == ["GITHUB_TOKEN"]
    assert skills.read_preflight(meta) == SkillPreflight(
        url="https://api.example.com/user", bearer_env="GITHUB_TOKEN"
    )


def test_read_preflight_absent_is_none(skills_dir):
    """没声明（连 skill.json 都没有）→ None，不告警、不影响别的东西。"""
    _make_skill(skills_dir, "demo")

    assert skills.read_preflight(skills.get_meta("demo")) is None


def test_read_preflight_rejects_incomplete_declaration(skills_dir, caplog):
    """缺 url / 缺 bearer → 按"没声明"处理 + 告警（体检是锦上添花，写错不该让技能加载不了）。"""
    skill_dir = _make_skill(skills_dir, "demo")
    _with_config(skill_dir, {"env": ["GITHUB_TOKEN"], "preflight": {"url": "https://x/y"}})
    meta = skills.get_meta("demo")

    with caplog.at_level("WARNING"):
        assert skills.read_preflight(meta) is None

    assert "preflight" in caplog.text


def test_read_preflight_rejects_unrequested_bearer(skills_dir, caplog):
    """`bearer` 必须是本技能**声明过的** env 键——否则等于让它凭空读一个没申请的凭据。"""
    skill_dir = _make_skill(skills_dir, "demo")
    _with_config(
        skill_dir,
        {
            "env": ["GITHUB_TOKEN"],
            "preflight": {"url": "https://x/y", "bearer": "CHAT_MODEL_API_KEY"},
        },
    )
    meta = skills.get_meta("demo")

    with caplog.at_level("WARNING"):
        assert skills.read_preflight(meta) is None

    assert "CHAT_MODEL_API_KEY" in caplog.text


def test_read_preflight_ignores_broken_config(skills_dir, caplog):
    """skill.json 不是 JSON 对象 → 两个读取方都按"没声明"处理（不炸扫描）。"""
    skill_dir = _make_skill(skills_dir, "demo")
    (skill_dir / "skill.json").write_text("[1, 2]", encoding="utf-8")
    meta = skills.get_meta("demo")

    with caplog.at_level("WARNING"):
        assert skills.read_preflight(meta) is None
        assert skills.read_skill_env(meta) == []
