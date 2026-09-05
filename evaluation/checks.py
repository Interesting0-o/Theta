"""确定性断言：评估的"评分器"，不依赖 LLM、不花钱。

每个检查是 `fn(result, workspace) -> (passed: bool, detail: str)`，
`result` 为 runner.EvalResult（鸭子类型，本模块不 import runner 以避免环依赖），
`workspace` 为本次任务的工作区路径（Path）。用工厂函数生成带中文标签的 `Check`，
示例任务直接在 tasks.py 里引用这些工厂。

内置检查覆盖四个维度（与评估目标一一对应）：
- 终态：file_content / file_exists —— 任务的最终交付物对不对；
- 审批闸门效率：approvals_between / no_approvals —— 只读任务审批数应为 0；
- 计划：plan_steps_at_least —— 编排工具是否被真正使用；
- 过程健康：terminated —— 正常收尾（无异常、未超轮数），拒绝路径也要能结束。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List

CheckFn = Callable[["object", Path], "tuple[bool, str]"]


@dataclass(frozen=True)
class Check:
    """一个命名断言：label 用于报告展示，fn 做实际判定。"""

    label: str
    fn: CheckFn


@dataclass(frozen=True)
class CheckOutcome:
    """单次检查的结论。"""

    label: str
    passed: bool
    detail: str


# ---------------- 终态：交付物 ----------------

def file_content(rel_path: str, expected: str) -> Check:
    """工作区里 rel_path 的文件内容与 expected 完全一致。"""

    def check(result, workspace: Path) -> tuple[bool, str]:
        target = workspace / rel_path
        if not target.is_file():
            return False, f"文件不存在: {rel_path}"
        actual = target.read_text(encoding="utf-8")
        if actual == expected:
            return True, "内容一致"
        return False, f"内容不符: {actual[:80]!r} != {expected[:80]!r}"

    return Check(f"file_content({rel_path})", check)


def file_exists(rel_path: str) -> Check:
    """工作区里存在 rel_path 文件（不校验内容）。"""

    def check(result, workspace: Path) -> tuple[bool, str]:
        target = workspace / rel_path
        if target.is_file():
            return True, "存在"
        return False, f"文件不存在: {rel_path}"

    return Check(f"file_exists({rel_path})", check)


# ---------------- 审批闸门效率 ----------------

def approvals_between(lo: int, hi: int) -> Check:
    """审批（interrupt）次数落在 [lo, hi]：写任务应 ≥1 且不过多，读任务应为 0。"""

    def check(result, workspace: Path) -> tuple[bool, str]:
        count = len(getattr(result, "interrupts", []))
        if lo <= count <= hi:
            return True, f"审批 {count} 次"
        return False, f"审批 {count} 次，期望 [{lo}, {hi}]"

    return Check(f"approvals_between({lo}, {hi})", check)


def no_approvals() -> Check:
    """零审批：只读/纯计划任务不应触发 interrupt。"""
    return approvals_between(0, 0)


# ---------------- 计划使用 ----------------

def plan_steps_at_least(min_steps: int) -> Check:
    """终态 current_plan 至少 min_steps 步（编排工具确实被调用且写回了 state）。"""

    def check(result, workspace: Path) -> tuple[bool, str]:
        state = getattr(result, "final_state", None) or {}
        steps = state.get("current_plan", []) or []
        if len(steps) >= min_steps:
            return True, f"计划 {len(steps)} 步"
        return False, f"计划仅 {len(steps)} 步，期望 ≥ {min_steps}"

    return Check(f"plan_steps_at_least({min_steps})", check)


# ---------------- 过程健康 ----------------

def terminated() -> Check:
    """正常收尾：无异常、未超最大轮数（拒绝路径也必须能结束而不是死循环）。"""

    def check(result, workspace: Path) -> tuple[bool, str]:
        ok = bool(getattr(result, "ok", False))
        error = getattr(result, "error", None)
        if ok:
            return True, "正常结束"
        return False, f"未正常结束: {error or '未知原因'}"

    return Check("terminated", check)


def run_checks(checks: List[Check], result, workspace: Path) -> List[CheckOutcome]:
    """按顺序跑一组检查，任何检查抛异常都记为该检查未通过（不中断整批）。"""
    outcomes: List[CheckOutcome] = []
    for item in checks:
        try:
            passed, detail = item.fn(result, workspace)
        except Exception as exc:  # noqa: BLE001 —— 检查自身 bug 不应吞掉整批评估
            passed, detail = False, f"检查异常: {exc!r}"
        outcomes.append(CheckOutcome(item.label, passed, detail))
    return outcomes
