"""汇总渲染：把 run_suite 的原始结果 + checks 结论压成纯文本表格。

无第三方依赖（不引 rich，保持骨架零成本）。表格列：
任务 | 检查通过数 | 审批次数 | 轮数 | 耗时 | 失败详情。
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from .checks import CheckOutcome, run_checks
from .runner import EvalResult
from .tasks import Task


def render_task(
    task: Task,
    result: EvalResult,
    workspace: Path,
) -> Tuple[List[CheckOutcome], List[str]]:
    """跑一个任务的检查项，返回 (检查结论, 行文本列表)。"""
    outcomes = run_checks(task.checks or [], result, workspace)
    passed = sum(1 for o in outcomes if o.passed)

    line = (
        f"{task.name:<20} | {passed}/{len(outcomes):<7} | "
        f"{len(result.interrupts):<4} | {result.rounds:<4} | "
        f"{result.elapsed_seconds:>6.1f}s | "
        f"{'OK' if result.ok else result.error or 'FAIL'}"
    )
    details = [line]
    for outcome in outcomes:
        if not outcome.passed:
            details.append(f"      ✗ {outcome.label}: {outcome.detail}")
    return outcomes, details


def print_report(
    runs: List[Tuple[Task, EvalResult, Path]],
) -> None:
    """打印整批评估的汇总表 + 通过率。"""
    header = f"{'任务':<22} | {'检查':<7} | {'审批':<4} | {'轮数':<4} | {'耗时':>8} | 结论"
    print(header)
    print("-" * len(header))

    total_checks = passed_checks = 0
    for task, result, workspace in runs:
        outcomes, lines = render_task(task, result, workspace)
        total_checks += len(outcomes)
        passed_checks += sum(1 for o in outcomes if o.passed)
        for line in lines:
            print(line)

    print("-" * len(header))
    rate = (passed_checks / total_checks * 100) if total_checks else 0.0
    print(f"共 {len(runs)} 个任务，检查通过 {passed_checks}/{total_checks}（{rate:.0f}%）")
