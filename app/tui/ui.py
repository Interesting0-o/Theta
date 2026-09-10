"""终端前端：实现基座的 `UI` 协议——stdin 取输入、标准输出渲染事件、审批面板收 y/n。

位置：`app/tui/ui.py`（2026-09-10 基座/前端分家后新增）。本类是**协议的唯一实现**（将来
web 前端是另一个实现），基座（`app/platform/loop.py`）只认协议、不认识本类。

输入通道的取消语义（协议要求"被取消不丢输入"）：stdin 由**独立 reader 线程**独占读、
经 pump 转进 asyncio 队列；`read_line` 只是 `queue.get()`——被基座 cancel 时队列内容不丢。
"""
from __future__ import annotations

import asyncio
import queue

from app.schema.ui_schema import ReadyForInput, SessionStarted, TurnFailed, TurnFinished
from app.tui.input import _ask_yes_no, _pump_stdin, _start_stdin_reader
from app.tui.panels import AGENT_TITLE, COMMAND_TITLE, USER_TITLE, format_tool_approval


class TerminalUI:
    """终端前端：stdin 线程 + 标准输出渲染 + 审批面板。

    结构上即 `app/platform/ui.py::UI` 协议的实现（Python 的类型检查是结构化的，无需继承）；
    基座只按协议调用 emit / read_line / decide 三个方法。
    """

    def __init__(self) -> None:
        self._reader_q: queue.Queue | None = None
        self._out_q: asyncio.Queue | None = None
        self._pump: asyncio.Task | None = None

    async def start(self) -> None:
        """起输入通道（单 stdin reader 线程 + pump 任务）；需在事件循环内调用。"""
        self._reader_q = _start_stdin_reader()
        self._out_q = asyncio.Queue()
        self._pump = asyncio.create_task(_pump_stdin(self._reader_q, self._out_q))

    async def aclose(self) -> None:
        """收尾输入通道（停 pump；reader 线程是 daemon，随进程结束）。"""
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None

    # ---------- UI 协议 ----------

    def emit(self, event) -> None:
        """把基座事件渲染成终端输出（只显示，不改状态）。"""
        if isinstance(event, SessionStarted):
            print(f"[会话] session_id={event.session_id}")
            print(f"[工作区] {event.workspace}（首次输入时才会建库）")
        elif isinstance(event, ReadyForInput):
            print(USER_TITLE)
        elif isinstance(event, TurnFinished):
            print(AGENT_TITLE)
            print(event.text)
        elif isinstance(event, TurnFailed):
            print(f"\n[agent 运行出错] {event.message}")

    async def read_line(self) -> str | None:
        """取一行用户输入；None = EOF。可被基座 cancel 而不丢输入（队列内容仍在）。"""
        return await self._out_q.get()

    async def decide(self, value: dict) -> bool:
        """画 Command 框 + 审批面板，向用户收 y/n；返回是否批准（决定权威在人）。"""
        print(COMMAND_TITLE)
        print(format_tool_approval([value]))
        print()
        return await _ask_yes_no(self._out_q, "是否批准该操作？(y/n): ")
