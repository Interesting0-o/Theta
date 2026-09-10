"""基座主事件循环：drain 待批 → 等 turn → 等输入，全程只经 UI 协议与前端说话。

位置：`app/platform/loop.py`（run_tui 的那条循环自 2026-09-10 基座/前端分家后归此）。
形状自 2026-09-07 事件化重构起未变（docs/MULTI_AGENT.md §6 统一审批视图）：

- pending 审批**即服务**：`drain_approvals` 逐条交前端问人（`ui.decide`）；
- 有 turn 在跑（含 park 挂起）→ 等它结束或新审批到达（`_race` 两路唤醒，不忙轮询）；
- 空闲 → `emit(ReadyForInput())`，等一行输入起下一条 turn（新 worker 审批可插队服务）。

本模块**不 print、不读 stdin**，也不 import app.tui（docs/ARCHITECTURE.md §2.7/§4）。
用户命令（`/init` 等，见 commands/ 包）在"起 turn 之前"介入这一循环——即基座的控制面事件：
提示词型把预设 prompt 当本轮输入（继续起 turn），基座动作型直接执行完就回顶部（不起 turn，
结果经 `Notice` 回话），未识别的 `/` 输入提示而不喂给模型。
"""
import asyncio

from app.platform.approvals import ApprovalInbox, ApprovalInboxServer, drain_approvals
from app.platform.commands import (
    UNKNOWN_COMMAND_HINT,
    ActionCommand,
    PromptCommand,
    is_command,
    parse_command,
)
from app.platform.runtime import build_session_runtime
from app.platform.turn import _race, build_turn_state, drive_turn
from app.platform.ui import UI
from app.resource import remove_legacy_single_db
from app.schema.ui_schema import (
    Notice,
    ReadyForInput,
    SessionStarted,
    TurnFailed,
    TurnFinished,
)

# 退出词（终端习惯；命令层落地后与 /quit 之类一并归一）
EXIT_WORDS = ("quit", "q", "exit")


