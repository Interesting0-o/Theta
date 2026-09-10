"""Theta 程序入口（薄壳）。

TUI 逻辑已迁入 `app/tui`（终端前端）+ `app/platform`（事件基座），本文件只保留
`python -m app.main` 的入口语义：调 `app.tui.run_tui()`，并把**入口边界**的配置类错误
翻成人话（docs/EXCEPTION_DESIGN.md §6）。
```
uv run python -m app.main        # 或 .venv/Scripts/python.exe -m app.main（勿动 venv）
```
"""
import asyncio
import sys

from app.exception import ConfigError
from app.tui import run_tui

if __name__ == "__main__":
    try:
        asyncio.run(run_tui())
    except ConfigError as exc:
        # 配置类失败（工作区不存在、审批收件箱端口被占…）是"说得清"的错误：
        # 打一句人话 + 非零退出，别让它裹在 traceback 里像个崩溃。
        print(f"\n❌ 启动失败（配置问题）：{exc}")
        sys.exit(1)
