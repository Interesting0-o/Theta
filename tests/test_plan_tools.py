"""编排工具核心逻辑的单测（不经过 FastMCP / 模型）。

编排工具 create_plan / update_plan_step / clear_plan 的核心逻辑由工具自身实现
（见 app/agent/tools.py）。本文件通过 `.func`（langchain @tool 暴露的底层原函数）
直接调用工具：跳过 schema/注入机制，把 state 与 tool_call_id 当普通参数传入，
仅验证逻辑与返回切片，与节点/图解耦。

注意：import app.agent.tools 会触发 app/agent/__init__ → graph → model 及
模块顶层 get_settings()/TavilySearch()，要求 .env 存在（CLAUDE.md 前提）。
"""
from pathlib import Path

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


def test_orchestrate_tools_only_return_declared_slice_keys():
    """**契约用例**：编排工具返回的切片键必须都被 OrchestrateNode 认。

    背景（`nodes.py::_EXTRA_SLICE_KEYS` 上方那段）：执行器只合并 `messages` / `current_plan` /
    `_EXTRA_SLICE_KEYS` 里列的键，**白名单之外的键一律静默丢弃**。而生产者（本模块的各编排工具）
    并不 import 那张白名单——所以"工具开始返回一个新切片、或白名单被删掉一项"的症状是**静默失效**
    （回执正常、state 没生效）。这条用例把两侧钉在一起：扫 `app/agent/tools.py` 里所有
    `return {…}` 的键，断言它们 ⊆ 白名单。

    用 AST 扫而不是真调用工具：真调用要造 state / workspace / 真技能目录，而这里要守的是**形状**
    （键名），不是行为——行为另有各工具的用例。
    """
    import ast

    from app.agent.nodes import _EXTRA_SLICE_KEYS

    tools_py = Path(__file__).resolve().parents[1] / "app" / "agent" / "tools.py"
    tree = ast.parse(tools_py.read_text(encoding="utf-8"))
    returned: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            returned |= {
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }

    allowed = {"messages", "current_plan", *_EXTRA_SLICE_KEYS}
    assert returned <= allowed, (
        f"工具有返回白名单之外的切片键 {sorted(returned - allowed)}——"
        f"要么改 _EXTRA_SLICE_KEYS（+ state.py），要么那是个写错的键"
    )
