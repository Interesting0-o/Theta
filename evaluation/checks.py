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

import re
import subprocess
import sys
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


def files_exist(*rel_paths: str) -> Check:
    """工作区里存在全部给定文件（用于多文件交付物）。"""

    def check(result, workspace: Path) -> tuple[bool, str]:
        missing = [p for p in rel_paths if not (workspace / p).is_file()]
        if not missing:
            return True, f"存在 {len(rel_paths)} 个文件"
        return False, f"缺少文件: {missing[:6]}"

    return Check(f"files_exist({', '.join(rel_paths)})", check)


def pytest_passes(test_path: str = "tests", timeout: int = 120) -> Check:
    """在任务工作区跑 `python -m pytest <test_path>`，要求退出码 0。

    这是"多文件功能开发"的回归闸门：模型必须交付能跑的测试，且功能确实通过。
    用评估器自己的解释器（`sys.executable`），不依赖模型在终端里是否找对了 python。
    """

    def check(result, workspace: Path) -> tuple[bool, str]:
        target = workspace / test_path
        if not target.exists():
            return False, f"测试路径不存在: {test_path}"
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", str(target), "-q"],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"pytest 超时（>{timeout}s）"
        if proc.returncode == 0:
            return True, "pytest 通过"
        output = (proc.stdout + "\n" + proc.stderr).strip().splitlines()
        tail = "\n".join(output[-8:])
        return False, f"pytest 退出码 {proc.returncode}: {tail[-500:]}"

    return Check(f"pytest_passes({test_path})", check)


def file_contains(rel_path: str, *needles: str) -> Check:
    """文件内容包含全部给定子串（顺序无关）。用于"交付物必须写到某些事实"。"""

    def check(result, workspace: Path) -> tuple[bool, str]:
        target = workspace / rel_path
        if not target.is_file():
            return False, f"文件不存在: {rel_path}"
        text = target.read_text(encoding="utf-8")
        missing = [n for n in needles if n not in text]
        if not missing:
            return True, f"包含 {len(needles)} 个片段"
        return False, f"缺少片段: {missing[:5]}"

    return Check(f"file_contains({rel_path})", check)


def file_matches(rel_path: str, pattern: str) -> Check:
    """文件内容匹配正则（`re.search`，多行）。用于计数等格式宽松的断言。"""

    def check(result, workspace: Path) -> tuple[bool, str]:
        target = workspace / rel_path
        if not target.is_file():
            return False, f"文件不存在: {rel_path}"
        text = target.read_text(encoding="utf-8")
        if re.search(pattern, text, flags=re.MULTILINE):
            return True, f"匹配 /{pattern}/"
        return False, f"未匹配 /{pattern}/"

    return Check(f"file_matches({rel_path}, {pattern})", check)


# ---------------- 轨迹：工具调用序列 ----------------

def _tool_call_events(result) -> list:
    return [
        event
        for event in getattr(result, "trace", []) or []
        if event.get("kind") == "ai_tool_calls"
    ]


def tool_called(name: str, min_times: int = 1) -> Check:
    """轨迹里至少调用了 name 工具 min_times 次。"""

    def check(result, workspace: Path) -> tuple[bool, str]:
        count = sum(
            1
            for event in _tool_call_events(result)
            for call in event.get("tool_calls", [])
            if call.get("name") == name
        )
        if count >= min_times:
            return True, f"{name} 调用 {count} 次"
        return False, f"{name} 调用 {count} 次，期望 ≥ {min_times}"

    return Check(f"tool_called({name})", check)


def tool_call_count_between(name: str, lo: int, hi: int) -> Check:
    """轨迹里 name 工具调用次数落在 [lo, hi]。"""

    def check(result, workspace: Path) -> tuple[bool, str]:
        count = sum(
            1
            for event in _tool_call_events(result)
            for call in event.get("tool_calls", [])
            if call.get("name") == name
        )
        if lo <= count <= hi:
            return True, f"{name} 调用 {count} 次"
        return False, f"{name} 调用 {count} 次，期望 [{lo}, {hi}]"

    return Check(f"tool_call_count_between({name})", check)


def failed_tool_seen(name: str | None = None) -> Check:
    """轨迹里出现过失败的工具结果（可选限定工具名）。

    失败判定来自 `ToolNode` 的结构化戳（`additional_kwargs.error_type`）或审批拒绝；
    不是解析模型正文。
    """

    def check(result, workspace: Path) -> tuple[bool, str]:
        hits = [
            event
            for event in getattr(result, "trace", []) or []
            if event.get("kind") == "tool_result"
            and event.get("status") in {"failed", "denied"}
            and (name is None or event.get("name") == name)
        ]
        if hits:
            return True, f"失败 {len(hits)} 次（如 {hits[0].get('name')}）"
        return False, "未观察到失败的工具结果"

    return Check(f"failed_tool_seen({name or 'any'})", check)


# ---------------- 审批闸门效率 ----------------

def approvals_between(lo: int, hi: int) -> Check:
    """审批（interrupt）次数落在 [lo, hi]：写任务应 ≥1 且不过多，读任务应为 0。"""

    def check(result, workspace: Path) -> tuple[bool, str]:
        count = getattr(result, "interrupt_count", 0)
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
