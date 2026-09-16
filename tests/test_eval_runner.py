"""evaluation/runner.py 的单测：回放模式的 SKIP 装配 + 工作区清理顺序。

这里只覆盖**不需要建图**的路径（fixture 不可用 → 压根不建图、不拉 MCP），所以跑得飞快。
真图回放由 `python -m evaluation`（零成本）与 `tests/test_command_review_e2e.py` 覆盖。

**清理顺序是本文件的重点**：检查项（checks.py）在报告阶段才跑、且要读工作区里的终态文件，
所以 `run_suite` **不能**自己删工作区——删早了文件类断言恒失败。这个 bug 2026-09-16 实跑时
才暴露（`--keep-workspace` 8/8、默认 6/8），值得一条回归钉子。

前提：import evaluation 会经 __init__ → runner → app.agent.graph，故需要 .env 存在。
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage

from evaluation import fixtures
from evaluation.runner import cleanup_workspaces, run_suite, skipped_result
from evaluation.tasks import Task

MISSING = "never_recorded_xyz"


def _task(name=MISSING, prompt="问一句"):
    return Task(name=name, prompt=prompt)


def test_run_suite_skips_unrecorded_task_without_building_a_graph(tmp_path):
    """未录制 → SKIP，且**不建图**（否则每个没录过的任务都要拉一组 MCP 子进程）。"""
    (task, result, workspace), = asyncio.run(run_suite([_task()], workspace_root=tmp_path))

    assert result.skipped and not result.ok
    assert result.skip_reason and "未录制" in result.skip_reason
    assert workspace is None  # 跳过就没有工作区
    assert list(tmp_path.iterdir()) == []  # 也没留下临时目录


def test_run_suite_skips_task_whose_fixture_went_stale(tmp_path, monkeypatch):
    """任务改了（prompt 与录制时不一致）→ SKIP 并要求重录：陈旧的 fixture 不能为新问题背书。"""
    monkeypatch.setattr(fixtures, "FIXTURES_DIR", tmp_path / "fixtures")
    fixtures.save(
        fixtures.Fixture(
            task="stale_demo",
            prompt="旧问题",
            setup={},
            replies=[AIMessage(content="旧的答复")],
        )
    )

    (_, result, workspace), = asyncio.run(
        run_suite([_task(name="stale_demo", prompt="新问题")], workspace_root=tmp_path)
    )

    assert result.skipped and workspace is None
    assert "prompt" in (result.skip_reason or "")


def test_skipped_result_carries_the_reason():
    result = skipped_result(_task(), "未录制：某文件不存在")

    assert result.skipped and result.task_name == MISSING
    assert result.skip_reason == "未录制：某文件不存在"
    assert result.rounds == 0 and result.interrupt_count == 0


def test_cleanup_workspaces_removes_them(tmp_path):
    keep_dir = tmp_path / "ws-a"
    keep_dir.mkdir()
    runs = [(_task(), skipped_result(_task(), "x"), keep_dir), (_task(), skipped_result(_task(), "y"), None)]

    cleanup_workspaces(runs)

    assert not keep_dir.exists()
    assert tmp_path.exists()  # 只删工作区本身，不动它的父目录


def test_cleanup_workspaces_tolerates_missing_dir(tmp_path):
    """早已不存在的目录（人工删过 / 上次清理过）不该让收尾炸掉。"""
    cleanup_workspaces([(_task(), skipped_result(_task(), "x"), tmp_path / "gone")])


def test_run_suite_rejects_unknown_mode(tmp_path):
    with pytest.raises(ValueError, match="未知的评估模式"):
        asyncio.run(run_suite([_task()], workspace_root=tmp_path, mode="nonsense"))
