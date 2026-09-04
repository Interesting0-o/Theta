"""
文件IO操作的MCP服务器：在工作区沙箱（WORKSPACE_PATH）内提供文件与目录操作。

工具清单：
- create_file / create_dir            新建文件 / 新建目录
- write_file / edit_file / read_file  整文件覆盖 / 字符串锚定增量编辑 / 读取
- delete_file / delete_dir            删除文件 / 删除目录（非空需 recursive）
- copy_path                           复制文件或目录
- list_dir / get_directory_tree       列目录 / 递归目录树
- search_content                      工作区内按内容检索（大小写不敏感子串）

安全模型：
- 全部工具（含读操作）都受工作区沙箱约束：相对路径以 WORKSPACE_PATH 为基准解析，
  绝对路径与符号链接先 resolve 到真实路径再校验是否在工作区内，越界抛 WorkspaceViolationError。
- 每个 @mcp.tool 工具都用 @guard 包裹（见 mcp_service/utils.py）：异常分类后只 return
  不 raise——越界→workspace_violation / IO 失败→io_error / 内部 bug→internal_error，
  保证异常不漏到 FastMCP。
"""
import shutil
import os
from pathlib import Path
from app.exception import WorkspaceViolationError
from mcp.server.fastmcp import FastMCP
from app.schema.agent_schema import ToolResult
from app.exception import ConfigError
from mcp_service.utils import guard

#----------------环境变量注入处理---------------#

_workspace_path = os.environ.get("WORKSPACE_PATH")

if _workspace_path is None:
    raise ConfigError("WORKSPACE_PATH 未设置")

WORKSPACE_PATH = Path(_workspace_path).resolve()

if not WORKSPACE_PATH.exists():
    raise ConfigError(f"agent工作区路径{WORKSPACE_PATH} 不存在")



#----------------------路径处理----------------------#

def _resolve_path(path: str) -> Path:
    """
    解析路径：相对路径以工作区 WORKSPACE_PATH 为基准解析成绝对路径，
    绝对路径原样使用；随后 resolve() 消掉 ../ 与符号链接得到真实路径。
    注意：不解析为基于 cwd（MCP 子进程 cwd 是项目根），否则相对路径会因
    落在工作区外而被误判越界。
    """
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raw = WORKSPACE_PATH / raw
    return raw.resolve()

#----------------------路径检查----------------------#

def _is_in_workspace(path: Path) -> None:
    """
    检查路径是否在工作区路径内，如果不在则抛出异常。
    """
    if not path.is_relative_to(WORKSPACE_PATH):
        raise WorkspaceViolationError(str(path), str(WORKSPACE_PATH))

#---------------------MCP服务------------------------#

mcp = FastMCP("FileIO")

@mcp.tool()
@guard
def create_file(path: str, content: str = "") -> ToolResult:
    """
    新建一个文件并写入内容；若文件已存在则报错（改已有文件请用 edit_file / write_file）。
    Args:
        path: 包含父级目录的文件地址同时还需要包含文件名以及后缀名。
        content: 要写入文件的内容,如果不提供content参数,则默认为空
    """
    # 解析与越界校验放在 try 之外：越界会抛 WorkspaceViolationError，冒泡交给 guard 分类，
    # 而不被工具自己的 except 吞掉。
    file_path = _resolve_path(path)
    _is_in_workspace(file_path)

    try:
        if file_path.exists():
            return ToolResult(
                success=False,
                content=f"文件已存在: {file_path}（改已有文件请用 edit_file / write_file）",
            )
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    except OSError as e:
        # 环境性失败（磁盘满/无权限等）→ 返回数据让模型可重试；内部 bug 交给 guard
        return ToolResult(success=False, error_type="io_error", content=f"写入文件失败: {e}")
    return ToolResult(success=True, content=f"文件 {path} 已创建并写入")


@mcp.tool()
@guard
def create_dir(dir_path: str) -> ToolResult:
    """
    在指定位置新建一个目录
    Args:
        dir_path: 包含父级目录的地址
    """
    path = _resolve_path(dir_path)
    _is_in_workspace(path)

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"创建目录失败: {e}")
    return ToolResult(success=True, content=f"目录已创建: {dir_path}")


