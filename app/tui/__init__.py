"""app/tui —— **终端前端**：只做"取输入 + 渲染事件"，不含调度。

设计的家（docs/ARCHITECTURE.md §2.7/§3/§4 + docs/MULTI_AGENT.md §6 统一审批视图）：
主程序 = **事件基座**（`app/platform`：run 生命周期 / 审批 broker / runtime），前端只实现
基座的 `UI` 协议（emit 渲染 / read_line 取输入 / decide 就待审请求问人）。2026-09-10 起
基座与前端分家——**基座不 print、不读 stdin**；本包只显示与取输入，不碰调度与 park/resume。

模块（各自单一职责，无跨包循环）：
- input.py    stdin 单 reader 线程 + pump + 一问一答原语
- panels.py   终端渲染素材：审批面板 / 标题框 / 截断（纯函数）
- ui.py       TerminalUI：实现 `app/platform/ui.py::UI` 协议
- runner.py   run_tui()：解析启动工作区 → 组 UI + AgentPlatform → 跑基座循环

`app/main.py` **保留为程序入口**（`python -m app.main`），只调本包导出的 `run_tui`。
将来的 web 前端 = 另一个 UI 协议实现（在 app/platform 之外另起包），复用同一基座。
基座侧 `app/platform` 不 import 本包；`app.agent` 只在基座 runtime 内懒加载 → import 本包
不触发 .env。
"""
from app.tui.runner import run_tui

__all__ = ["run_tui"]
