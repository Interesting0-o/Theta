"""evaluation/fixtures.py 的单测：录制产物的读写与"任务已改动"指纹比对。

fixture 是层 A 回放的**唯一输入**，读错了会让回放测的变成别的东西；而指纹比对是防止
"陈旧的 fixture 拿着旧问题的答案为新问题背书"——那种静默通过比失败糟得多。

前提：`import evaluation` 会经 __init__ → runner → app.agent.graph，故需要 .env 存在。
"""
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from evaluation import fixtures


@pytest.fixture(autouse=True)
def _isolated_fixture_dir(tmp_path, monkeypatch):
    """fixture 落点指到 tmp——否则测试会写进仓库的 evaluation/fixtures/。"""
    monkeypatch.setattr(fixtures, "FIXTURES_DIR", tmp_path / "fixtures")


def _fixture(task="demo", prompt="做个事", setup=None, replies=None):
    return fixtures.Fixture(
        task=task,
        prompt=prompt,
        setup=setup if setup is not None else {"a.txt": "内容"},
        replies=replies
        if replies is not None
        else [AIMessage(content="开始", tool_calls=[{"name": "read_file", "args": {"path": "a.txt"}, "id": "call_1"}]), AIMessage(content="做完了")],
        model="test-model",
    )


def test_save_then_load_roundtrip_keeps_tool_call_ids():
    """**tool_calls 的 id 一个都不能丢**：ReviewNode 拿它兑现 ToolMessage、提问闸门按它索引。"""
    fixtures.save(_fixture())
    loaded = fixtures.load("demo")

    assert loaded.prompt == "做个事" and loaded.setup == {"a.txt": "内容"}
    assert loaded.model == "test-model" and loaded.recorded_at  # 落盘时自动补时间戳
    assert [m.content for m in loaded.replies] == ["开始", "做完了"]
    assert loaded.replies[0].tool_calls[0]["id"] == "call_1"
    assert loaded.replies[0].tool_calls[0]["args"] == {"path": "a.txt"}
    assert loaded.replies[1].tool_calls == []


def test_saved_file_is_readable_chinese_json():
    """落盘要一眼能读（ensure_ascii=False）：人复查 fixture 时不该看到 \\uXXXX。"""
    path = fixtures.save(_fixture())
    text = path.read_text(encoding="utf-8")

    assert "做个事" in text and "\\u" not in text
    assert json.loads(text)["task"] == "demo"


def test_load_missing_file_says_not_recorded():
    with pytest.raises(fixtures.FixtureError, match="未录制"):
        fixtures.load("never_recorded")


def test_load_broken_json_is_a_fixture_error_not_a_crash():
    path = fixtures.path_for("broken")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 json", encoding="utf-8")

    with pytest.raises(fixtures.FixtureError, match="读不动"):
        fixtures.load("broken")


def test_load_rejects_empty_replies():
    """一条回复都没有 = 录制那次跑废了：当成"录过了"会让回放静默跑成另一回事。"""
    path = fixtures.save(_fixture(replies=[AIMessage(content="占位")]))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["replies"] = []
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(fixtures.FixtureError, match="一条回复都没有"):
        fixtures.load("demo")


def test_load_rejects_malformed_reply():
    path = fixtures.save(_fixture())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["replies"] = ["这不是对象"]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(fixtures.FixtureError, match="必须是对象"):
        fixtures.load("demo")


# ------------------------- 指纹比对 -------------------------


def test_resolve_hits_when_task_is_unchanged():
    fixtures.save(_fixture(prompt="做个事", setup={"a.txt": "内容"}))

    fixture, reason = fixtures.resolve("demo", "做个事", {"a.txt": "内容"})

    assert fixture is not None and reason is None


def test_resolve_reports_missing_fixture_instead_of_raising():
    fixture, reason = fixtures.resolve("demo", "做个事", {})

    assert fixture is None and reason and "未录制" in reason


def test_resolve_flags_changed_prompt():
    """prompt 变了 → 录制下来的输出不再是"对这个问题的回答"，必须重录。"""
    fixtures.save(_fixture(prompt="旧问题"))

    fixture, reason = fixtures.resolve("demo", "新问题", {"a.txt": "内容"})

    assert fixture is None and reason and "prompt" in reason


def test_resolve_flags_changed_setup():
    fixtures.save(_fixture(prompt="做个事", setup={"a.txt": "旧内容"}))

    fixture, reason = fixtures.resolve("demo", "做个事", {"a.txt": "新内容"})

    assert fixture is None and reason and "setup" in reason


def test_stale_reason_is_none_when_everything_matches():
    fixture = _fixture(prompt="做个事", setup={"a.txt": "内容"})
    assert fixtures.stale_reason(fixture, "做个事", {"a.txt": "内容"}) is None


def test_fixture_replies_are_plain_ai_messages():
    """录制只存 content + tool_calls：思考内容本就不进 messages，additional_kwargs 不参与图行为。"""
    fixtures.save(_fixture())
    loaded = fixtures.load("demo")

    assert all(isinstance(m, AIMessage) for m in loaded.replies)
    assert all(not isinstance(m, ToolMessage) for m in loaded.replies)
