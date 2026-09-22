"""app/resource/memory.py 测试：模板播种 + 记忆条目的解析 / 追加 / 按 key 覆写。

import app.resource.memory 会触发 app.agent 包 __init__（需 .env，测试全局前提）；路径断言
monkeypatch app.resource.paths.RESOURCE_ROOT 到 tmp，不碰真实 resource 目录。

重点覆盖"注释里的格式示例不算条目"：模板刻意把示例放进 HTML 注释，而示例正文自己就含一行
`## [m1] …`——朴素按标题正则扫描会把它当真（编号凭空 +1、覆写可能改到示例那行）。
"""
import app.resource.memory as memory
import app.resource.paths as resource


def _workspace(tmp_path, monkeypatch, name="proj"):
    """把 resource 根指到 tmp，返回一个（已播种模板的）工作区路径。"""
    monkeypatch.setattr(resource, "RESOURCE_ROOT", tmp_path / "res")
    ws = tmp_path / name
    memory.ensure_memory_template(str(ws))
    return str(ws)


def test_ensure_memory_template_seeds_once(tmp_path, monkeypatch):
    monkeypatch.setattr(resource, "RESOURCE_ROOT", tmp_path / "res")
    ws = tmp_path / "some" / "proj"

    path = memory.ensure_memory_template(str(ws))

    assert path == resource.memory_root(str(ws)) / "memory.md"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert content.startswith("# 长期记忆")
    assert "user-preference / decision / convention / project-fact" in content

    # 幂等：再次调用不覆盖既有内容
    memory.ensure_memory_template(str(ws))
    assert path.read_text(encoding="utf-8") == content


def test_template_example_in_comment_is_not_an_entry(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)

    assert memory.parse_entries(memory.read_text(ws)) == []
    # 编号从 m1 起，不与注释里的示例（也写着 m1）打架
    assert memory.next_key([]) == "m1"
    assert memory.append_entry(ws, "decision", "第一条真实条目", "2026-09-10").key == "m1"


def test_append_keeps_order_and_preserves_comment(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)

    first = memory.append_entry(ws, "user-preference", "输出用中文", "2026-09-10")
    second = memory.append_entry(ws, "decision", "传输定案 stdio", "2026-09-11")

    assert (first.key, second.key) == ("m1", "m2")
    entries = memory.read_entries(ws)
    assert [e.key for e in entries] == ["m1", "m2"]
    assert [e.content for e in entries] == ["输出用中文", "传输定案 stdio"]
    assert entries[1].type == "decision" and entries[1].date == "2026-09-11"

    raw = memory.read_text(ws)
    assert "<!--" in raw and "-->" in raw  # 模板注释原样保留（追加写原始文本）


def test_overwrite_touches_only_target_span(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)
    memory.append_entry(ws, "decision", "第一条", "2026-09-10")
    memory.append_entry(ws, "decision", "第二条", "2026-09-10")
    memory.append_entry(ws, "decision", "第三条", "2026-09-10")

    updated = memory.overwrite_entry(ws, "m2", "convention", "第二条（已修正）")

    assert updated is not None
    assert (updated.key, updated.type) == ("m2", "convention")
    assert updated.date == "2026-09-10"  # 首次记入日不变
    entries = memory.read_entries(ws)
    assert [e.content for e in entries] == ["第一条", "第二条（已修正）", "第三条"]
    assert [e.key for e in entries] == ["m1", "m2", "m3"]  # 不重排、不重新编号
    raw = memory.read_text(ws)
    assert "## [m1] decision · 2026-09-10\n第一条\n\n## [m2] convention" in raw  # 空行仍在
    assert "追加格式" in raw  # 注释没被覆写误伤


def test_overwrite_unknown_key_keeps_file(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)
    memory.append_entry(ws, "decision", "唯一一条", "2026-09-10")
    before = memory.read_text(ws)

    assert memory.overwrite_entry(ws, "m9", "decision", "不该写进去") is None
    assert memory.read_text(ws) == before


def test_heading_after_comment_close_on_same_line_is_ignored(tmp_path, monkeypatch):
    """注释闭合符后面紧跟的"标题"不在行首，不算条目（`_iter_headings` 的行首守卫）。"""
    text = "# 记忆\n\n<!-- 示例 -->## [m9] decision · 2026-01-01\n真·正文\n"

    assert memory.parse_entries(text) == []


def test_memory_block_empty_when_no_entries(tmp_path, monkeypatch):
    """只有模板、没有真实条目 → 没有可注入的内容（调用方据此跳过该条系统消息）。"""
    ws = _workspace(tmp_path, monkeypatch)

    assert memory.memory_block(ws) == ""


def test_memory_block_injects_visible_text(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)
    memory.append_entry(ws, "decision", "传输定案 stdio", "2026-09-10")

    block = memory.memory_block(ws)

    assert block.startswith("# 长期记忆")  # 文件头即注入块标题
    assert "传输定案 stdio" in block
    assert "<!--" not in block and "追加格式" not in block  # 格式示例不给模型


def test_memory_block_truncates_to_newest_entries(tmp_path, monkeypatch):
    """超 cap：从最新往前装，装不下的旧条目退化成一行"未注入 + 编号"，正文不再注入。"""
    ws = _workspace(tmp_path, monkeypatch)
    memory.append_entry(ws, "decision", "旧" * 200, "2026-09-10")
    memory.append_entry(ws, "decision", "中" * 200, "2026-09-10")
    memory.append_entry(ws, "decision", "新" * 200, "2026-09-10")

    block = memory.memory_block(ws, cap=500)  # 只装得下最新那一条

    assert "## [m3]" in block and "新" * 200 in block
    assert "未注入" in block and "m1" in block and "m2" in block
    assert "中" * 200 not in block and "旧" * 200 not in block


def test_strip_comments_yields_clean_visible_text(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)
    memory.append_entry(ws, "decision", "可见条目", "2026-09-10")

    visible = memory.strip_comments(memory.read_text(ws))

    assert "<!--" not in visible and "追加格式" not in visible
    assert "可见条目" in visible
    assert "\n\n\n" not in visible  # 注释整行去掉后的多余空行已收敛
