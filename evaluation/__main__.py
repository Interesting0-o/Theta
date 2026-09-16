"""评估入口：python -m evaluation [--live | --record] [--task NAME] [--workspace-root DIR] [--keep-workspace]。

**默认走层 A 回放——不花 token**：拿 `evaluation/fixtures/` 里录制好的模型输出当脚本，其余全是真的
（真图、真 MCP 运行体、真工具执行）。真跑要显式 `--live`，那才是验证 prompt / 工具 docstring 改动的
手段（回放的模型不读输入，对这类改动是瞎的）。

前提与 TUI 相同：.env 存在（model.py 顶层 get_settings）、TAVILY 可空。
安全默认：terminal 与四个联网工具在审批策略层一律拒绝（policy.DEFAULT_DENY_TOOLS），
文件写入隔离在每任务临时工作区。
"""
import argparse
import asyncio
from pathlib import Path

from .report import print_report
from .runner import (
    MODE_LIVE,
    MODE_RECORD,
    MODE_REPLAY,
    cleanup_workspaces,
    run_suite,
)
from .tasks import EXAMPLE_TASKS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Theta 模型行为评估（默认层 A 回放，零 LLM 成本）")
    live_or_record = parser.add_mutually_exclusive_group()
    live_or_record.add_argument(
        "--live", action="store_true", help="层 B：真调模型（花 token）"
    )
    live_or_record.add_argument(
        "--record",
        action="store_true",
        help="层 B：真跑并把模型输出录成 fixture（覆盖写 evaluation/fixtures/<task>.json）",
    )
    parser.add_argument("--task", help="只跑指定任务名（默认全跑 EXAMPLE_TASKS）")
    parser.add_argument("--workspace-root", type=Path, help="临时工作区根目录（默认系统 temp）")
    parser.add_argument("--keep-workspace", action="store_true", help="保留工作区供人工复查失败现场")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    mode = MODE_RECORD if args.record else (MODE_LIVE if args.live else MODE_REPLAY)

    tasks = (
        [t for t in EXAMPLE_TASKS if t.name == args.task]
        if args.task
        else EXAMPLE_TASKS
    )
    if not tasks:
        print(f"未找到任务: {args.task}（现有: {', '.join(t.name for t in EXAMPLE_TASKS)}）")
        return

    runs = await run_suite(tasks, workspace_root=args.workspace_root, mode=mode)
    try:
        # 报告里的检查项要读工作区里的终态文件，所以清理必须在这之后（见 cleanup_workspaces）
        print_report(runs, mode=mode)
    finally:
        if not args.keep_workspace:
            cleanup_workspaces(runs)


if __name__ == "__main__":
    asyncio.run(main())
