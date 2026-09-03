"""编排工具核心逻辑的单测（不经过 FastMCP / 模型）。

编排工具 create_plan / update_plan_step / clear_plan 的核心逻辑由工具自身实现
（见 app/agent/tools.py）。本文件通过 `.func`（langchain @tool 暴露的底层原函数）
直接调用工具：跳过 schema/注入机制，把 state 与 tool_call_id 当普通参数传入，
仅验证逻辑与返回切片，与节点/图解耦。

注意：import app.agent.tools 会触发 app/agent/__init__ → graph → model 及
模块顶层 get_settings()/TavilySearch()，要求 .env 存在（CLAUDE.md 前提）。
"""
from app.agent.tools import clear_plan, create_plan, update_plan_step


def _call(tool, *args):
    return tool.func(*args)


def _plan(items):
    return [{"id": str(i + 1), "task": t, "status": "pending"} for i, t in enumerate(items)]


def _message(out):
    return out["messages"][0].content


def test_create_plan_assigns_ids_and_pending():
    out = _call(create_plan, ["读 A", "改 B", "测 C"], {"current_plan": []}, "c1")
    assert out["current_plan"] == _plan(["读 A", "改 B", "测 C"])
    assert out["current_plan"][0]["status"] == "pending"
    assert "已生成计划（3 步）" in _message(out)
    assert "读 A" in _message(out)


def test_create_plan_skips_blank():
    out = _call(create_plan, ["   ", "x"], {"current_plan": []}, "c1")
    assert out["current_plan"] == [{"id": "1", "task": "x", "status": "pending"}]


def test_update_marks_done_without_mutating_input_state():
    src_plan = _plan(["a", "b"])
    state = {"current_plan": list(src_plan)}  # state 副本，供工具读
    out = _call(update_plan_step, "1", "done", state, "c2")
    assert out["current_plan"][0]["status"] == "done"
    assert out["current_plan"][1]["status"] == "pending"
    # 原 state 里的列表不被修改
    assert state["current_plan"][0]["status"] == "pending"
    assert "步骤 1 已标记为 done" in _message(out)
    # 快照体现打勾
    assert "- [x] 1. a" in _message(out)


def test_update_in_progress():
    state = {"current_plan": _plan(["a"])}
    out = _call(update_plan_step, "1", "in_progress", state, "c2")
    assert out["current_plan"][0]["status"] == "in_progress"


def test_update_unknown_step_keeps_plan():
    state = {"current_plan": _plan(["a"])}
    out = _call(update_plan_step, "99", "done", state, "c2")
    assert "current_plan" not in out  # 未返回计划切片 = 计划保持不变
    assert "步骤 99 不存在" in _message(out)


def test_update_invalid_status():
    state = {"current_plan": _plan(["a"])}
    out = _call(update_plan_step, "1", "nope", state, "c2")
    assert "current_plan" not in out
    assert "非法状态" in _message(out)


def test_clear_plan_returns_empty():
    out = _call(clear_plan, {"current_plan": _plan(["a"])}, "c3")
    assert out["current_plan"] == []
    assert "已清空全部计划" in _message(out)
