"""app/resource.py 纯函数测试：路径单点（workspace_key / session_db_path / memory_root / legacy 清理）。

app.resource 只依赖 stdlib/pathlib（import 不触发 app.agent → 无需 .env）。
路径断言用 monkeypatch RESOURCE_ROOT 到 tmp，避免碰真实 resource 目录。
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
