r"""**路径单点**：项目里全部"东西落在哪"都在这算（基准 + 落点）。

本模块是 `app/resource/` 包里唯一管**路径**的一支（同包另有 memory / skills / images /
profile——都是"读 agent 之外的东西、转成内部表示"，判据见 docs/ARCHITECTURE.md §4 的 A/B/C）。

## 资源清单（一眼看全：项目里到底有几样东西、各落在哪）

| 资源 | 落点 | 归属 |
| --- | --- | --- |
| 会话 checkpoint | `<项目根>/resource/<ws_key>/sessions/<sid>/agent.db` | 按 (工作区, 会话) 分区的私有运行时数据（可丢、不进仓库） |
| 长期记忆 | `<项目根>/resource/<ws_key>/memory/memory.md` | 同上（跨会话长存） |
| 项目画像 | `<工作区>/AGENT.md` | 工作区**内**、随用户仓库走 |
| 交接材料 | `<工作区>/HANDOFF.md` | 同上 |
| 技能库 | `<项目根>/skills/` | 随 Theta 出厂 |
| 策略表 | `app/agent/{tool,command_policy}.json` | 闸门判定材料——**刻意不去 paths**：表必须与解析器同住（`app/agent/gates.py`），故它用 `Path(__file__).with_name(...)` 自拼 |
| 配置 / 默认沙箱 | `<项目根>/.env`、`<项目根>/tmp/` | 部署私密 / gitignore |
| 评估样本 | `<项目根>/evaluation/fixtures/` | 随仓库；归 `evaluation` 自己（app 不该知道评估框架） |

## 规定：路径怎么取（2026-09-22 定，细化见 docs/ARCHITECTURE.md §4.4）

路径便宜（可拼、可上溯、`..` 随便接），所以"能不能拿到"从不构成约束，约束只剩规定：

1. **落点只能调用、不许拼** —— 约定资源（名字与位置由我们定死，如 `AGENT.md`）一律经具名函数取；
   判据是**约定 vs 发现**：`<ws>/**/*.py` 是发现（由内容与模式决定），拼接 + glob 正当。
2. **拼接的权利只属于"这段布局的主人"，且只在自己根以下** —— `skills.py` 拼
   `SKILLS_DIR/<dir>/skill.json` 正当（目录里摆什么是它的知识）；**别人不许替它拼**，多一个 `..`
   也不行。
3. **基准不许自算** —— 不许再用 `Path(__file__).parents[N]` 推断项目根（文件一挪，它不报错，
   只是静默指向别处）；基准只能由本题给出，其余全部从 `PROJECT_ROOT` 派生。
4. **`..` 在生产代码里基本是禁令** —— 唯一合法例外是**解析别人给的路径**（`file_io` 的沙箱解析、
   `images` 的 `@路径`、`config` 的 `.env` 定位）：那里是输入处理，`resolve()` 后必须过校验。

## 布局与注意

`<项目根>/resource/<ws_key>/…`
- `<ws_key>`：工作区**绝对路径**的可读 slug（把 `\` `/` 及盘符 `:` 替换成 `-`）末尾追加
  `_<sha1(该绝对路径)[:8]>`（如 `E:\code\python\Myllm` → `E-code-python-Myllm_3f9a1c2b`），
  稳定、可读、跨进程/重启一致；哈希用于消歧——纯替换会把 `C:\work\a-b` 与 `C:\work\a\b`
  塌成同一个 `C-work-a-b`，两个工作区就会共用一份 resource 目录。

注意：本模块（以及同包其余各支）只依赖 stdlib/pathlib——**不 import app.agent**（其 `__init__`
re-export graph → nodes，而 nodes 顶部读 `get_settings()`，import 即要 .env）。`app.resource`
因此可被 `app.platform`（基座）与测试顶层 import 而不触发 .env。

⚠️ `RESOURCE_ROOT` **只在本题定义、刻意不由 `app/resource/__init__.py` 转出**：它是可
monkeypatch 的模块全局（测试把它指到 tmp），转出去会让 `app.resource.RESOURCE_ROOT = …`
这种打补丁**静默失效**（函数读的仍是本模块的全局）。要在测试里改它，请打
`app.resource.paths.RESOURCE_ROOT`。同理，**别的模块里出现的同名常量都是"借名字"，不是真值**：
读者是谁、补丁就该打在哪（例如 `skills.SKILLS_DIR` 是 skills.py 从本题取过去的名字，它的函数
读的是 skills 模块里的全局，故测试打 `skills.SKILLS_DIR` 有效）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

# ---------------------- 基准：项目根（**唯一出处**，其余全部由它派生） ----------------------
# 本文件在 app/resource/ 下，故上溯三级到项目根。
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ---------------------- 由项目根派生的资源根 ----------------------
# resource/（gitignore 整目录忽略）：按 (工作区, 会话) 分区的私有运行时数据
RESOURCE_ROOT = PROJECT_ROOT / "resource"
# skills/：随 Theta 出厂的技能库（目录内部的布局见 app/resource/skills.py）
SKILLS_DIR = PROJECT_ROOT / "skills"
# tmp/：默认工作区（零参入口 langgraph dev 用；缺省时由 app/agent/graph.py 建目录）
DEFAULT_WORKSPACE = PROJECT_ROOT / "tmp"


def _sanitize_segments(text: str) -> str:
    """把分隔符/盘符转成 `-`，并收敛连续 `-`、去掉首尾 `-`，得到合法目录段。"""
    for ch in ("\\", "/", ":"):
        text = text.replace(ch, "-")
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-")


# ---------------------- 工作区内的约定文件（落点函数） ----------------------
# 文件名常量与落点函数**必须成对**放在一起（拆开就必然有人重新拼一遍），
# 消费方（app/resource/profile.py、app/agent/prompt.py …）只许调函数、不许自己拼。

# 项目画像文件名（`/init` 命令生成的就是它；与 file_io 沙箱同一根）
AGENT_MD_FILENAME = "AGENT.md"
# 交接材料文件名（project-handoff 技能的保存落点，见 skills/project-handoff/SKILL.md）
HANDOFF_MD_FILENAME = "HANDOFF.md"


def agent_md_path(workspace_path: str) -> Path:
    """工作区根的项目画像文件：`<工作区>/AGENT.md`。"""
    return Path(workspace_path) / AGENT_MD_FILENAME


def handoff_md_path(workspace_path: str) -> Path:
    """工作区根的交接材料：`<工作区>/HANDOFF.md`。"""
    return Path(workspace_path) / HANDOFF_MD_FILENAME


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
    """某工作区的长期记忆目录（其下 memory.md=正文；建会话时在此播种模板）。"""
    return workspace_dir(workspace_path) / "memory"


# 长期记忆文件名（白名单单点：模型无法指定路径）
MEMORY_FILENAME = "memory.md"


def memory_path(workspace_path: str) -> Path:
    """某工作区的记忆文件：`resource/<ws_key>/memory/memory.md`（内容与解析见
    `app/resource/memory.py`；落点归本题，别再在那边拼一遍）。
    """
    return memory_root(workspace_path) / MEMORY_FILENAME


def sessions_dir(workspace_path: str) -> Path:
    """某工作区的会话目录（`resource/<ws_key>/sessions/`，其下每个 `<session_id>/` 一个库）。"""
    return workspace_dir(workspace_path) / "sessions"


def iter_session_dbs(workspace_path: str) -> list[Path]:
    """枚举该工作区**已有**的会话 checkpoint 库（按路径排序；目录不存在 → 空表）。

    只做路径枚举、不读库内容——读会话状态在 `app/platform/commands/session.py`。
    """
    root = sessions_dir(workspace_path)
    if not root.is_dir():
        return []
    return sorted(p / "agent.db" for p in root.iterdir() if (p / "agent.db").is_file())


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
