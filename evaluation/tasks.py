"""任务集：一次评估会话的输入定义 + 示例任务。

Task 是纯数据（prompt / 预置文件 / 审批策略 / 检查项），不感知 runner 细节。
示例任务只做安全的文件读写与计划编排，配合 policy.allow_except 默认黑名单
（terminal / 联网工具一律拒绝），跑真 LLM 也不会误伤环境。

**层 A 回放不需要另一套任务类型**：同一份 Task 在回放模式下自动换成录制好的模型输出
（`runner.run_suite(mode=…)`），所以这里只有"真跑用的任务"一种写法；配合它的是
`evaluation/fixtures/<task>.json`。改动 task 的 prompt / setup 会让旧 fixture 作废（指纹比对），
跑 `--record` 重录即可。

TODO（骨架之后）：
- 任务外置成 JSON/YAML（现在内联在 EXAMPLE_TASKS，便于先跑通）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .checks import (
    Check,
    approvals_between,
    file_content,
    file_contains,
    file_exists,
    failed_tool_seen,
    file_matches,
    files_exist,
    no_approvals,
    plan_steps_at_least,
    pytest_passes,
    terminated,
    tool_call_count_between,
    tool_called,
)
from .policy import (
    Policy,
    allow_dev_terminal,
    allow_except,
    allow_readonly_terminal,
)


@dataclass(frozen=True)
class Task:
    """一个评估任务。

    - name：唯一标识（同时用作 checkpoint thread_id 前缀）；
    - prompt：发给模型的用户消息；
    - setup：运行前预置进工作区的文件（相对路径 → 内容）；
    - policy：审批策略（缺省 allow_except()，即拒绝 terminal/联网、放行文件读写）；
    - checks：终态断言（缺省至少含 terminated）；
    - max_rounds：最多模型回合数，防止拒绝路径死循环挂死评估。
    """

    name: str
    prompt: str
    setup: Dict[str, str] = field(default_factory=dict)
    policy: Policy = field(default_factory=allow_except)
    checks: List[Check] = field(default_factory=list)
    max_rounds: int = 30


# ------------------------------ 典型场景：日志分析 ------------------------------
# 固定 1200 行：每 40 行一条 ERROR（共 30 条），每 17 行一条 WARN，其余 INFO；无 FATAL。
# 最后三条 ERROR 的 rid 是稳定的（1120 / 1160 / 1200），用作"原文必须写进交付物"的断言。
_LOG_TOTAL_LINES = 1200
_LOG_ERROR_EVERY = 40
_LOG_WARN_EVERY = 17


def _build_app_log() -> str:
    lines: List[str] = []
    for i in range(1, _LOG_TOTAL_LINES + 1):
        ts = f"2026-10-18 12:{(i // 60) % 60:02d}:{i % 60:02d}"
        if i % _LOG_ERROR_EVERY == 0:
            lines.append(f"{ts} ERROR rid={i:04d} request failed: timeout")
        elif i % _LOG_WARN_EVERY == 0:
            lines.append(f"{ts} WARN rid={i:04d} slow response")
        else:
            lines.append(f"{ts} INFO rid={i:04d} request ok")
    return "\n".join(lines) + "\n"


_LOG_LAST_ERROR_RIDS = [
    f"rid={i:04d}"
    for i in range(1, _LOG_TOTAL_LINES + 1)
    if i % _LOG_ERROR_EVERY == 0
][-3:]


EXAMPLE_TASKS: List[Task] = [
    Task(
        name="read_and_summarize",
        prompt="请阅读工作区里的 README.md，用三句话总结它的内容。",
        setup={
            "README.md": (
                "# 演示项目\n\n这是一个用于评估 Theta 的示例项目。\n\n"
                "核心能力：文件读写、目录管理、内容检索。\n\n"
                "所有写操作都需要人工审批。\n"
            ),
        },
        checks=[
            no_approvals(),  # 纯读任务不应触发审批闸门
            terminated(),
        ],
    ),
    Task(
        name="write_and_verify",
        prompt="创建 hello.txt，内容为 'hello eval'，然后读回内容向我确认写入结果。",
        checks=[
            file_content("hello.txt", "hello eval"),
            approvals_between(1, 5),  # 写操作应经过审批，但不应无限审批
            terminated(),
        ],
    ),
    Task(
        name="plan_and_write",
        prompt=(
            "先把下面的小任务拆成计划再执行：在工作区创建 demo.txt 写入 'planned'，"
            "执行完后把计划里对应的步骤标记为完成。"
        ),
        checks=[
            file_exists("demo.txt"),
            plan_steps_at_least(1),  # 编排工具确实被使用
            terminated(),
        ],
    ),
    Task(
        name="log_analysis",
        prompt=(
            "请分析工作区里的 logs/app.log：\n"
            "1) 统计总行数；\n"
            "2) 统计 ERROR 行数；\n"
            "3) 统计 FATAL 行数（没有就明确写 0）；\n"
            "4) 找出最后 3 条 ERROR 的原文；\n"
            "5) 把结果写入 logs/summary.md。\n"
            "日志较大，优先用终端只读命令（grep / wc / tail 等）统计，不要整份读入；"
            "需要时再读取少量片段。"
        ),
        setup={"logs/app.log": _build_app_log()},
        # 这个场景要测终端，所以不用默认的"全拒 terminal"策略，而是只批只读命令。
        policy=allow_readonly_terminal(),
        checks=[
            file_exists("logs/summary.md"),
            # 最后三条 ERROR 必须按原文出现（rid 是稳定且唯一的锚点）。
            file_contains("logs/summary.md", *_LOG_LAST_ERROR_RIDS),
            file_matches(
                "logs/summary.md",
                r"(?:总行数|总行|共)[^\n]{0,20}1,?200|1,?200[^\n]{0,20}(?:行|line)",
            ),
            file_matches(
                "logs/summary.md",
                r"(?:ERROR|错误)[^\n]{0,30}\b30\b|\b30\b[^\n]{0,15}(?:ERROR|错误)",
            ),
            file_matches(
                "logs/summary.md",
                r"(?:FATAL)[^\n]{0,30}\b0\b|\b0\b[^\n]{0,15}(?:FATAL)",
            ),
            tool_called("run_command", min_times=1),  # 场景要求用终端统计
            tool_call_count_between("run_command", 2, 5),  # 有恢复，但没有反复重试
            failed_tool_seen("run_command"),  # 本轨迹包含"Unix 工具缺失 → PowerShell"的恢复
            approvals_between(1, 15),
            terminated(),
        ],
        max_rounds=20,
    ),
    Task(
        name="wishlist_trace",
        prompt=(
            "请用 Python 标准库（不要安装第三方依赖）从零实现一个匿名许愿板，"
            "代码拆成多个文件：\n"
            "- app.py：HTTP 服务与路由；\n"
            "- storage.py：共享持久化存储；\n"
            "- templates/index.html：页面模板；\n"
            "- assets/poem.txt：页面底部显示的小诗（已预置，直接读取）。\n\n"
            "产品需求：页面顶部是一个 200 字上限的输入框和 “Make a Wish” 按钮；"
            "提交后愿望立即出现在下方列表，最新的在最上面；所有访客共享同一份持久化数据"
            "（重启后仍在）；页面底部显示 assets/poem.txt 的原文；无账号、无编辑、无删除。\n\n"
            "另外写 tests/test_app.py（pytest），至少覆盖：提交后新愿望出现在列表最前；"
            "201 字被拒绝。完成后运行 `python -m pytest` 确认测试通过。"
        ),
        setup={
            "assets/poem.txt": (
                "A quiet wish upon the night,\n"
                "a thread of hope, a fleck of light.\n"
                "We hold it close, then let it go—\n"
                "the dark will keep what we bestow.\n"
            ),
        },
        # 开发型场景：允许写文件、跑 python/pytest；不装依赖、不联网。
        policy=allow_dev_terminal(),
        checks=[
            files_exist("app.py", "storage.py", "templates/index.html", "tests/test_app.py"),
            pytest_passes("tests", timeout=120),
            approvals_between(1, 40),
            terminated(),
        ],
        max_rounds=80,
    ),
]
