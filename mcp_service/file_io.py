import shutil
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from app.schema.agent_schema import ToolResult

mcp = FastMCP("FileIO")


def _resolve_path(path: str) -> Path:
    return Path(path).expanduser().resolve()


@mcp.tool()
def create_file(path: str, content: str = "") -> ToolResult:
    """
    新建或追加写入一个文件。
    Args:
        path: 包含父级目录的文件地址同时还需要包含文件名以及后缀名。
        content: 要写入文件的内容
    """
    try:
        file_path = _resolve_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("a", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return ToolResult(success=False, content=str(e))
    return ToolResult(success=True, content=f"文件 {path} 已创建并写入")


@mcp.tool()
def create_dir(dir_path: str) -> ToolResult:
    """
    在指定位置新建一个目录
    Args:
        dir_path: 包含父级目录的地址
    """
    try:
        path = _resolve_path(dir_path)
        path.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return ToolResult(success=False, content=str(e))
    return ToolResult(success=True, content=f"目录已创建: {dir_path}")


@mcp.tool()
def delete_file(path: str) -> ToolResult:
    """
    删除一个文件。
    Args:
        path: 索要删除文件的路径
    """
    try:
        file_path = _resolve_path(path)
        if not file_path.exists():
            return ToolResult(success=False, content=f"文件不存在: {file_path}")
        if file_path.is_dir():
            return ToolResult(success=False, content=f"目标不是文件，而是目录: {file_path}")
        file_path.unlink()
        return ToolResult(success=True, content=f"文件已删除: {file_path}")
    except Exception as e:
        return ToolResult(success=False, content=f"删除文件失败: {e}")


@mcp.tool()
def delete_dir(dir_path: str, recursive: bool = False) -> ToolResult:
    """删除目录，可选择是否递归删除。"""
    try:
        dir_path_obj = _resolve_path(dir_path)
        if not dir_path_obj.exists():
            return ToolResult(success=False, content=f"目录不存在: {dir_path_obj}")
        if not dir_path_obj.is_dir():
            return ToolResult(success=False, content=f"目标不是目录: {dir_path_obj}")
        if not recursive and any(dir_path_obj.iterdir()):
            return ToolResult(
                success=False,
                content=f"目录不为空，未开启递归删除: {dir_path_obj}",
            )
        shutil.rmtree(dir_path_obj)
        return ToolResult(success=True, content=f"目录已删除: {dir_path_obj}")
    except Exception as e:
        return ToolResult(success=False, content=f"删除目录失败: {e}")


@mcp.tool()
def copy_path(source: str, destination: str, recursive: bool = True) -> ToolResult:
    """复制文件或目录到指定位置。"""
    try:
        src = _resolve_path(source)
        dst = _resolve_path(destination)
        if not src.exists():
            return ToolResult(success=False, content=f"源路径不存在: {src}")

        if src.is_dir():
            if recursive:
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                dst.mkdir(parents=True, exist_ok=True)
            return ToolResult(success=True, content=f"目录已复制: {src} -> {dst}")

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return ToolResult(success=True, content=f"文件已复制: {src} -> {dst}")
    except Exception as e:
        return ToolResult(success=False, content=f"复制失败: {e}")


@mcp.tool()
def write_file(
    path: str,
    content: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> ToolResult:
    """
    写入文件内容。
    当提供 start_line / end_line 时，按指定行区间做增量替换；
    如果未提供，则直接覆盖整个文件。
    Args:
        path: 所要写入文件的路径
        content: 要写入的内容
        start_line: 写入文件大的开始行数
        end_ine:写入文件的结束行数
    """
    try:
        file_path = _resolve_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        if start_line is None and end_line is None:
            file_path.write_text(content, encoding="utf-8")
            return ToolResult(success=True, content=f"文件已写入: {file_path}")

        start_index = 1 if start_line is None else max(1, start_line)
        end_index = start_index if end_line is None else max(start_index, end_line)

        if start_index < 1 or end_index < start_index:
            return ToolResult(
                success=False,
                content=f"非法行区间: start_line={start_line}, end_line={end_line}",
            )

        if not file_path.exists():
            file_path.write_text("", encoding="utf-8")

        existing = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
        start_pos = start_index - 1
        end_pos = end_index

        if start_pos > len(existing):
            existing.extend(["\n"] * (start_pos - len(existing)))

        existing[start_pos:end_pos] = [content]
        file_path.write_text("".join(existing), encoding="utf-8")
        return ToolResult(
            success=True,
            content=f"文件已按行区间写入: {file_path} (lines {start_index}-{end_index})",
        )
    except Exception as e:
        return ToolResult(success=False, content=f"写入文件失败: {e}")
    
@mcp.tool()
def read_file(path:str)->ToolResult:
    """
    读取指定路径的文件
    Args:
        path:需要读取文件的路径
    """
    try:
        with open(path,"r",encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return ToolResult(success=False,content=str(e))
    return ToolResult(success=True,content=content)


@mcp.tool()
def list_dir(path: str) -> ToolResult:
    """
    列出指定目录下的文件与子目录(按名称排序，目录在前)。
    Args:
        path: 要列出的目录路径
    """
    try:
        dir_path = _resolve_path(path)
        if not dir_path.is_dir():
            return ToolResult(success=False, content=f"路径不是目录或不存在: {dir_path}")
        entries = []
        for entry in sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            kind = "dir" if entry.is_dir() else "file"
            entries.append(f"[{kind}] {entry.name}")
        content = "\n".join(entries) if entries else "(空目录)"
        return ToolResult(success=True, content=f"目录: {dir_path}\n{content}")
    except PermissionError:
        return ToolResult(success=False, content=f"无权限访问目录: {path}")
    except Exception as e:
        return ToolResult(success=False, content=f"列目录失败: {e}")


@mcp.tool()
def get_directory_tree(path: str, max_depth: int = 3) -> ToolResult:
    """
    递归获取目录结构树，可用于了解项目/文件夹的整体布局。
    Args:
        path: 根目录路径
        max_depth: 最大递归深度(默认 3，最小 1)
    """
    try:
        root = _resolve_path(path)
        if not root.is_dir():
            return ToolResult(success=False, content=f"路径不是目录或不存在: {root}")
        max_depth = max(1, int(max_depth))
        lines: list[str] = [f"{root}/"]

        def walk(current: Path, depth: int, prefix: str, seen: set) -> None:
            if depth > max_depth:
                lines.append(f"{prefix}  ... (超出深度 {max_depth})")
                return
            try:
                entries = sorted(
                    current.iterdir(),
                    key=lambda p: (not p.is_dir(), p.name.lower()),
                )
            except PermissionError:
                lines.append(f"{prefix}  (无权限访问)")
                return
            for i, entry in enumerate(entries):
                last = i == len(entries) - 1
                branch = "└── " if last else "├── "
                is_dir = entry.is_dir()
                lines.append(f"{prefix}{branch}{entry.name}{'/' if is_dir else ''}")
                if is_dir:
                    try:
                        real = entry.resolve()
                    except OSError:
                        continue
                    if real in seen:
                        lines.append(f"{prefix}    └── (循环引用，已跳过)")
                        continue
                    seen = seen | {real}
                    child_prefix = prefix + ("    " if last else "│   ")
                    walk(entry, depth + 1, child_prefix, seen)

        walk(root, 1, "", set())
        return ToolResult(success=True, content="\n".join(lines))
    except Exception as e:
        return ToolResult(success=False, content=f"获取目录结构失败: {e}")


if __name__ == "__main__":
    mcp.run(transport="stdio")