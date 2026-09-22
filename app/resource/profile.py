"""项目画像：工作区根 `AGENT.md` 的读取与注入块（资源层里的"认知资源"一支）。

答的是"**这是什么项目**"（目录 / 技术栈 / 主流程 / 怎么跑），由 `/init` 命令生成、随仓库走
——与长期记忆（`app/resource/memory.py`，私有、跨会话）是两条独立通道，边界见
docs/LONG_TERM_MEMORY.md §1。

**位置史**：`app/agent/profile.py`（独立模块）→ 2026-09-12 并进 `LLMNode`（当时的理由是
"生产侧只有本节点一个消费者"）→ 2026-09-21 剥出到资源层。前一次并入的理由后来站不住了
（`memory_block` 同样只被 `LLMNode` 调用，却一直住在自己的模块里），剥出的判据是
docs/ARCHITECTURE.md §4 的 A/B/C：读 agent 之外的文件、转成内部表示。

**留在 `LLMNode` 的**是"读一次还是每轮读"这个决定（实例内 memo，见 `LLMNode._read_profile`）
——那是节点的"这一拍做什么"，不是资源访问。本模块只提供**无状态**的读盘与截断。
"""
from __future__ import annotations

from app.config import TEXT_BUDGET_CHARS
from app.resource.paths import agent_md_path

# 画像注入上限：超长只截断、不做压缩/摘要。值归 app/config.py::TEXT_BUDGET_CHARS（同族一处出处）。
AGENT_MD_INJECT_CAP = TEXT_BUDGET_CHARS

# 注入块标题：AGENT.md 由模型/人自由撰写，加一行标题让模型知道这段是什么
PROFILE_BLOCK_HEADER = "# 项目画像（工作区 AGENT.md）\n\n"


def agent_md_block(workspace_path: str, cap: int = AGENT_MD_INJECT_CAP) -> str:
    """读工作区根的 AGENT.md，返回注入用的正文（带块标题）；没有该文件/内容为空 → `""`。

    超 `cap` 截断并附一行提示——画像本应精简，截断只是兜底。
    """
    path = agent_md_path(workspace_path)  # 落点归 paths，别在这里拼（见 paths.py 的"规定"）
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return ""
    if len(text) > cap:
        text = text[:cap] + f"\n…（AGENT.md 超长已截断，共 {len(text)} 字符；用 read_file 看全文）"
    return PROFILE_BLOCK_HEADER + text
