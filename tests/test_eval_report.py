"""evaluation/report.py 的单测：汇总表的**通过判据**与 CLI 退出码。

退出码是层 A 回放"能进 CI"的兑现点（零成本但不拦回归等于白跑），所以这里把三条判据逐个钉住：
检查红 / 任务报错但一条检查都没有 / 一个任务都没跑成。纯函数 + 桩数据，不建图、不花钱。
"""
import asyncio
import sys
from pathlib import Path

from evaluation.__main__ import main
from evaluation.checks import file_exists
from evaluation.report import print_report
from evaluation.runner import EvalResult, skipped_result
from evaluation.tasks import Task


def _task(name: str = "t", checks: list | None = None) -> Task:
    return Task(name=name, prompt="x", checks=checks if checks is not None else [])


def _result(task: Task, **kwargs) -> EvalResult:
    base = {"task_name": task.name, "ok": True}
    return EvalResult(**{**base, **kwargs})


def _run(tmp_path: Path, task: Task, result: EvalResult):
    return [(task, result, tmp_path)]


def test_all_green_returns_true_and_says_so(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    task = _task(checks=[file_exists("a.txt")])

    assert print_report(_run(tmp_path, task, _result(task))) is True
    assert "结论：通过。" in capsys.readouterr().out


def test_failing_check_returns_false(tmp_path, capsys):
    task = _task(checks=[file_exists("missing.txt")])

    assert print_report(_run(tmp_path, task, _result(task))) is False
    assert "结论：不通过" in capsys.readouterr().out


def test_graph_error_with_no_checks_still_fails(tmp_path, capsys):
    """图/运行体报错的任务往往一条检查都没有——光看通过率（0/0）会把它漏成绿色。"""
    task = _task()
    result = _result(task, ok=False, error="boom")

    assert print_report(_run(tmp_path, task, result)) is False
    assert "结论：不通过" in capsys.readouterr().out


def test_all_skipped_is_not_a_pass(tmp_path, capsys):
    """一个任务都没跑成（fixture 缺失/陈旧）不能报绿——那正是要人看见的状态。"""
    task = _task()
    runs = [(task, skipped_result(task, "未录制"), None)]

    assert print_report(runs) is False
    assert "--record" in capsys.readouterr().out


def test_partial_skip_does_not_turn_a_green_run_red(tmp_path):
    """SKIP 不是模型的锅，同"不计入通过率"的口径：只要跑起来的都绿，整批就算通过。"""
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    ok_task = Task(name="ok", prompt="x", checks=[file_exists("a.txt")])
    skipped_task = _task(name="skipped")

    runs = [
        (ok_task, _result(ok_task), tmp_path),
        (skipped_task, skipped_result(skipped_task, "未录制"), None),
    ]
    assert print_report(runs) is True


def test_unknown_task_name_is_a_usage_error(monkeypatch, capsys):
    """`--task` 打错不能静默退出 0——用法错误给 2（与 argparse 同惯例）。"""
    monkeypatch.setattr(sys, "argv", ["evaluation", "--task", "no_such_task"])

    assert asyncio.run(main()) == 2
    assert "未找到任务" in capsys.readouterr().out
