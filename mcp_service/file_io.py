"""
文件IO操作的MCP服务器：在工作区沙箱（WORKSPACE_PATH）内提供文件与目录操作。

工具清单：
- create_file / create_dir            新建文件 / 新建目录
- write_file / edit_file / read_file  整文件覆盖 / 字符串锚定增量编辑 / 读取（可按行号分段）
- delete_file / delete_dir            删除文件 / 删除目录（非空需 recursive）
- copy_path                           复制文件或目录
- list_dir / get_directory_tree       列目录 / 递归目录树
- glob                                按文件名/通配模式定位路径
- search_content                      工作区内按内容检索（子串或正则，可带上下文）

安全模型：
- 全部工具（含读操作）都受工作区沙箱约束：相对路径以 WORKSPACE_PATH 为基准解析，
  绝对路径与符号链接先 resolve 到真实路径再校验是否在工作区内，越界抛 WorkspaceViolationError。
- 每个 @mcp.tool 工具都用 @guard 包裹（见 mcp_service/utils.py）：异常分类后只 return
  不 raise——越界→workspace_violation / IO 失败→io_error / 内部 bug→internal_error，
  保证异常不漏到 FastMCP。
"""
import os
import re
import shutil
from pathlib import Path
from mcp.server.fastmcp import FastMCP

from app.exception import ConfigError, InvalidArgumentError, WorkspaceViolationError
from app.schema.agent_schema import ToolResult
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
def read_file(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> ToolResult:
    """
    读取指定路径的文件，可选只读某段行区间，避免大文件整段塞满上下文。
    Args:
        path: 需要读取文件的路径。
        start_line: 可选，起始行号（从 1 计、含）；只传它则从该行读到文件尾。
        end_line: 可选，结束行号（含）；须不小于 start_line。留空默认读到文件尾。
    """
    # 行号参数非法在文件 IO 前拦下，抛 InvalidArgumentError（guard 归成 invalid_argument）
    if start_line is not None and start_line < 1:
        raise InvalidArgumentError(f"start_line 从 1 开始计，收到 {start_line}")
    if end_line is not None and end_line < 1:
        raise InvalidArgumentError(f"end_line 从 1 开始计，收到 {end_line}")
    if start_line is not None and end_line is not None and start_line > end_line:
        raise InvalidArgumentError(
            f"start_line({start_line}) 不能大于 end_line({end_line})"
        )

    file_path = _resolve_path(path)  # 解析路径
    _is_in_workspace(file_path)  # 读取同样受工作区沙箱约束

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"读取文件失败: {e}")

    # 未指定行区间：整文件原样返回（保持原有行为，不附加行号标题，避免污染编辑锚定）
    if start_line is None and end_line is None:
        return ToolResult(success=True, content=content)

    lines = content.splitlines()
    total = len(lines)
    lo = start_line if start_line is not None else 1
    hi = end_line if end_line is not None else total

    if total == 0 or lo > total:
        return ToolResult(
            success=True,
            content=f"（{path} 共 {total} 行；请求的行区间 {lo}-{hi} 落在文件之外，无内容）",
        )

    effective_hi = min(hi, total)
    body = "\n".join(lines[lo - 1 : effective_hi])
    return ToolResult(
        success=True,
        content=f"[{path} 第 {lo}-{effective_hi} 行 / 共 {total} 行]\n{body}",
    )


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


# ---------------- glob：按文件名/通配模式定位 ----------------

def _glob_segment_regex(segment: str) -> str:
    """把单个 glob 段（不含 /）转成正则片段；* 不跨 /（跨目录靠 ** 由外层展开）。"""
    out: list[str] = []
    i, n = 0, len(segment)
    while i < n:
        ch = segment[i]
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            j = segment.find("]", i + 1)
            if j == -1:  # 缺闭合括号：整段按字面处理
                out.append(re.escape(segment[i:]))
                break
            cls = segment[i + 1 : j]
            if cls.startswith("!"):  # shell 的 [!..] 取反 → 正则的 [^..]
                cls = "^" + cls[1:]
            cls = cls.replace("\\", "\\\\")
            out.append(f"[{cls}]")
            i = j
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