class AgentPlatform:
    """基座：驱动 agent 图与审批 broker，经 UI 协议与前端说话（不 print、不读 stdin）。

    - workspace / session_id 由**前端**决定（终端 = 启动目录 + uuid 新会话）后作参量传入；
    - db / checkpointer / 图在**首次用户输入**时才懒建（`step_factory`），此后同进程内各轮复用；
    - `step_factory` 可注入（默认 `build_session_runtime`）：测试传假 step 即可驱动整条循环
      （否则循环本身无法在无 db/无 .env 下被测）。
    """

    def __init__(self, workspace: str, session_id: str, ui: UI, step_factory=None) -> None:
        self.workspace = workspace
        self.session_id = session_id
        self.ui: UI = ui
        self._step_factory = step_factory or build_session_runtime
        self._connection = None  # AsyncSqliteSaver 连接；首次交互创建，退出时关闭
        self._step = None  # compiled.ainvoke 闭包；首次交互创建后复用

    async def run(self) -> None:
        """跑到退出（EOF / 退出词）：收尾 inbox、取消在跑的 turn、关会话连接。"""
        remove_legacy_single_db()  # 旧 resource/agent.db 单库一次性清理（用户已确认删除）

        inbox = ApprovalInboxServer()
        turn: asyncio.Task | None = None
        turn_done = asyncio.Event()
        try:
            # 先起收件箱再宣告会话就绪：收件箱起不来（端口被占等）会抛 ConfigError，
            # 那时不该先打一句"会话 session_id=…"让用户以为已经跑起来了。
            await inbox.start()
            self.ui.emit(SessionStarted(session_id=self.session_id, workspace=self.workspace))

            while True:
                # 1) 审批 pending 即服务（本地 park + worker HTTP 一视同仁）
                if inbox.queue.pending():
                    await drain_approvals(inbox.queue, self.ui)
                    continue

                # 2) 有 turn 在跑（含 park 挂起）→ 等它结束或新审批到达
                if turn is not None and not turn.done():
                    inbox.queue.clear_new()  # pending 已空，清掉过期 set 再等
                    await _race(turn_done.wait, inbox.queue.new_pending.wait)
                    continue

                # 3) 空闲 → 提示可输入，等一行有效输入（新 worker 审批可插队服务）
                inbox.queue.clear_new()
                self.ui.emit(ReadyForInput())
                idx, line = await self._read_user_line(inbox.queue)
                if idx == 1:  # 审批先到 → 回顶部 drain
                    continue
                if line is None or line in EXIT_WORDS:
                    break  # EOF / 退出

                # 控制面命令（图之外，见 commands/ 包）：提示词型把预设 prompt 当本轮输入、
                # 继续走下面的起 turn；基座动作型由基座直接执行完 continue；
                # 未识别的 `/` 输入不喂给模型，直接提示、不起 turn。
                command = parse_command(line)
                if isinstance(command, PromptCommand):
                    line = command.prompt
                elif isinstance(command, ActionCommand):
                    await command.handler(self, command.args)
                    continue
                elif is_command(line):
                    self.ui.emit(Notice(text=UNKNOWN_COMMAND_HINT))
                    continue

                # 首次交互才建库/checkpoint/图（之后各轮复用同一 runtime）
                if self._step is None:
                    self._connection, self._step = await self._step_factory(
                        self.workspace, self.session_id
                    )
                turn_done.clear()
                turn = asyncio.create_task(
                    self._run_turn(build_turn_state(self.session_id, line), inbox.queue, turn_done)
                )
                # 回循环顶部：新 turn 的审批/终态都从 drain 与 turn_done 两个信号驱动
        finally:
            if turn is not None and not turn.done():
                turn.cancel()
            await inbox.stop()
            if self._connection is not None:
                await self._connection.close()

    async def switch_session(self, session_id: str) -> None:
        """切到另一个会话（`/session <id>`、`/new session` 命令用）。

        当前会话的 checkpoint 是**逐步落盘**的，不需要额外"落定"动作，关掉连接即可；
        `step` 的闭包捕获了本会话的 thread_id，所以必须丢弃、由下一条消息懒重建
        （走 `_step_factory`，测试注入的假 step 照常生效）。
        """
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
        self.session_id = session_id
        self._step = None

    async def _read_user_line(self, inbox: ApprovalInbox) -> tuple[int, str | None]:
        """等一行**有效**输入；空行保持同一提示继续等，审批先到则返回 (1, None) 让上层回顶。

        返回 (0, line)：line 为 None 即 EOF；退出词的判定留给调用方。
        """
        while True:
            idx, line = await _race(self.ui.read_line, inbox.new_pending.wait)
            if idx == 1:  # 审批先到（worker 请求）→ 回顶部 drain
                return idx, None
            if line:  # 非空行（含退出词）：交调用方判定
                return idx, line
            if line is None:  # EOF
                return idx, None
            # 空行：不改提示、继续等

    async def _run_turn(
        self, initial: dict, queue: ApprovalInbox, turn_done: asyncio.Event
    ) -> None:
        """后台跑一个 turn，把终态/错误翻成事件交前端；结束置 turn_done。

        图运行抛错（内部 bug / 工具异常漏网等）不静默：emit TurnFailed，仍置 turn_done
        让事件循环回到空闲，避免 "Task exception was never retrieved" 与死等的观感。
        """
        try:
            result = await drive_turn(self._step, initial, queue)
            messages = result.get("messages") or []
            if messages:
                self.ui.emit(TurnFinished(text=str(messages[-1].content)))
        except asyncio.CancelledError:
            raise  # 关闭清理主动取消，放行
        except Exception as exc:  # noqa: BLE001 —— 用户态提示，不让任务静默失败
            self.ui.emit(TurnFailed(message=f"{type(exc).__name__}: {exc}"))
        finally:
            turn_done.set()
