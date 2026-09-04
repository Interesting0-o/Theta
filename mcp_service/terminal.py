"""终端执行 MCP 服务器：任意 shell 命令，但执行模型与"进程生命周期"是工程核心。

安全前提：本 server 没有任何文件沙箱——`run_command` / `start_process` 以当前用户权限
执行任意 shell（默认 cwd 是 MCP 服务器启动目录 = 工作区，但命令可 `cd` 到工作区外）。
它的闸门只有 tool.json 的**人工审批**，因此所有执行/终止类工具都要求 `description`
（模型必须用人话解释这条命令要干什么），审批面板会把 命令+解释 同屏展示给人确认。
**已知限制**：终端能 `cd ../../ && rm -r` 越出工作区目录，目前不做结构拦截，只靠知情审批
（见 CLAUDE.md 已知限制）。

**硬性检查**：命令文本含 `sudo`（大小写不敏感）一律直接拒绝——提权不在本 agent 的能力范围内，
`run_command` / `start_process` 在 spawn 前拦截。

执行模型（把"等多久"与"杀不杀"解耦）：
- 所有执行都经 async 子进程，交给进程表(_PROCS)托管：spawn 后立即起两个后台 reader 任务
  读 stdout/stderr 到共享 buffer，避免管道写满后子进程被阻塞；
- `run_command`（一次性，随用随删）：等待至多 timeout 秒；**到点未结束不杀**，转入受管句柄
  返回"仍在运行 + 部分输出"，由模型决定继续等(process_wait)/看增量(process_read)/终止
  (process_kill)；
- `start_process`（常驻级，如后端服务器）：spawn 后最多探 startup_wait 秒是否启动即崩，
  否则返回 running，进程跨轮存活，生命周期由 process_* 管理；
- 进程以进程组启动（posix start_new_session / windows 新进程组），MCP 服务器退出时
  atexit 杀光全部子进程，不留残留。
"""
from __future__ import annotations

import asyncio
import atexit
import os
import signal
import subprocess
import time
from itertools import count
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from app.exception import InvalidArgumentError
from app.schema.agent_schema import ToolResult
from mcp_service.utils import guard

mcp = FastMCP("Terminal")


# ------------------------------ 进程输出缓冲 ------------------------------

class _OutputBuffer:
    """子进程 stdout/stderr 的合并文本缓冲，支持"增量读、只消费读走的部分"。

    reader 任务把 stdout/stderr 分块追加进来；调用方用 read_new(limit=) 取**新增**内容，
    只有返回给模型的部分才推进消费游标（limit 截断时剩余留在缓冲里下次再读），保证
    大输出不会因一次展示截断而丢失。
    """

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._total = 0
        self._seen = 0
        self._last_append_ts = time.monotonic()

    def append(self, text: str) -> None:
        if text:
            self._parts.append(text)
            self._total += len(text)
            self._last_append_ts = time.monotonic()

    @property
    def last_append_ts(self) -> float:
        return self._last_append_ts

    def _all(self) -> str:
        return "".join(self._parts)

    def read_new(self, limit: int | None = None) -> str:
        """返回自上次读以来新增的文本；limit 限制本次返回的字符数，剩余留待下次。"""
        if self._total <= self._seen:
            return ""
        end = self._total if limit is None else min(self._total, self._seen + max(0, limit))
        text = self._all()[self._seen:end]
        self._seen = end
        return text

    def drain(self) -> str:
        return self.read_new(limit=None)


# ------------------------------ 进程记录 ------------------------------

class _Proc:
    def __init__(self, id_: str, command: str, description: str, resident: bool,
                 proc: asyncio.subprocess.Process, cwd: str) -> None:
        self.id = id_
        self.command = command
        self.description = description
        self.resident = resident
        self.proc = proc
        self.cwd = cwd
        self.started = time.monotonic()
        self.buffer = _OutputBuffer()
        self.exit_code: int | None = None
        self.readers: list[asyncio.Task] = []

    @property
    def running(self) -> bool:
        return self.exit_code is None and self.proc.returncode is None


_PROCS: dict[str, _Proc] = {}
_IDS = count(1)


def _next_id() -> str:
    return f"p{next(_IDS)}"


def _register(command: str, description: str, resident: bool, proc, cwd: str) -> _Proc:
    rec = _Proc(_next_id(), command, description, resident, proc, cwd)
    _PROCS[rec.id] = rec
    return rec


# ------------------------------ 子进程 spawn / 终止 ------------------------------

