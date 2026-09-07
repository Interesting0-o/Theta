"""app/tui —— 事件驱动的 TUI 壳（main 重构的家）。

设计（docs/MULTI_AGENT.md §6/§9/§10，2026-09-07）：**agent 图是 TUI 的一个模块而不是
全部**；main 收敛为**事件驱动壳**——run 可 park（interrupt 阻塞图、不阻塞进程），主 agent
自身 interrupt 与子 agent 审批对齐为同一种 ApprovalPending，审核收敛为**同一 decide() +
worker_id 标记路由**（判定在 approval.py，broker 在 approval_inbox.py）。
`app/main.py` **保留为程序入口**（`python -m app.main`），只调本包导出的 `run_tui`。

模块（各自单一职责，无跨包循环）：
- input.py         stdin 单 reader 线程 + pump + 一问一答原语
- approval_inbox.py 统一审批 broker：ApprovalInbox（纯队列）+ ApprovalInboxServer（HTTP 收件箱）
- approval.py      判定面：渲染面板 + decide_approval + drain_approvals（pending 即服务）
- driver.py        run_tui 事件循环 + drive_turn（turn 核心 park/resume）+ get_agent_db_path

边界：agent 图在 app/agent（driver 懒加载，import 本包不触发 .env）；跨进程共享 schema 在
app/schema/approval_schema.py（worker 侧 http_agent 也 import，故留在原处）。
"""
from app.tui.driver import run_tui

__all__ = ["run_tui"]
