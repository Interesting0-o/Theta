"""terminal 进程管理器的行为单测（直接调用 MCP 工具原函数，不经过 FastMCP）。

覆盖：description 必填校验 / run_command 完成与退出码 / cwd / 超时不杀并转受管句柄 /
start_process 常驻与启动期探测 / process_wait/read/kill/list / 未知进程号。

注意：这些工具是 async，测试用 asyncio.run 包住单条场景；跨平台命令差异用 cmd 变量。
每个场景都在同一事件循环里创建并 kill 掉受管进程，不留跨测试残留。
"""
import asyncio
import os
import re
import subprocess

from mcp_service import terminal
from mcp_service.terminal import (
    process_kill,
    process_list,
    process_read,
    process_wait,
    run_command,
    start_process,
)

_NT = os.name == "nt"


def _sleep(count: int) -> str:
    """一条"会持续约 count 秒再结束"的跨平台命令。"""
    if _NT:
        # ping -n N 约持续 N-1 秒
        return f"ping -n {count} 127.0.0.1 >nul"
    return f"sleep {count}"


def _long_cmd(prefix: str = "") -> str:
    """会运行较久、且先打一行 prefix 输出的命令（测超时后 partial output）。"""
    if _NT:
        return f"echo {prefix} && ping -n 5 127.0.0.1 >nul" if prefix else f"ping -n 5 127.0.0.1 >nul"
    return f"echo {prefix}; sleep 5" if prefix else "sleep 5"


def _pid(content: str) -> str:
    m = re.search(r"进程 (p\d+) 仍在运行", content)
    assert m, f"内容里没找到运行中的进程号: {content}"
    return m.group(1)


def test_run_command_success():
    async def _run():
        return await run_command("echo hello", description="测试回显")

    res = asyncio.run(_run())
    assert res.success is True
    assert "exit_code: 0" in res.content
    assert "hello" in res.content


def test_run_command_nonzero_exit():
    async def _run():
        return await run_command("exit 3", description="测试退出码")

    res = asyncio.run(_run())
    assert res.success is False
    assert "exit_code: 3" in res.content


def test_run_command_cwd(tmp_path):
    async def _run():
        return await run_command("echo hi", description="测试 cwd", cwd=str(tmp_path))

    res = asyncio.run(_run())
    assert res.success is True
    assert "hi" in res.content


def test_run_command_requires_description():
    async def _run():
        return await run_command("echo hi", description="")

    res = asyncio.run(_run())
    assert res.success is False
    assert res.error_type == "invalid_argument"


def test_run_command_denies_sudo():
    async def _run():
        return await run_command("sudo whoami", description="提权尝试")

    res = asyncio.run(_run())
    assert res.success is False
    assert res.error_type == "invalid_argument"
    assert "sudo" in res.content


def test_start_process_denies_sudo():
    async def _run():
        return await start_process("echo a && sudo b", description="提权尝试")

    res = asyncio.run(_run())
    assert res.success is False
    assert res.error_type == "invalid_argument"
    assert "sudo" in res.content


def test_run_command_rejects_missing_cwd(tmp_path):
    async def _run():
        return await run_command("echo hi", description="x", cwd=str(tmp_path / "不存在"))

    res = asyncio.run(_run())
    assert res.success is False
    assert res.error_type == "invalid_argument"


def test_run_command_timeout_keeps_process_and_kill():
    async def _scenario():
        res = await run_command(_long_cmd(prefix="started"), description="测试超时", timeout=1)
        assert res.success is True
        assert "仍在运行" in res.content
        pid = _pid(res.content)
        assert "started" in res.content  # 超时返回应带上前期已产生的那行输出
        kill = await process_kill(pid)
        assert kill.success is True

    asyncio.run(_scenario())


