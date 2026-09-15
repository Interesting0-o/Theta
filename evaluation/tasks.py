"""任务集：一次评估会话的输入定义 + 示例任务。

Task 是纯数据（prompt / 预置文件 / 审批策略 / 检查项），不感知 runner 细节。
示例任务只做安全的文件读写与计划编排，配合 policy.allow_except 默认黑名单
（terminal / 联网工具一律拒绝），跑真 LLM 也不会误伤环境。

TODO（骨架之后）：
- 任务外置成 JSON/YAML（现在内联在 EXAMPLE_TASKS，便于先跑通）；
- 增加"层 A"任务类型：直接给定期望的 tool_calls 序列做回放（无 LLM 成本）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .checks import (
    Check,
    approvals_between,
    file_content,
    file_exists,
    no_approvals,
    plan_steps_at_least,
    terminated,
)
from .policy import Policy, allow_except


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
]
