"""评估入口：python -m evaluation [--task NAME] [--workspace-root DIR] [--keep-workspace]。

前提与 TUI 相同：.env 存在（model.py 顶层 get_settings）、TAVILY 可空。
安全默认：terminal 与四个联网工具在审批策略层一律拒绝（policy.DEFAULT_DENY_TOOLS），
文件写入隔离在每任务临时工作区。
"""
import argparse
import asyncio
from pathlib import Path

from .report import print_report
from .runner import run_suite
from .tasks import EXAMPLE_TASKS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CodingAgent 评估骨架")
    parser.add_argument("--task", help="只跑指定任务名（默认全跑 EXAMPLE_TASKS）")
    parser.add_argument("--workspace-root", type=Path, help="临时工作区根目录（默认系统 temp）")
    parser.add_argument("--keep-workspace", action="store_true", help="保留工作区供人工复查失败现场")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    tasks = (
        [t for t in EXAMPLE_TASKS if t.name == args.task]
        if args.task
        else EXAMPLE_TASKS
    )
    if not tasks:
        print(f"未找到任务: {args.task}（现有: {', '.join(t.name for t in EXAMPLE_TASKS)}）")
        return

    runs = await run_suite(
        tasks,
        workspace_root=args.workspace_root,
        keep_workspace=args.keep_workspace,
    )
    print_report(runs)


if __name__ == "__main__":
    asyncio.run(main())