@mcp.tool()
@guard
def delete_file(path: str) -> ToolResult:
    """
    删除一个文件。
    Args:
        path: 索要删除文件的路径
    """
    file_path = _resolve_path(path)
    _is_in_workspace(file_path)  # 检查路径是否在工作区路径内

    try:
        if not file_path.exists():
            return ToolResult(success=False, content=f"文件不存在: {file_path}")
        if file_path.is_dir():
            return ToolResult(success=False, content=f"目标不是文件，而是目录: {file_path}")
        file_path.unlink()
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"删除文件失败: {e}")
    return ToolResult(success=True, content=f"文件已删除: {file_path}")


@mcp.tool()
@guard
def delete_dir(dir_path: str, recursive: bool = False) -> ToolResult:
    """删除目录，可选择是否递归删除。"""
    dir_path_obj = _resolve_path(dir_path)
    _is_in_workspace(dir_path_obj)  # 检查路径是否在工作区路径内

    try:
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
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"删除目录失败: {e}")
    return ToolResult(success=True, content=f"目录已删除: {dir_path_obj}")


@mcp.tool()
@guard
def copy_path(source: str, destination: str, recursive: bool = True) -> ToolResult:
    """复制文件或目录到指定位置。"""
    src = _resolve_path(source)
    _is_in_workspace(src)  # 检查源路径是否在工作区路径内
    dst = _resolve_path(destination)
    _is_in_workspace(dst)  # 检查目标路径是否在工作区路径内

    try:
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
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"复制失败: {e}")


@mcp.tool()
@guard
def write_file(path: str, content: str) -> ToolResult:
    """
    整文件覆盖写入。小范围修改请用 edit_file（字符串锚定），不要整文件重写。
    Args:
        path: 所要写入文件的路径
        content: 要写入的完整内容（覆盖原文件）
    """
    file_path = _resolve_path(path)
    _is_in_workspace(file_path)  # 检查路径是否在工作区路径内

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"写入文件失败: {e}")
    return ToolResult(success=True, content=f"文件已写入: {file_path}")


@mcp.tool()
@guard
def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> ToolResult:
    """
    用字符串锚定做增量编辑：把文件中的 old_string 精确替换为 new_string。

    这是修改已有代码的首选方式——不需要数行号（行号会随前序编辑漂移），也不重写整个文件。
    规则：
    - old_string 必须在文件中精确出现（含空白与缩进），否则报错并提示先 read_file 核对；
    - 默认要求唯一匹配：多处出现时返回命中次数，需加长锚定片段使其唯一，或传 replace_all；
    - replace_all=True 时替换全部匹配。

    Args:
        path: 要编辑的文件路径
        old_string: 要被替换的原文片段（须唯一匹配）
        new_string: 替换后的新内容
        replace_all: 是否替换全部匹配，默认 False
    """
    file_path = _resolve_path(path)
    _is_in_workspace(file_path)  # 检查路径是否在工作区路径内

    try:
        if not file_path.exists():
            return ToolResult(success=False, content=f"文件不存在: {file_path}")
        text = file_path.read_text(encoding="utf-8")
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"读取文件失败: {e}")

    if not old_string:
        return ToolResult(success=False, error_type="invalid_argument", content="old_string 不能为空")

    count = text.count(old_string)
    if count == 0:
        return ToolResult(
            success=False,
            error_type="invalid_argument",
            content="未找到 old_string（文件内容可能已变化，请先 read_file 核对后再编辑）",
        )
    if count > 1 and not replace_all:
        return ToolResult(
            success=False,
            error_type="invalid_argument",
            content=f"old_string 命中 {count} 处，不是唯一匹配：请加长锚定片段使其唯一，或传 replace_all=True",
        )

    updated = text.replace(old_string, new_string)
    try:
        file_path.write_text(updated, encoding="utf-8")
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"写入文件失败: {e}")

    times = count if replace_all else 1
    return ToolResult(success=True, content=f"文件 {file_path} 已编辑（替换 {times} 处）")

