"""计划纯状态转换函数（app/agent/utils.py）的单测（不经过 FastMCP / 模型）。

注意：import app.agent.utils 会触发 app/agent/__init__ → graph → model，
要求 .env 存在（CLAUDE.md 前提）。
"""
from app.agent.utils import (
    apply_step_status,
    empty_plan,
    plan_snapshot,
    steps_to_plan,
)


def test_steps_to_plan_assigns_ids_and_pending():
    plan = steps_to_plan(["读 A", "改 B", "测 C"])
    assert plan == [
        {"id": "1", "task": "读 A", "status": "pending"},
        {"id": "2", "task": "改 B", "status": "pending"},
        {"id": "3", "task": "测 C", "status": "pending"},
    ]


def test_steps_to_plan_skips_blank():
    plan = steps_to_plan(["   ", "x"])
    assert plan == [{"id": "1", "task": "x", "status": "pending"}]


def test_apply_step_status_marks_done_without_mutating_original():
    plan = steps_to_plan(["a", "b"])
    new, ok, message = apply_step_status(plan, "1", "done")
    assert ok
    assert message == "步骤 1 已标记为 done"
    assert new[0]["status"] == "done"
    assert new[1]["status"] == "pending"
    assert plan[0]["status"] == "pending"  # 原列表不被修改


def test_apply_step_status_in_progress():
    plan = steps_to_plan(["a"])
    new, ok, _ = apply_step_status(plan, "1", "in_progress")
    assert ok
    assert new[0]["status"] == "in_progress"


def test_apply_step_status_unknown_id_keeps_plan():
    plan = steps_to_plan(["a"])
    new, ok, message = apply_step_status(plan, "99", "done")
    assert not ok
    assert new is plan
    assert "不存在" in message


def test_apply_step_status_invalid_status():
    plan = steps_to_plan(["a"])
    new, ok, _ = apply_step_status(plan, "1", "nope")
    assert not ok
    assert new is plan


def test_empty_plan_returns_empty():
    assert empty_plan() == []


def test_plan_snapshot_shows_marks():
    plan = steps_to_plan(["a", "b"])
    plan, ok, _ = apply_step_status(plan, "2", "done")
    text = plan_snapshot(plan)
    assert "- [ ] 1. a" in text
    assert "- [x] 2. b" in text
