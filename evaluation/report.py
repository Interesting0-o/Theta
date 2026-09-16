"""汇总渲染：把 run_suite 的原始结果 + checks 结论压成纯文本表格。

无第三方依赖（不引 rich，保持骨架零成本）。表格列：
任务 | 检查通过数 | 审批次数 | 轮数 | 耗时 | 失败详情。

跳过的任务（回放模式下 fixture 缺失/陈旧）在表里标 SKIP，**不计入通过率**——它不是模型的锅，
但也不能藏，末尾再单列一遍原因。
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from .checks import CheckOutcome, run_checks
from .runner import MODE_LIVE, MODE_RECORD, MODE_REPLAY, EvalResult
from .tasks import Task

# 报告抬头：**跑的是哪一层必须一眼看到**——层 A 全绿不等于评估过了（它测不了 prompt 改动）。
MODE_TITLES = {
    MODE_REPLAY: (
        "层 A · 回放：模型换成脚本，其余全是真的（零 LLM 成本）。"
        "它断言「除模型之外的一切」没变——**改了 prompt / 工具 docstring 请跑 --live**。"
    ),
    MODE_LIVE: "层 B · 真跑：调用真实模型（花 token）。这是 prompt 改动唯一的验证手段。",
    MODE_RECORD: "层 B · 真跑 + 录制：把模型输出存成 fixture，供日后 --replay 免费回归。",
}


def render_task(
    task: Task,
    result: EvalResult,
    workspace: Optional[Path],
) -> Tuple[List[CheckOutcome], List[str]]:
    """跑一个任务的检查项，返回 (检查结论, 行文本列表)；跳过的任务不跑检查。"""
    if result.skipped:
        line = f"{task.name:<20} | {'SKIP':<7} | {'-':<4} | {'-':<4} | {'-':>7} | {result.skip_reason}"
        return [], [line]

    outcomes = run_checks(task.checks or [], result, workspace or Path("."))
    passed = sum(1 for o in outcomes if o.passed)

    line = (
        f"{task.name:<20} | {passed}/{len(outcomes):<7} | "
        f"{result.interrupt_count:<4} | {result.rounds:<4} | "
        f"{result.elapsed_seconds:>6.1f}s | "
        f"{'OK' if result.ok else result.error or 'FAIL'}"
    )
    details = [line]
    for outcome in outcomes:
        if not outcome.passed:
            details.append(f"      ✗ {outcome.label}: {outcome.detail}")
    return outcomes, details


def print_report(
    runs: List[Tuple[Task, EvalResult, Optional[Path]]],
    mode: str = MODE_REPLAY,
) -> None:
    """打印整批评估的汇总表 + 通过率。"""
    print(MODE_TITLES.get(mode, mode))
    print()

    header = f"{'任务':<22} | {'检查':<7} | {'审批':<4} | {'轮数':<4} | {'耗时':>8} | 结论"
    print(header)
    print("-" * len(header))

    total_checks = passed_checks = 0
    skipped: List[Tuple[Task, EvalResult]] = []
    for task, result, workspace in runs:
        if result.skipped:
            skipped.append((task, result))
        outcomes, lines = render_task(task, result, workspace)
        total_checks += len(outcomes)
        passed_checks += sum(1 for o in outcomes if o.passed)
        for line in lines:
            print(line)

    print("-" * len(header))
    checked = len(runs) - len(skipped)
    rate = (passed_checks / total_checks * 100) if total_checks else 0.0
    print(
        f"共 {len(runs)} 个任务：跑了 {checked}、跳过 {len(skipped)}；"
        f"检查通过 {passed_checks}/{total_checks}（{rate:.0f}%）"
    )
    if skipped:
        print()
        print("跳过的任务（不计入通过率）：")
        for task, result in skipped:
            print(f"  - {task.name}: {result.skip_reason}")