@mcp.tool()
@guard
def read_file(path: str) -> ToolResult:
    """
    读取指定路径的文件
    Args:
        path:需要读取文件的路径
    """
    file_path = _resolve_path(path)  # 解析路径
    _is_in_workspace(file_path)  # 读取同样受工作区沙箱约束

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"读取文件失败: {e}")
    return ToolResult(success=True, content=content)


@mcp.tool()
@guard
def list_dir(path: str) -> ToolResult:
    """
    列出指定目录下的文件与子目录(按名称排序，目录在前)。
    Args:
        path: 要列出的目录路径
    """
    dir_path = _resolve_path(path)
    _is_in_workspace(dir_path)  # 列出同样受工作区沙箱约束

    try:
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
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"列目录失败: {e}")


@mcp.tool()
@guard
def get_directory_tree(path: str, max_depth: int = 3) -> ToolResult:
    """
    递归获取目录结构树，可用于了解项目/文件夹的整体布局。
    Args:
        path: 根目录路径
        max_depth: 最大递归深度(默认 3，最小 1)
    """
    try:
        root = _resolve_path(path)
        _is_in_workspace(root)  # 目录结构读取同样受工作区沙箱约束；越界抛给 guard，不被下方 OSError 吞掉
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
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"获取目录结构失败: {e}")


@mcp.tool()
@guard
def search_content(
    query: str,
    path: str = ".",
    max_results: int = 50,
    extensions: list[str] | None = None,
) -> ToolResult:
    """
    在工作区文件中按内容检索：大小写不敏感的子串匹配，返回命中位置（相对路径:行号:该行内容）。

    这是"grep 式"检索，用来定位某段逻辑 / 某句报错文本 / 某个标识符出现在哪些文件。
    找到位置后，可用 read_file 读取对应文件的上下文。

    Args:
        query: 要检索的字符串。纯文本子串、大小写不敏感，不是正则。
        path: 检索的起始目录，默认整个工作区；相对/绝对路径均可，须在工作区内。
        max_results: 最多返回多少条命中，超出即截断并提示。
        extensions: 只在这些后缀里检索（如 [".py", ".md"]，不带点也接受）；默认不限定，
            但自动跳过隐藏目录(. 开头)、二进制文件与指向工作区外的符号链接。
    """
    root = _resolve_path(path)
    _is_in_workspace(root)
    if not root.is_dir():
        return ToolResult(success=False, content=f"路径不是目录或不存在: {root}")

    if not query or not query.strip():
        return ToolResult(success=False, content="query 不能为空")

    max_results = max(1, max_results)
    query_lower = query.lower()
    ext_set = None
    if extensions:
        ext_set = {e if e.startswith(".") else f".{e}" for e in extensions}
        ext_set = {e.lower() for e in ext_set}

    hits: list[str] = []
    capped = False

    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过隐藏目录（.git / .venv / node_modules 等以点开头的）
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for filename in filenames:
            if filename.startswith("."):
                continue
            file_path = Path(dirpath) / filename
            try:
                real = file_path.resolve()
            except OSError:
                continue
            # 沙箱一致性：符号链接指向工作区外 → 跳过，避免把外部文件内容搜进来
            if not real.is_relative_to(WORKSPACE_PATH):
                continue
            if ext_set is not None and real.suffix.lower() not in ext_set:
                continue
            try:
                data = real.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:4096]:  # 粗略二进制探测，跳过
                continue
            text = data.decode("utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if query_lower in line.lower():
                    rel = real.relative_to(WORKSPACE_PATH)
                    hits.append(f"{rel}:{lineno}: {line}")
                    if len(hits) >= max_results:
                        capped = True
                        break
            if capped:
                break
        if capped:
            break

    if not hits:
        return ToolResult(success=True, content=f"未在 {root} 中找到包含 {query!r} 的文本")

    body = (
        f"命中过多，仅显示前 {max_results} 条（可缩小 path/extensions 或收窄 query）：\n"
        if capped
        else f"共命中 {len(hits)} 条：\n"
    )
    return ToolResult(success=True, content=body + "\n".join(hits))


if __name__ == "__main__":
    mcp.run(transport="stdio")