async def _reader_task(stream, buf: _OutputBuffer) -> None:
    """后台读一路管道，直到 EOF；chunk 解码后追加进 buffer。"""
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            buf.append(chunk.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 —— 读管道失败只意味着输出截断，进程主逻辑不受影响
        pass


def _resolve_cwd(cwd: str) -> str | None:
    """把可选的 cwd 解析成绝对路径；传了但不存在 → InvalidArgumentError（可修正重试）。"""
    if not cwd:
        return None
    path = Path(cwd).expanduser().resolve()
    if not path.is_dir():
        raise InvalidArgumentError(f"cwd 不存在或无法访问: {cwd}")
    return str(path)


def _build_spawn_kwargs(cwd: str | None) -> dict:
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if cwd:
        kwargs["cwd"] = cwd#type: ignore
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        # 服务/长任务不需控制台窗口，隐藏它（避免每次跑命令闪黑窗）
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs["creationflags"] = flags
    else:
        # 独立进程组/会话：让进程组的组长是 pid，便于整棵进程树一起杀
        kwargs["start_new_session"] = True
    return kwargs


async def _spawn(command: str, cwd: str | None) -> tuple[_Proc, asyncio.subprocess.Process]:
    proc = await asyncio.create_subprocess_shell(command, **_build_spawn_kwargs(cwd))
    rec = _register(command, "", False, proc, cwd or "")
    rec.readers = [
        asyncio.create_task(_reader_task(proc.stdout, rec.buffer)),
        asyncio.create_task(_reader_task(proc.stderr, rec.buffer)),
    ]
    return rec, proc


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """终止整棵子进程树：posix 杀进程组，Windows 用 taskkill /T。"""
    pid = proc.pid
    if pid is None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
            return
        except Exception:  # noqa: BLE001 —— taskkill 失败则退回 proc.kill()
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return
    try:
        os.killpg(pid, signal.SIGKILL)  # start_new_session → pid 即进程组组长
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _collect(rec: _Proc, drain_grace: float = 1.0) -> None:
    """进程已结束后收尾：让 reader 把管道残留读尽，再把它移出进程表。"""
    if rec.readers:
        try:
            await asyncio.wait_for(
                asyncio.gather(*rec.readers, return_exceptions=True), timeout=drain_grace
            )
        except asyncio.TimeoutError:
            for task in rec.readers:
                task.cancel()
    _PROCS.pop(rec.id, None)


async def _cancel_readers(rec: _Proc) -> None:
    for task in rec.readers:
        if not task.done():
            task.cancel()


# ------------------------------ 校验 / 渲染辅助 ------------------------------

def _require_text(value, field: str) -> None:
    if not value or not isinstance(value, str) or not value.strip():
        raise InvalidArgumentError(f"{field} 不能为空")


def _deny_sudo(command: str) -> None:
    """简单硬性检查：命令含 sudo 字样一律拒绝（提权不在本 agent 的能力范围内）。"""
    if "sudo" in command.lower():
        raise InvalidArgumentError(
            "命令含 sudo，已拒绝：本 agent 不做提权操作。如确需管理权限，请自行在终端执行或改用其它方式"
        )


def _find(process_id: str) -> _Proc:
    rec = _PROCS.get(process_id)
    if rec is None:
        raise InvalidArgumentError(
            f"进程 {process_id} 不存在或已被回收（可能已结束并交付；可用 process_list 查看）"
        )
    return rec


def _render_finished(rec: _Proc, code: int) -> ToolResult:
    text = rec.buffer.drain()
    lines = [f"exit_code: {code}"]
    if text.strip():
        lines.append("--- 输出 ---")
        lines.append(text.rstrip("\n"))
    else:
        lines.append("(无输出)")
    return ToolResult(success=code == 0, content="\n".join(lines))


def _render_running(rec: _Proc, note: str) -> ToolResult:
    elapsed = time.monotonic() - rec.started
    idle = max(0.0, time.monotonic() - rec.buffer.last_append_ts)
    kind = "常驻" if rec.resident else "一次性(超时转受管)"
    new_text = rec.buffer.read_new(limit=8000)
    lines = [
        f"进程 {rec.id} 仍在运行（{kind}）：{note}",
        f"command: {rec.command[:160]}",
        f"状态: running（exit_code 尚未产生）  已运行 {elapsed:.0f}s  最近一次新输出 {idle:.0f}s 前",
    ]
    if new_text.strip():
        lines.append("--- 近期输出 ---")
        lines.append(new_text.rstrip("\n"))
    lines.append(
        f"进程未被终止：可 process_wait({rec.id}) 继续等、process_read({rec.id}) 看增量、"
        f"process_kill({rec.id}) 终止"
    )
    return ToolResult(success=True, content="\n".join(lines))


# ------------------------------ 工具：一次性执行 ------------------------------

@mcp.tool()
@guard
async def run_command(
    command: str,
    description: str,
    cwd: str = "",
    timeout: int = 300,
) -> ToolResult:
    """执行一条有明确结束点的 shell 命令并等待其结果（一次性，跑完自动回收）。

    适合测试、编译、一次性命令等**会自己结束**的操作；命令要下载/起服务等长时间运行时，
    超时未结束**不会杀进程**，而是转入受管句柄返回"仍在运行 + 部分输出"，可再用
    process_wait / process_read / process_kill 管理。

    Args:
        command: 要执行的 shell 命令。
        description: 必填，用人话说明这条命令要做什么、期望的结果（如 "用 pip 下载
            pandas 库并安装"）。审批时会与命令一起展示给人确认。
        cwd: 工作目录，留空则使用服务器启动目录（即工作区根）。目录必须存在。
        timeout: 最多等待多少秒等它结束，默认 300。**超时不杀**，只是停止等待。
    """
    _require_text(command, "command")
    _deny_sudo(command)
    _require_text(description, "description")
    cwd = _resolve_cwd(cwd)
    timeout = max(1, int(timeout))

    rec, _proc = await _spawn(command, cwd)
    rec.description = description
    try:
        code = await asyncio.wait_for(rec.proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        # 超时那一瞬进程可能恰好退出：以 returncode 为准，别误报 running
        if rec.proc.returncode is not None:
            rec.exit_code = rec.proc.returncode
            await _collect(rec)
            return _render_finished(rec, rec.exit_code)
        return _render_running(rec, "等待超时但进程仍在运行")

    rec.exit_code = code
    await _collect(rec)
    return _render_finished(rec, code)


# ------------------------------ 工具：常驻级启动 ------------------------------

@mcp.tool()
@guard
async def start_process(
    command: str,
    description: str,
    cwd: str = "",
    startup_wait: int = 5,
) -> ToolResult:
    """在后台启动一条常驻进程（如后端 dev 服务器），其生命周期由 MCP 管理。

    与 run_command 的区别：立即返回、进程跨轮存活；适合**启动后不自然退出**的服务。
    spawn 后会先探测 startup_wait 秒：若进程在这期间就退出（多半是启动即崩/命令打错），
    直接返回退出码与报错；否则返回 running。启动完成后可用 process_wait 等就绪行、
    process_read 看日志、process_kill 停掉；会话/MCP 服务器退出时进程会被自动杀净。

    Args:
        command: 要启动的命令（通常不会自然结束，如 "uvicorn app:app"）。
        description: 必填，用人话说明启动的是什么服务、为什么需要它常驻。
        cwd: 工作目录，留空则使用服务器启动目录（工作区根）。目录必须存在。
        startup_wait: 探测"启动即崩"的最大等待秒数，默认 5；此间进程仍在跑即视为常驻成功。
    """
    _require_text(command, "command")
    _deny_sudo(command)
    _require_text(description, "description")
    cwd = _resolve_cwd(cwd)
    startup_wait = max(0, int(startup_wait))

    rec, _proc = await _spawn(command, cwd)
    rec.resident = True
    rec.description = description

    if startup_wait > 0:
        try:
            code = await asyncio.wait_for(rec.proc.wait(), timeout=startup_wait)
        except asyncio.TimeoutError:
            code = None
    else:
        code = None

    if code is not None:
        # 启动期即退出：多半是启动失败，把结果回给模型分析
        rec.exit_code = code
        await _collect(rec)
        note = f"启动期间即退出（exit_code={code}），疑似启动失败"
        return ToolResult(success=code == 0, content=f"{note}\n{_render_finished(rec, code).content}")

    return _render_running(rec, "已在后台常驻")


# ------------------------------ 工具：进程管理 ------------------------------

@mcp.tool()
@guard
async def process_wait(process_id: str, timeout: int = 120) -> ToolResult:
    """等待指定进程结束（最多 timeout 秒）。到点仍未结束不杀，返回"仍在运行+新增输出"。

    判断"慢 vs 卡死"的依据：每次返回都带"距上次新输出的秒数"与本次新增输出。
    - 输出在持续增长（下载进度在走）→ 慢，可再去处理别的事、稍后回来再 process_wait；
    - 长期无新输出（百分比卡住）→ 卡死，用 process_kill 终止。
    进程结束/被收取后，其条目即从进程表移除。

    Args:
        process_id: run_command 超时或 start_process 返回的进程号（形如 p1）。
        timeout: 最多等待秒数，默认 120。
    """
    rec = _find(process_id)
    timeout = max(1, int(timeout))
    if rec.exit_code is None:
        try:
            code = await asyncio.wait_for(rec.proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            if rec.proc.returncode is not None:
                rec.exit_code = rec.proc.returncode
                await _collect(rec)
                return _render_finished(rec, rec.exit_code)
            return _render_running(rec, "等待超时仍未结束")
        rec.exit_code = code
    await _collect(rec)
    return _render_finished(rec, rec.exit_code or 0)


@mcp.tool()
@guard
async def process_read(process_id: str) -> ToolResult:
    """读进程自上次读取以来新增的输出与当前状态（不阻塞，读完即返回）。

    常驻服务轮询日志、或等待中的下载看进度都靠它；进程若已结束，本次返回完整剩余输出
    并自动回收其条目。

    Args:
        process_id: 目标进程号（形如 p1）。
    """
    rec = _find(process_id)
    new_text = rec.buffer.read_new(limit=12000)
    if rec.exit_code is not None or rec.proc.returncode is not None:
        if rec.exit_code is None:
            rec.exit_code = rec.proc.returncode
        await _collect(rec)
        return _render_finished(rec, rec.exit_code or 0)

    elapsed = time.monotonic() - rec.started
    idle = max(0.0, time.monotonic() - rec.buffer.last_append_ts)
    lines = [
        f"进程 {rec.id} 仍在运行：已运行 {elapsed:.0f}s，最近一次新输出 {idle:.0f}s 前",
        f"command: {rec.command[:160]}",
    ]
    if new_text.strip():
        lines.append("--- 新增输出 ---")
        lines.append(new_text.rstrip("\n"))
    else:
        lines.append("(本段无新输出)")
    lines.append(f"后续可用 process_wait({rec.id}) 继续等 / process_kill({rec.id}) 终止")
    return ToolResult(success=True, content="\n".join(lines))


@mcp.tool()
@guard
async def process_kill(process_id: str) -> ToolResult:
    """强制终止指定进程（含其子进程树），并从进程表中移除。

    只作用于本会话内经 run_command/start_process 启动的受管进程。进程已自然结束时
    调用它只是确认清理。

    Args:
        process_id: 目标进程号（形如 p1）。
    """
    rec = _find(process_id)
    _kill_tree(rec.proc)
    try:
        await asyncio.wait_for(rec.proc.wait(), timeout=5)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass
    rec.exit_code = rec.proc.returncode if rec.proc.returncode is not None else -9
    await _cancel_readers(rec)
    _PROCS.pop(rec.id, None)
    return ToolResult(
        success=True,
        content=f"进程 {rec.id} 已终止（command: {rec.command[:120]}）",
    )


@mcp.tool()
@guard
async def process_list() -> ToolResult:
    """列出本会话内所有受管进程（运行中与已结束未收取的），用于审计与兜底清理。

    进程会在结束时被 process_wait/process_read/process_kill 收取后从清单移除；
    MCP 服务器退出时会自动杀净全部残留。
    """
    if not _PROCS:
        return ToolResult(success=True, content="当前没有受管进程。")
    lines = [f"受管进程（{len(_PROCS)} 个）："]
    now = time.monotonic()
    for rec in sorted(_PROCS.values(), key=lambda r: r.id):
        kind = "常驻" if rec.resident else "一次性"
        if rec.running:
            idle = max(0.0, now - rec.buffer.last_append_ts)
            lines.append(
                f"- {rec.id} [{kind}] running  已运行{now - rec.started:.0f}s  "
                f"最近输出{idle:.0f}s前  {rec.command[:100]}"
            )
        else:
            lines.append(
                f"- {rec.id} [{kind}] 已结束(exit={rec.exit_code}) 未收取  "
                f"{rec.command[:100]}  ← 用 process_read 收取"
            )
    return ToolResult(success=True, content="\n".join(lines))


# ------------------------------ 退出清理 ------------------------------

def _kill_all_on_exit() -> None:
    for rec in list(_PROCS.values()):
        _kill_tree(rec.proc)


atexit.register(_kill_all_on_exit)


if __name__ == "__main__":
    mcp.run(transport="stdio")
