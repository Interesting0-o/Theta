"""app/resource/paths.py 纯函数测试：路径单点（基准 / 派生根 / 各落点 / legacy 清理）。

app.resource 只依赖 stdlib/pathlib（import 不触发 app.agent → 无需 .env）。
路径断言用 monkeypatch RESOURCE_ROOT 到 tmp，避免碰真实 resource 目录。
末节另钉两条**规定**（基准不许自算、落点只能调用），见 app/resource/paths.py 的 docstring。
"""
import re
from pathlib import Path

import app.resource.paths as resource


def test_workspace_key_is_sanitized_abs_path(tmp_path):
    ws = tmp_path / "My Proj"
    k = resource.workspace_key(str(ws))

    # 稳定：同一路径两次一致
    assert k == resource.workspace_key(str(ws))
    # 绝对路径作目录名：分隔符与盘符已被 '-' 替代，无非法字符/首尾短线
    assert "/" not in k and "\\" not in k and ":" not in k
    assert "--" not in k
    assert not k.startswith("-") and not k.endswith("-")
    # 形态：可读 slug + "_" + 8 位小写十六进制
    assert re.fullmatch(r"[^\\/:]+_[0-9a-f]{8}", k)
    # slug 部分以真实目录名收尾（可读）
    assert k.rsplit("_", 1)[0].endswith(Path(str(ws)).resolve().name)
    # 不同工作区 → 不同 key
    assert k != resource.workspace_key(str(tmp_path / "Other"))


def test_workspace_key_disambiguates_separator_collisions(tmp_path):
    """纯替换 `-` 会让 `a-b` 与 `a/b` 塌成同一个 key（两个工作区共用一份 resource 目录）。"""
    dashed = tmp_path / "a-b"
    nested = tmp_path / "a" / "b"
    dashed.mkdir()
    nested.mkdir(parents=True)

    assert resource.workspace_key(str(dashed)) != resource.workspace_key(str(nested))


def test_workspace_key_is_stable_across_path_spellings(tmp_path):
    """同一目录的不同写法（多一个 `.`/`..`）解析到同一 key。"""
    ws = tmp_path / "p"
    ws.mkdir()

    assert resource.workspace_key(str(ws)) == resource.workspace_key(str(ws / "."))
    assert resource.workspace_key(str(ws)) == resource.workspace_key(str(ws / ".." / "p"))


def test_session_db_path_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(resource, "RESOURCE_ROOT", tmp_path / "res")
    ws = tmp_path / "some" / "project"

    db = resource.session_db_path(str(ws), "conversation_456")
    key = resource.workspace_key(str(ws))

    assert db == tmp_path / "res" / key / "sessions" / "conversation_456" / "agent.db"
    assert db.parent.name == "conversation_456"
    assert db.parent.parent.name == "sessions"


def test_memory_root_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(resource, "RESOURCE_ROOT", tmp_path / "res")
    ws = tmp_path / "some" / "project"

    assert resource.memory_root(str(ws)) == (
        tmp_path / "res" / resource.workspace_key(str(ws)) / "memory"
    )


def test_remove_legacy_single_db(tmp_path, monkeypatch):
    monkeypatch.setattr(resource, "RESOURCE_ROOT", tmp_path)
    # 造旧单库（主库 + wal + shm）
    for suffix in ("", "-wal", "-shm"):
        Path(str(tmp_path / "agent.db") + suffix).write_text("x", encoding="utf-8")

    resource.remove_legacy_single_db()

    for suffix in ("", "-wal", "-shm"):
        assert not Path(str(tmp_path / "agent.db") + suffix).exists()

    resource.remove_legacy_single_db()  # 幂等：再跑一次不抛错


# ---------------------- 收束后：基准、派生根与工作区内的落点（2026-09-22） ----------------------


def test_project_root_is_the_single_anchor():
    """项目根只有一个出处，其余资源根全部由它派生（规定 3：基准不许自算）。

    这条同时钉住"谁是 owner"：`RESOURCE_ROOT` / `SKILLS_DIR` / `DEFAULT_WORKSPACE` 都必须是
    `PROJECT_ROOT` 的直接子路径——哪天有人拿 `Path(__file__)` 另算一份，这里会红。
    """
    assert resource.PROJECT_ROOT.is_dir()
    assert resource.RESOURCE_ROOT == resource.PROJECT_ROOT / "resource"
    assert resource.SKILLS_DIR == resource.PROJECT_ROOT / "skills"
    assert resource.DEFAULT_WORKSPACE == resource.PROJECT_ROOT / "tmp"


def test_workspace_file_landings_are_functions_not_inline_joins(tmp_path):
    """工作区内的约定文件由落点函数给出（规定 1：落点只能调用、不许拼）。"""
    ws = str(tmp_path)
    assert resource.agent_md_path(ws) == Path(ws) / "AGENT.md"
    assert resource.handoff_md_path(ws) == Path(ws) / "HANDOFF.md"
    assert resource.agent_md_path(ws).name == resource.AGENT_MD_FILENAME
    assert resource.handoff_md_path(ws).name == resource.HANDOFF_MD_FILENAME


def test_memory_path_lives_here_like_session_db_path(tmp_path, monkeypatch):
    """记忆文件的落点与会话库同类：都归 paths.py（此前 `memory_path` 住在 memory.py）。"""
    monkeypatch.setattr(resource, "RESOURCE_ROOT", tmp_path / "res")
    ws = str(tmp_path / "proj")
    assert resource.memory_path(ws) == resource.memory_root(ws) / "memory.md"
    assert resource.memory_path(ws).name == resource.MEMORY_FILENAME


def test_project_root_is_computed_in_exactly_one_place():
    """**规定 3 的机械防线**：生产代码里只许 `app/resource/paths.py` 用 `Path(__file__)` 推基准。

    白名单里的另外两处是**刻意**的，别顺手删：
    - `app/agent/gates.py`：策略表（`tool.json` / `command_policy.json`）必须与解析器同住，
      故它用 `Path(__file__).with_name(...)` 自拼（见 paths.py 的资源清单）；
    - `evaluation/fixtures.py`：评估框架自己的样本目录，app 不该知道评估的存在。

    谁要在别处"就近算一下项目根"（`Path(__file__).parents[N]`），这里立刻变红——那正是这条
    规定要防的动作：它不报错，只是静默指向别处。
    """
    allowlist = {
        Path("app/resource/paths.py"),
        Path("app/agent/gates.py"),
        Path("evaluation/fixtures.py"),
    }
    offenders = [
        p
        for root in ("app", "mcp_service", "skills", "evaluation")
        for p in Path(root).rglob("*.py")
        if "__pycache__" not in p.parts
        and "Path(__file__)" in p.read_text(encoding="utf-8")
        and p not in allowlist
    ]
    assert offenders == [], f"这些模块在自算基准（应改为 import app.resource.paths）：{offenders}"
