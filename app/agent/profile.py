"""项目画像（工作区根的 `AGENT.md`）的读取单点——跨会话的"这是什么项目"。

与记忆的分工（docs/LONG_TERM_MEMORY.md §4）：AGENT.md 答**这是什么项目**（目录 / 技术栈 /
主流程 / 怎么跑，随仓库走、可人工维护或由 `/init` 命令生成）；`memory.md` 答**做这个项目时
怎么按约束来**（用户偏好 / 决策 / 约定，落 resource 私有目录）。两通道不混一个文件，
故本模块独立于 `app/agent/memory.py`。

注入时机：**会话第一次启动时读入**（LLMNode 实例内 memo，见 app/agent/nodes.py），不像记忆
那样每轮实时读盘——画像默认慢变，且它是工作区里可随时重读的普通文件（不像记忆是私有结论）。

本模块只依赖 stdlib；路径以传入的 `workspace_path` 为根（与 file_io 沙箱同一根）。
"""
from __future__ import annotations

from pathlib import Path

# 工作区根的项目画像文件名（§4 约定；`/init` 命令生成的就是它）
AGENT_MD_FILENAME = "AGENT.md"

# 注入上限（与 memory.py::MEMORY_INJECT_CAP 同量级）：超长只截断、不做压缩/摘要
AGENT_MD_INJECT_CAP = 8000

# 注入块标题：AGENT.md 由模型/人自由撰写，加一行标题让模型知道这段是什么
_BLOCK_HEADER = "# 项目画像（工作区 AGENT.md）\n\n"


def agent_md_path(workspace_path: str) -> Path:
    """某工作区的项目画像文件路径（工作区根 / AGENT.md）。"""
    return Path(workspace_path) / AGENT_MD_FILENAME


def agent_md_block(workspace_path: str, cap: int = AGENT_MD_INJECT_CAP) -> str:
    """读工作区根的 AGENT.md，返回注入用的正文（带块标题）；没有该文件/内容为空 → `""`。

    超 `cap` 截断并附一行提示——画像本应精简，截断只是兜底。
    """
    path = agent_md_path(workspace_path)
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return ""
    if len(text) > cap:
        text = text[:cap] + f"\n…（AGENT.md 超长已截断，共 {len(text)} 字符；用 read_file 看全文）"
    return _BLOCK_HEADER + text
