r"""resource 落盘路径单点：所有"按 (工作区, 会话) 定位"的数据都在这算。

布局：`<项目根>/resource/<ws_key>/…`
- `<ws_key>`：工作区**绝对路径**的可读 slug（把 `\` `/` 及盘符 `:` 替换成 `-`）末尾追加
  `_<sha1(该绝对路径)[:8]>`（如 `E:\code\python\Myllm` → `E-code-python-Myllm_3f9a1c2b`），
  稳定、可读、跨进程/重启一致；哈希用于消歧——纯替换会把 `C:\work\a-b` 与 `C:\work\a\b`
  塌成同一个 `C-work-a-b`，两个工作区就会共用一份 resource 目录；
- `<ws_key>/sessions/<session_id>/agent.db`：该 (工作区, 会话) 的 LangGraph checkpoint（每会话一库）；
- `<ws_key>/memory/`：Phase B 长期记忆 md 落点（本期只预留，不使用）。

注意：本模块只依赖 stdlib/pathlib——**不 import app.agent**（其 `__init__` re-export graph→model→
模块顶层 `get_settings()`，import 即要 .env）。放 `app/` 根（命名空间包、无 `__init__`）使
`app.platform`（基座）与测试能顶层 import 本模块而不触发 .env。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

# 项目根/resource（gitignore 整目录忽略）
RESOURCE_ROOT = Path(__file__).resolve().parent.parent / "resource"


def _sanitize_segments(text: str) -> str:
    """把分隔符/盘符转成 `-`，并收敛连续 `-`、去掉首尾 `-`，得到合法目录段。"""
    for ch in ("\\", "/", ":"):
        text = text.replace(ch, "-")
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-")


def workspace_key(workspace_path: str) -> str:
    """工作区目录名 = 绝对路径的可读 slug + `_<sha1(该绝对路径)[:8]>`。

    例：`E:\\code\\python\\Myllm` → `E-code-python-Myllm_3f9a1c2b`；`/mnt/e/...` → `mnt-e-..._…`。
    slug 与哈希**同源**（都用这里 resolve() 后的同一个字符串），所以 key 是「解析后绝对路径」
    的纯函数：同一目录永远同一 key（跨进程/重启一致），不同目录不会塌成同一个 key。

    哈希消的是 slug 的歧义：`-` 替换不可逆，`C:\\work\\a-b` 与 `C:\\work\\a\\b` 的 slug 都是
    `C-work-a-b`——不加哈希这两个工作区就共用一份 resource 目录（checkpoint 与 memory 一起串味）。
    8 位十六进制（32 bit）在同一台机器的工作区数量级下碰撞概率可忽略，又不显著拉长目录名。

    resolve() 顺带归一符号链接与 Windows 真实大小写/8.3 短名（工作区总是已存在的目录），
    故无需再 normcase——何况只 normcase 哈希输入会让大小写不同的同一目录得到「同哈希 + 不同 slug」。
    """
    abspath = str(Path(workspace_path).expanduser().resolve())
    digest = hashlib.sha1(abspath.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"{_sanitize_segments(abspath)}_{digest}"


def workspace_dir(workspace_path: str) -> Path:
    """该工作区在 resource 下的根目录（含 sessions/、memory/）。"""
    return RESOURCE_ROOT / workspace_key(workspace_path)


def session_db_path(workspace_path: str, session_id: str) -> Path:
    """某 (工作区, 会话) 的 checkpoint 库文件：resource/<ws_key>/sessions/<sid>/agent.db。"""
    return workspace_dir(workspace_path) / "sessions" / session_id / "agent.db"


def memory_root(workspace_path: str) -> Path:
    """某工作区的长期记忆 md 目录（Phase B 预留）。"""
    return workspace_dir(workspace_path) / "memory"


def remove_legacy_single_db() -> None:
    """删除旧单库 resource/agent.db（含 -wal / -shm）；不存在即 no-op。

    重构前所有会话共用一个 agent.db；切到 (工作区,会话) 分库后它不再被读，用户已确认直接删除。
    按调用时 RESOURCE_ROOT 计算（便于测试 monkeypatch）。
    """
    legacy = RESOURCE_ROOT / "agent.db"
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(legacy) + suffix)
        if candidate.exists():
            candidate.unlink()
