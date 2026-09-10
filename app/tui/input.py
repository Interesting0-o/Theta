"""stdin 输入解耦：单 reader 线程 + pump 转 asyncio.Queue。

从 app/main.py 迁入（事件化重构后与审批判定面分家）。职责单一：**只做键盘/EOF 行的搬运**，
不掺 agent 图与审批判定——"问一行 y/n"这类交互由 `app/tui/ui.py::TerminalUI` 自己做
（它要在同一处统一处理 EOF，见该文件的 `_take_line`）。

- `_start_stdin_reader`：daemon 线程专读 sys.stdin 行 → 线程队列（EOF 时 put(None)）。
  阻塞 `input()` 会让唯一事件循环被键盘占死，故用单 reader 线程独占 stdin。
- `_pump_stdin`：把线程队列行转进 asyncio.Queue（只挂一个 to_thread 等行）；EOF 后**返回**。
"""
import asyncio
import queue
import sys
import threading


def _start_stdin_reader() -> queue.Queue:
    """起 daemon 线程专读 sys.stdin 行；EOF 时 put(None)。单 reader 唯一占有 stdin。"""
    reader_q: queue.Queue = queue.Queue()

    def _read():
        for line in sys.stdin:
            reader_q.put(line)
        reader_q.put(None)

    threading.Thread(target=_read, name="stdin-reader", daemon=True).start()
    return reader_q


async def _pump_stdin(reader_q: queue.Queue, out_q: asyncio.Queue):
    """把线程队列里的 stdin 行转进 asyncio.Queue（每次只挂一个 to_thread 等行）。

    空行也转（审批 y/n 时空回车=拒绝/无效），仅 EOF 用 None 表示退出。
    """
    while True:
        raw = await asyncio.to_thread(reader_q.get)
        if raw is None:
            out_q.put_nowait(None)
            return
        out_q.put_nowait(raw.strip())


