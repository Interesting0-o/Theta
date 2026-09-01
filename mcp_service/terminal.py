import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from app.schema.agent_schema import ToolResult

mcp = FastMCP("Terminal")


@mcp.tool()
def run_command(command: str, cwd: str = "", timeout: int = 30) -> ToolResult:
    """
    在终端执行一条 shell 命令并返回输出(Windows 下使用 cmd.exe)。
    注意：该工具具有任意命令执行能力，请谨慎使用。
    Args:
        command: 要执行的命令
        cwd: 工作目录(留空则使用进程当前目录)
        timeout: 超时秒数(默认 30，最小 1)
    """
    kwargs: dict = {}
    if cwd:
        kwargs["cwd"] = Path(cwd).expanduser().resolve()

    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout)),
            **kwargs,
        )
    except subprocess.TimeoutExpired as e:
        detail = f"命令执行超时({timeout}s): {command}"
        partial = getattr(e, "stdout", None)
        if isinstance(partial, str) and partial.strip():
            detail += f"\n部分输出:\n{partial}"
        return ToolResult(success=False, content=detail)
    except FileNotFoundError:
        return ToolResult(success=False, content=f"cwd 不存在或无法访问: {cwd}")
    except Exception as e:
        return ToolResult(success=False, content=f"命令执行失败: {e}")

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    content = f"exit_code: {proc.returncode}\n"
    if stdout:
        content += f"--- stdout ---\n{stdout}\n"
    if stderr:
        content += f"--- stderr ---\n{stderr}\n"
    if not stdout and not stderr:
        content += "(无输出)"
    return ToolResult(success=proc.returncode == 0, content=content)


if __name__ == "__main__":
    mcp.run(transport="stdio")
