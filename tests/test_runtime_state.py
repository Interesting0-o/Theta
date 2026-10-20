"""RUNTIME_STATE 内联 + 用量日志（Phase 1，2026-10-20）。

- 动态状态内联到当前 user message 尾部，不进而稳定系统前缀；
- Human 有 id 时写回 state（下一次请求前缀一致）；按标记替换，不重复追加；
- 动态状态清空时把旧块剥掉；
- 没有 Human / content 非 str 时退回尾部 SystemMessage（备选路径）；
- `usage_fields` / `llm_usage` 日志读取 cache 命中字段。

需要导出 WORKSPACE_PATH（app.agent 包 __init__ 会 import graph→mcp 链）。
"""
import asyncio
import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.nodes import LLMNode


class _CaptureModel:
    def __init__(self):
        self.seen: list = []
        self.reply = AIMessage(content="ok")

    async def ainvoke(self, messages):
        self.seen = list(messages)
        return self.reply


def _run(node, state):
    return asyncio.run(node(state))


def _last_human(model) -> HumanMessage:
    return next(m for m in reversed(model.seen) if isinstance(m, HumanMessage))


def _plan(task="做事"):
    return [{"id": "1", "task": task, "status": "pending"}]


# ---------------------- 内联 / 写回 ----------------------


def test_runtime_state_inlined_and_written_back_with_id(tmp_path):
    model = _CaptureModel()
    node = LLMNode(model=model, workspace_path=str(tmp_path), attach_images=False)
    human = HumanMessage(content="用户原话", id="h1")
    state = {"messages": [human], "current_plan": _plan()}

    out = _run(node, state)

    # Human 有 id → 更新后的 Human 写回 state；reducer 会按 id 原位替换
    assert len(out["messages"]) == 2
    updated = out["messages"][0]
    assert isinstance(updated, HumanMessage) and updated.id == "h1"
    assert updated.content.startswith("用户原话")
    assert "<RUNTIME_STATE" in updated.content
    assert "# 当前任务:" in updated.content and "做事" in updated.content
    assert out["messages"][1] is model.reply

    # 模型看到的最后一条 Human 与写回的一致
    assert _last_human(model).content == updated.content
    # 调用方传入的 state 不被原地修改
    assert human.content == "用户原话"


def test_runtime_state_replaced_not_duplicated(tmp_path):
    model = _CaptureModel()
    node = LLMNode(model=model, workspace_path=str(tmp_path), attach_images=False)
    old = (
        "用户原话\n\n---\n<RUNTIME_STATE version=\"1\">\n"
        "# 当前任务:\n1. 旧任务 当前状态为 待处理\n</RUNTIME_STATE>"
    )
    state = {
        "messages": [HumanMessage(content=old, id="h1")],
        "current_plan": _plan("新任务"),
    }

    out = _run(node, state)
    content = out["messages"][0].content

    assert content.count("<RUNTIME_STATE") == 1
    assert content.count("</RUNTIME_STATE>") == 1
    assert "新任务" in content
    assert "旧任务" not in content


def test_runtime_state_removed_when_empty(tmp_path):
    model = _CaptureModel()
    node = LLMNode(model=model, workspace_path=str(tmp_path), attach_images=False)
    old = (
        "用户原话\n\n---\n<RUNTIME_STATE version=\"1\">\n"
        "# 当前任务:\n1. 旧任务 当前状态为 待处理\n</RUNTIME_STATE>"
    )
    state = {"messages": [HumanMessage(content=old, id="h1")]}

    out = _run(node, state)

    assert out["messages"][0].content == "用户原话"
    assert "<RUNTIME_STATE" not in _last_human(model).content


def test_runtime_state_falls_back_to_trailing_system_without_human(tmp_path):
    model = _CaptureModel()
    node = LLMNode(model=model, workspace_path=str(tmp_path), attach_images=False)
    state = {"messages": [], "current_plan": _plan()}

    out = _run(node, state)

    assert out["messages"] == [model.reply]  # 没有 Human 可写回
    assert isinstance(model.seen[-1], SystemMessage)
    assert "<RUNTIME_STATE" in model.seen[-1].content


def test_runtime_state_worker_has_plan_but_no_session_context(tmp_path):
    model = _CaptureModel()
    node = LLMNode(
        model=model,
        workspace_path=str(tmp_path),
        attach_images=False,
        inject_session_context=False,
    )
    state = {"messages": [HumanMessage(content="hi")], "current_plan": _plan()}

    _run(node, state)
    human = _last_human(model).content

    assert "# 当前任务:" in human and "做事" in human
    assert "# 长期记忆" not in human
    assert "# 项目画像" not in human


# ---------------------- 用量字段 / 日志 ----------------------


def _reply_with_usage():
    return AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 5,
            "total_tokens": 105,
            "input_token_details": {"cache_read": 80},
        },
        response_metadata={
            "token_usage": {
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 20,
            }
        },
    )


def test_usage_fields_extracts_cache_metrics():
    fields = LLMNode.usage_fields(_reply_with_usage())
    assert fields == {
        "input_tokens": 100,
        "output_tokens": 5,
        "cache_read": 80,
        "cache_hit": 80,
        "cache_miss": 20,
    }


def test_usage_fields_tolerates_missing_metadata():
    assert LLMNode.usage_fields(AIMessage(content="ok")) == {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read": None,
        "cache_hit": None,
        "cache_miss": None,
    }


def test_llm_usage_is_logged(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="app.agent.nodes")

    class _UsageModel(_CaptureModel):
        async def ainvoke(self, messages):
            self.seen = list(messages)
            return _reply_with_usage()

    node = LLMNode(model=_UsageModel(), workspace_path=str(tmp_path), attach_images=False)
    _run(node, {"messages": [HumanMessage(content="hi")], "session_id": "s1"})

    text = caplog.text
    assert "llm_usage" in text
    assert "input=100" in text and "output=5" in text
    assert "cache_read=80" in text and "cache_hit=80" in text and "cache_miss=20" in text
    assert "session=s1" in text
