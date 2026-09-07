"""CodingAgent 程序入口（薄壳）。

TUI 逻辑已迁入 `app/tui`（事件驱动 driver + 审批判定 + 收件箱），本文件只保留
`python -m app.main` 的入口语义：调 `app.tui.run_tui()`。
```
uv run python -m app.main        # 或 .venv/Scripts/python.exe -m app.main（勿动 venv）
```
"""
import asyncio

from app.tui import run_tui

if __name__ == "__main__":
    asyncio.run(run_tui())