def _compile_glob(pattern: str) -> re.Pattern:
    """把以 root 为基准的 shell 通配编译成匹配相对路径的整串正则。"""
    segments = pattern.split("/")
    regex_parts: list[str] = []
    for i, seg in enumerate(segments):
        # 普通段之间补回被 split 吃掉的字面 '/'（如 "sub/*.py"）；紧跟 '**' 后的段
        # 不再补——'**' 的展开式自带尾部 '/'，重复会要求两个斜杠。
        if i > 0 and segments[i - 1] != "**":
            regex_parts.append("/")
        if seg == "**":
            if i == len(segments) - 1:
                # 结尾 '**'：匹配该前缀下的任意深度（文件或目录名），如 "a/**"
                regex_parts.append("(?:[^/]+/)*[^/]*")
            else:
                regex_parts.append("(?:[^/]+/)*")
        else:
            regex_parts.append(_glob_segment_regex(seg))
    return re.compile("^" + "".join(regex_parts) + "$")


@mcp.tool()
@guard
def glob(
    pattern: str,
    path: str = ".",
    include_dirs: bool = False,
    max_results: int = 200,
) -> ToolResult:
    """
    按文件名/通配模式在工作区内定位路径，返回相对工作区根的清单。
    与 search_content（按内容）互补：知道"名字长什么样"找路径用它（找出所有
    *_test.py、某目录下的 .ts 文件等），比 run_command 里裸 find/fd 免审批。

    匹配规则（shell 通配，含 **）：
    - pattern 不含 '/'：按"任意深度下的名字"匹配，如 "*.py" 会命中深层 .py 文件；
    - pattern 含 '/'：按相对 path 的路径匹配，** 表示 0 或多级目录
      （如 "src/**/*.ts"、"tests/**/test_*.py"）；
    - 只列出名字、不读内容；检索时跳过隐藏目录（.git/.venv 等），但目录里以点开头
      的文件（如 .env.example）只要名字匹配就会列出。
    沙箱：结果只可能落在 path 起始目录内（path 须在工作区），不会触达工作区外。

    Args:
        pattern: shell 通配模式，如 "*.py"、"test_*.py"、"src/**/*.ts"。
        path: 起始目录，默认整个工作区；须在工作区内。
        include_dirs: 是否也列出目录（目录会带尾 '/'），默认 False（只列文件）。
        max_results: 最多返回多少条命中，超出即截断并提示（默认 200）。
    """
    if not pattern or not pattern.strip():
        raise InvalidArgumentError("pattern 不能为空，给出要找的文件名/通配模式")

    root = _resolve_path(path)
    _is_in_workspace(root)  # 起始目录越界抛给 guard
    if not root.is_dir():
        return ToolResult(success=False, content=f"路径不是目录或不存在: {root}")

    max_results = max(1, int(max_results))
    stripped = pattern.strip()
    has_dir = "/" in stripped
    try:
        if has_dir:
            matcher = _compile_glob(stripped)

            def _match(entry: Path) -> bool:
                return matcher.fullmatch(entry.relative_to(root).as_posix()) is not None
        else:
            matcher = re.compile("^" + _glob_segment_regex(stripped) + "$")

            def _match(entry: Path) -> bool:
                return matcher.fullmatch(entry.name) is not None
    except re.error as e:
        raise InvalidArgumentError(f"通配模式 {pattern!r} 无法解析: {e}") from e

    hits: list[str] = []
    capped = False

    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过隐藏目录：不向 .git/.venv 等内部钻（但同目录下的点文件仍会参与匹配）
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        if include_dirs:
            for name in dirnames:
                entry = Path(dirpath) / name
                if _match(entry):
                    hits.append(entry.relative_to(root).as_posix() + "/")
                    if len(hits) >= max_results:
                        capped = True
                        break
        if capped:
            break
        for name in sorted(filenames):
            entry = Path(dirpath) / name
            if _match(entry):
                hits.append(entry.relative_to(root).as_posix())
                if len(hits) >= max_results:
                    capped = True
                    break
        if capped:
            break

    if not hits:
        return ToolResult(success=True, content=f"未在 {root} 中找到匹配 {pattern!r} 的路径")

    body = (
        f"命中过多，仅显示前 {max_results} 条（可加长 pattern 或收窄 path）：\n"
        if capped
        else f"共命中 {len(hits)} 条：\n"
    )
    return ToolResult(success=True, content=body + "\n".join(hits))


