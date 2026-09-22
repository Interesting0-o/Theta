"""项目画像（工作区根 AGENT.md）的读取与截断——`app/resource/profile.py::agent_md_block`。

位置史：`app/agent/profile.py` → 2026-09-12 并入 `LLMNode`（静态方法）→ 2026-09-21 剥到资源层
（判据见 docs/ARCHITECTURE.md §4 的 A/B/C），用例随之改为直接调模块函数（不必构造 LLMNode
实例，也仍不碰 resource 目录）。

`import app.agent.nodes` 会经包 `__init__`（→ graph → model）触发 `get_settings()`，需 .env
存在（CLAUDE.md 前提）。
"""
from app.resource.paths import AGENT_MD_FILENAME
from app.resource.profile import agent_md_block


def test_agent_md_filename_is_workspace_root_agent_md():
    """画像文件名是单一常量（`/init` 生成的就是它），改这里等于改全网约定。"""
    assert AGENT_MD_FILENAME == "AGENT.md"


def test_agent_md_block_missing_file_returns_empty(tmp_path):
    assert agent_md_block(str(tmp_path)) == ""


def test_agent_md_block_reads_and_tags_header(tmp_path):
    (tmp_path / "AGENT.md").write_text("  # Theta\n\nLangGraph 编码助手。  ", encoding="utf-8")

    block = agent_md_block(str(tmp_path))

    assert block.startswith("# 项目画像（工作区 AGENT.md）")  # 加标签，模型知道这段是什么
    assert "LangGraph 编码助手。" in block
    assert block.endswith("LangGraph 编码助手。")  # 首尾空白已 trim


def test_agent_md_block_blank_file_returns_empty(tmp_path):
    """只有空白的画像不注入（免得多塞一条空系统消息）。"""
    (tmp_path / "AGENT.md").write_text("   \n\n\t", encoding="utf-8")

    assert agent_md_block(str(tmp_path)) == ""


def test_agent_md_block_truncates_over_cap(tmp_path):
    (tmp_path / "AGENT.md").write_text("长" * 500, encoding="utf-8")

    block = agent_md_block(str(tmp_path), cap=100)

    body = block.split("\n\n", 1)[1]  # 去掉块标题（标题里也有字，别混进字符计数）
    assert body.startswith("长" * 100)  # 截到 cap
    assert "已截断" in body
    assert "共 500 字符" in body  # 报的是原长，不是截断后的长度
