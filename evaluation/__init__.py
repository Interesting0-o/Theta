"""Theta 评估框架（骨架）。

定位：与 tests/ 分工——tests/ 守"机制"（guard / 沙箱 / 路由等确定性单测，
98 个用例已覆盖），本包守"模型行为"（prompt / 工具 docstring / tool.json 迭代时，
LLM 涌现行为可能静默回归：不建 plan、读任务调写工具、拒绝后死循环等）。

结构：
- tasks.py   任务集：Task（prompt / 预置文件 / 审批策略 / 检查项）+ 示例任务
- policy.py  审批策略：替代 TUI 里的人工 y/n（模拟人，可自动批准/拒绝/黑名单）
- runner.py  驱动器：复用 app/main.py 的 interrupt → Command(resume) 循环，
              用 InMemorySaver 编译图，每任务隔离临时工作区
- checks.py  确定性断言：终态文件 / 审批次数 / plan 状态 / 正常终止（不花钱）
- report.py  汇总渲染：纯文本表格，无第三方依赖

用法（需 .env 存在，与 TUI 相同前提）：
    python -m evaluation                        # 跑 tasks.py 的示例任务集
    python -m evaluation --task write_and_verify

注意：
- 真实 LLM 评估会花 token；文件写入被隔离在每任务的临时工作区（不碰仓库）；
  terminal / 联网工具由策略默认拒绝（policy.DEFAULT_DENY_TOOLS）。
- 层 A（无 LLM 成本的录制回放）复用 tests/ 的假模型模式，待接入，见 runner.py。
"""
from .tasks import Task, EXAMPLE_TASKS
from .policy import auto_approve, auto_reject, allow_except, DEFAULT_DENY_TOOLS
from .runner import (
    EvalResult,
    InterruptRecord,
    build_eval_graph,
    run_suite,
    run_task,
)
from .checks import Check, run_checks
from .report import print_report

__all__ = [
    "Task",
    "EXAMPLE_TASKS",
    "auto_approve",
    "auto_reject",
    "allow_except",
    "DEFAULT_DENY_TOOLS",
    "EvalResult",
    "InterruptRecord",
    "build_eval_graph",
    "run_task",
    "run_suite",
    "Check",
    "run_checks",
    "print_report",
]