@mcp.tool()
@guard
def search_content(
    query: str,
    path: str = ".",
    max_results: int = 50,
    extensions: list[str] | None = None,
    use_regex: bool = False,
    case_sensitive: bool = False,
    context_lines: int = 0,
) -> ToolResult:
    """
    在工作区文件中按内容检索，返回命中位置（相对路径:行号:该行内容）。

    这是"grep 式"检索，用来定位某段逻辑 / 某句报错文本 / 某个标识符出现在哪些文件。
    默认是大小写不敏感的子串匹配；需要更精确时可开正则、或带上下文减少一次 read_file。
    找到位置后，可用 read_file 读取对应文件（配合其行号参数）取上下文。

    匹配规则：
    - 默认：query 按纯文本子串、忽略大小写（与原行为一致）；
      case_sensitive=True 则区分大小写；
    - use_regex=True：query 按 Python 正则编译（非法会报错并带原因，可修正重试），
      此时 case_sensitive=True 会去掉 IGNORECASE；
    - context_lines=N：每个命中附带前后 N 行（N=0 只返回命中行本身，默认）。
    检索本身：自动跳过隐藏目录(. 开头)、二进制文件与指向工作区外的符号链接；
    path/extensions/max_results 与原来一致。

    Args:
        query: 检索词：纯文本子串（默认）或正则（use_regex=True）。
        path: 检索的起始目录，默认整个工作区；相对/绝对路径均可，须在工作区内。
        max_results: 最多返回多少条命中，超出即截断并提示。
        extensions: 只在这些后缀里检索（如 [".py", ".md"]，不带点也接受）；默认不限定。
        use_regex: query 是否按正则匹配，默认 False（纯子串）。
        case_sensitive: 是否区分大小写，默认 False（忽略）。
        context_lines: 每个命中附带的前后行数，默认 0（只返回命中行本身）。
    """
    root = _resolve_path(path)
    _is_in_workspace(root)
    if not root.is_dir():
        return ToolResult(success=False, content=f"路径不是目录或不存在: {root}")

    if not query or not query.strip():
        return ToolResult(success=False, content="query 不能为空")

    max_results = max(1, max_results)
    context_lines = max(0, int(context_lines or 0))

    # 匹配器：正则 / 子串，按 case_sensitive 决定是否忽略大小写
    if use_regex:
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            matcher = re.compile(query, flags)
        except re.error as e:
            raise InvalidArgumentError(f"query 不是合法正则: {e}") from e

        def _is_match(line: str) -> bool:
            return matcher.search(line) is not None
    else:
        if case_sensitive:
            def _is_match(line: str) -> bool:
                return query in line
        else:
            query_lower = query.lower()

            def _is_match(line: str) -> bool:
                return query_lower in line.lower()

    ext_set = None
    if extensions:
        ext_set = {e if e.startswith(".") else f".{e}" for e in extensions}
        ext_set = {e.lower() for e in ext_set}

    lines_out: list[str] = []
    total_hits = 0
    capped = False

    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过隐藏目录（.git / .venv / node_modules 等以点开头的）
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if capped:
            break
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
            text_lines = text.splitlines()

            # 本文件命中的行号（1 起始），截断到剩余额度再算上下文窗口
            matched = [no for no, line in enumerate(text_lines, 1) if _is_match(line)]
            if not matched:
                continue
            room = max_results - total_hits
            if room <= 0:
                capped = True
                break
            if len(matched) > room:
                matched = matched[:room]
                capped = True
            total_hits += len(matched)

            rel = real.relative_to(WORKSPACE_PATH)
            if context_lines:
                shown: set[int] = set()
                for no in matched:
                    shown.update(
                        range(
                            max(1, no - context_lines),
                            min(len(text_lines), no + context_lines) + 1,
                        )
                    )
                lines_out.extend(f"{rel}:{no}: {text_lines[no - 1]}" for no in sorted(shown))
            else:
                lines_out.extend(f"{rel}:{no}: {text_lines[no - 1]}" for no in matched)

            if capped:
                break
        if capped:
            break

    if total_hits == 0:
        return ToolResult(success=True, content=f"未在 {root} 中找到包含 {query!r} 的文本")

    ctx = f"（含每处前后 {context_lines} 行）" if context_lines else ""
    body = (
        f"命中过多，仅显示前 {max_results} 条命中{ctx}：\n"
        if capped
        else f"共命中 {total_hits} 条{ctx}：\n"
    )
    return ToolResult(success=True, content=body + "\n".join(lines_out))


if __name__ == "__main__":
    mcp.run(transport="stdio")