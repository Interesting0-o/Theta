"""app/agent/skills.py：技能源的扫描 / 解析 / 渲染（单源 = 项目根 skills/）。

源目录用 monkeypatch `skills.SKILLS_DIR` 指到 tmp——不碰仓库里真实的 `skills/`
（否则加一个技能就会弄坏这些用例）。
import app.agent.* 经 __init__ 触发 get_settings()，需 .env 存在（CLAUDE.md 前提）。
"""
from pathlib import Path

import pytest

from app.agent import skills


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


def test_skills_block_skips_unreadable_names(skills_dir):
    """清单里有个已失效的名字（技能被删了）→ 跳过它，别崩也别注入空块。"""
    _make_skill(skills_dir, "github", body="正文")

    text = skills.skills_block(["ghost", "github"])

    assert "当前已加载：github" in text
    assert "ghost" not in text


def test_bodies_size_sums_loaded_bodies(skills_dir):
    _make_skill(skills_dir, "a", body="x" * 100)
    _make_skill(skills_dir, "b", body="y" * 300)

    size_a = len(skills.read_body("a"))

    assert skills.bodies_size(["a", "b"]) > size_a  # 两个都算进去了
    assert skills.bodies_size([]) == 0
    assert skills.bodies_size(["ghost"]) == 0  # 读不到的按 0 计
