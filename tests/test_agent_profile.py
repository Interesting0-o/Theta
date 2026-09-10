"""app/agent/profile.py：项目画像（工作区根 AGENT.md）的读取与截断。

`import app.agent.*` 经包 __init__ 触发 get_settings()，需 .env 存在（CLAUDE.md 前提）；
本模块自身只读文件系统，不碰 resource 目录。
"""
import app.agent.profile as profile


def test_agent_md_path_is_workspace_root(tmp_path):
    assert profile.agent_md_path(str(tmp_path)) == tmp_path / "AGENT.md"


def test_agent_md_block_missing_file_returns_empty(tmp_path):
    assert profile.agent_md_block(str(tmp_path)) == ""


def test_agent_md_block_reads_and_tags_header(tmp_path):
    (tmp_path / "AGENT.md").write_text("  # Theta\n\nLangGraph 编码助手。  ", encoding="utf-8")

    block = profile.agent_md_block(str(tmp_path))

    assert block.startswith("# 项目画像（工作区 AGENT.md）")  # 加标签，模型知道这段是什么
    assert "LangGraph 编码助手。" in block
    assert block.endswith("LangGraph 编码助手。")  # 首尾空白已 trim


def test_agent_md_block_blank_file_returns_empty(tmp_path):
    """只有空白的画像不注入（免得多塞一条空系统消息）。"""
    (tmp_path / "AGENT.md").write_text("   \n\n\t", encoding="utf-8")

    assert profile.agent_md_block(str(tmp_path)) == ""


def test_agent_md_block_truncates_over_cap(tmp_path):
    (tmp_path / "AGENT.md").write_text("长" * 500, encoding="utf-8")

    block = profile.agent_md_block(str(tmp_path), cap=100)

    body = block.split("\n\n", 1)[1]  # 去掉块标题（标题里也有字，别混进字符计数）
    assert body.startswith("长" * 100)  # 截到 cap
    assert "已截断" in body
    assert "共 500 字符" in body  # 报的是原长，不是截断后的长度
