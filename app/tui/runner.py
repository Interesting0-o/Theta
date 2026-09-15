"""终端前端的入口：解析启动工作区、组 TerminalUI + AgentPlatform 并跑基座循环。

位置：`app/tui/runner.py`（2026-09-10 基座/前端分家后从 app/tui/driver.py 拆出）。
`app/main.py` 只调本模块的 `run_tui()`（入口语义不变）。

"工作区 = 启动目录"是**前端策略**（web 前端会显式指定工作区），故留在前端；基座只收
`workspace` 参量。`app.platform`（基座）在 import 期不碰 `app.agent` → `import app.tui`
无需 .env（真图装配在基座 runtime 里懒加载）。
"""
import uuid
from pathlib import Path

from app.platform import AgentPlatform
from app.tui.ui import TerminalUI


def resolve_tui_workspace() -> str:
    """TUI 工作区沙箱 = 进程启动目录（cwd）。

    以 workspace_path 参量传给基座（→ 图构造）——TUI 交互时 agent 操作的就是你启动它的
    那个项目目录；需要它操作别的目录就 cd 过去再启动。
    """
    return str(Path.cwd())


async def run_tui() -> None:
    """终端前端入口：新会话 + 终端 UI + 基座循环。

    每次启动 = 新会话（`session_id=uuid4().hex`）；db/图由基座在**首次用户输入**时才懒建。
    退出（q/quit/exit 或 EOF）时前端收尾自己的输入通道，基座收尾 inbox / 连接 / 在跑的 turn。
    """
    workspace = resolve_tui_workspace()
    session_id = uuid.uuid4().hex
    ui = TerminalUI()
    platform = AgentPlatform(workspace=workspace, session_id=session_id, ui=ui)
    try:
        await ui.start()  # 起 stdin reader 线程 + pump
        await platform.run()
    finally:
        await ui.aclose()