def test_start_process_resident_lifecycle():
    async def _scenario():
        res = await start_process(_sleep(5), description="测试常驻", startup_wait=1)
        assert res.success is True
        assert "仍在运行" in res.content
        pid = _pid(res.content)

        lst = await process_list()
        assert pid in lst.content

        rd = await process_read(pid)
        assert rd.success is True

        kill = await process_kill(pid)
        assert kill.success is True
        assert pid in kill.content

        lst2 = await process_list()
        assert pid not in lst2.content  # kill 后应已移除

    asyncio.run(_scenario())


def test_process_read_returns_output_of_finished_process():
    """已结束进程的输出必须完整给到模型，不许回"(无输出)"。

    回归：process_read 曾先无条件 read_new 一次（**推进消费游标**），紧接着的"已结束"
    分支却把它丢掉、只渲染 _render_finished——后者内部 drain() 再取就只剩空串，
    等于把"进程没输出"这个错误事实喂给模型（dev server 崩了、后台命令跑完都会中招）。
    """
    # 先打一行标记、再自己跑一会儿：启动时仍在运行（走 process_read 路径），稍后自然结束
    cmd = (
        "echo MARKER_OUTPUT_XYZ && ping -n 3 127.0.0.1 >nul"
        if _NT
        else "echo MARKER_OUTPUT_XYZ; sleep 2"
    )

    async def _scenario():
        res = await start_process(cmd, description="测试已结束进程的输出", startup_wait=0.3)
        assert "仍在运行" in res.content  # 启动时还没结束，否则测不到这条路径
        pid = _pid(res.content)

        await asyncio.sleep(4)  # 等它自然结束、reader 任务收完输出

        rd = await process_read(pid)
        assert "MARKER_OUTPUT_XYZ" in rd.content, rd.content
        assert "(无输出)" not in rd.content

    asyncio.run(_scenario())


def test_start_process_detects_immediate_crash():
    async def _scenario():
        # 一个必然启动即退出的命令（不存在的命令会被 shell 报错退出）
        cmd = "nonexistent_cmd_xyz" if not _NT else "nonexistent_cmd_xyz.exe"
        res = await start_process(cmd, description="测试启动失败", startup_wait=2)
        assert res.success is False or "退出" in res.content or "exit_code" in res.content

    asyncio.run(_scenario())


def test_process_wait_waits_for_finish():
    async def _scenario():
        res = await start_process(_sleep(1), description="测试 wait", startup_wait=0)
        assert res.success is True
        pid = _pid(res.content)
        waited = await process_wait(pid, timeout=10)
        assert waited.success is True
        assert "exit_code: 0" in waited.content

    asyncio.run(_scenario())


def test_process_unknown_id_is_invalid_argument():
    async def _scenario():
        return await process_wait("p_no_such_process", timeout=1)

    res = asyncio.run(_scenario())
    assert res.success is False
    assert res.error_type == "invalid_argument"


def test_spawned_commands_must_not_inherit_stdin():
    """命令的 stdin 必须是 DEVNULL——**不能继承 MCP 服务器的 stdin**。

    回归防线（2026-09-13 实测揪出）：terminal server 的 stdin 就是主程序连过来的 **JSON-RPC
    管道**，没人写、也**永不 EOF**。命令若继承它，凡启动时读一下 stdin 的程序就永远等下去——
    现场是 `git --version` 挂满 `run_command` 的 300 秒超时，而 `uv --version` 不读 stdin 所以
    秒回，表象成了"只有 git 执行不了"（用户看到的是"补环境老是补不全"）。更坏的一种可能是命令
    读到协议字节、把 MCP 会话搞乱。

    这里直接断言 spawn 参数（而不是跑一条"读 stdin"的命令）：pytest 进程自己的 stdin 通常已经是
    EOF，跑命令**测不出**回归——只有断言参数才真的守得住。
    （GitPython 起 git 时显式设了 `stdin=(istream or DEVNULL)`，所以漏的只有本模块这一处。）
    """
    kwargs = terminal._build_spawn_kwargs(None)

    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
