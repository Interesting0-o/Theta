"""Theta 评估框架：两层分工——层 A 回放（零成本）/ 层 B 真跑（花 token）。

定位：与 tests/ 分工——tests/ 守"机制"（guard / 沙箱 / 路由等确定性单测），本包守"模型行为"。
但"守模型行为"只有**层 B** 做得到：层 A 把模型换成脚本、拿**真实录制的输出**当固定输入样本，
断言"**除模型之外的一切**"（图接线 / tool.json 策略 / 工具真执行 / 编排分流 / 闸门 / compact）
没变。它与 tests/ 互补——那边用手写的 tool_calls 覆盖边界情况，这边用真实录制的形态。

⚠️ **改了 prompt 或工具 docstring 必须跑层 B**：回放的模型不读输入，层 A 对这类改动是瞎的，
"回放全绿"不等于"评估过了"。

结构：
- tasks.py     任务集：Task（prompt / 预置文件 / 审批策略 / 检查项）+ 示例任务
- policy.py    闸门策略：替代 TUI 里的人工决定（规则式 / 脚本式 / 全拒）
- models.py    模型侧两半：ReplayChatModel（回放）+ RecordingCallback（录制）
- fixtures.py  录制产物的落点、读写，以及"任务已改动需重录"的指纹比对
- runner.py    驱动器：三种模式共用同一个 park/resume 循环，只有"喂给图的模型"不同
- checks.py    确定性断言：终态文件 / 审批次数 / plan 状态 / 正常终止（不花钱）
- report.py    汇总渲染：纯文本表格，无第三方依赖

用法（需 .env 存在，与 TUI 相同前提）：
    python -m evaluation                        # 层 A 回放（默认，零成本）
    python -m evaluation --task write_and_verify
    python -m evaluation --live                 # 层 B 真跑（花 token）
    python -m evaluation --record               # 真跑并把模型输出录成 fixture

注意：真实 LLM 评估会花 token；文件写入被隔离在每任务的临时工作区（不碰仓库）；
terminal / 联网工具由策略默认拒绝（policy.DEFAULT_DENY_TOOLS）。
"""
from .tasks import Task, EXAMPLE_TASKS
from .policy import Policy, allow_except, deny_all, scripted, DEFAULT_DENY_TOOLS
from .models import ReplayChatModel, RecordingCallback
from .fixtures import Fixture, FixtureError, FIXTURES_DIR
from .runner import (
    MODE_LIVE,
    MODE_RECORD,
    MODE_REPLAY,
    EvalResult,
    build_eval_graph,
    cleanup_workspaces,
    run_suite,
    run_task,
)
from .checks import Check, run_checks
from .report import print_report

__all__ = [
    "Task",
    "EXAMPLE_TASKS",
    "Policy",
    "allow_except",
    "deny_all",
    "scripted",
    "DEFAULT_DENY_TOOLS",
    "ReplayChatModel",
    "RecordingCallback",
    "Fixture",
    "FixtureError",
    "FIXTURES_DIR",
    "MODE_REPLAY",
    "MODE_LIVE",
    "MODE_RECORD",
    "EvalResult",
    "build_eval_graph",
    "run_task",
    "run_suite",
    "cleanup_workspaces",
    "Check",
    "run_checks",
    "print_report",
]
