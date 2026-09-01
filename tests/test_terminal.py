from mcp_service.terminal import run_command


def test_run_command_success():
    result = run_command("echo hello")
    assert result.success is True
    assert "exit_code: 0" in result.content
    assert "hello" in result.content


def test_run_command_nonzero_exit():
    result = run_command("exit 3")
    assert result.success is False
    assert "exit_code: 3" in result.content


def test_run_command_cwd(tmp_path):
    result = run_command("echo hi", cwd=str(tmp_path))
    assert result.success is True
    assert "hi" in result.content


def test_run_command_timeout():
    # 强制 timeout 为最小值 1s，并执行一条睡眠命令验证超时路径
    result = run_command("ping -n 5 127.0.0.1 >nul" if __import__("os").name == "nt" else "sleep 5", timeout=1)
    assert result.success is False
    assert "超时" in result.content
