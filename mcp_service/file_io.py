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
import tempfile
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

#----------------------二进制探测----------------------

def _is_binary(data: bytes) -> bool:
    """粗略二进制探测：头部 4KB 里出现 NUL 字节即按二进制处理。

    原 search_content 的内联判定；read_file 也要用（同一语义），故提取成共享函数——
    两处口径必须一致，改一处漏一处就会出现"搜索跳过、read_file 炸掉"的分裂行为。
    """
    return b"\x00" in data[:4096]


def _format_size(size: int) -> str:
    """文件大小的人类可读形式（B / KB / MB / GB / TB，非 B 保留一位小数）。"""
    if size < 1024:
        return f"{size} B"
    value = size / 1024
    for unit in ("KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


# 注：这里曾有一张 _MAGIC_PREFIXES 魔数表（按文件头猜"PNG 图片/PDF 文档"这类标签）。2026-09-15
# 删除：它只是回执里的一句显示标签、无任何分支依赖它，而"能不能当文本读"已由 _is_binary 定性；
# 回执里本就回显了 path（后缀在内），这张表在多数场景是冗余的，又永远覆盖不全（其余一律印
# "未知类型"）——还给 skills/github 复制了第二份。判定"是二进制"就够了，猜"是什么"交给模型。


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
    """复制文件或目录到指定位置。

    Args:
        source: 源路径（文件或目录），须在工作区内。
        destination: 目标路径，须在工作区内。
        recursive: 源是**目录**时是否连内容一起复制（默认 True）。源是文件时该参数无意义。
            目录传 False 会直接失败——它只建一个空的目标目录、什么都不复制，回"已复制"
            等于给模型一个假事实（旧实现就是这么做的）。
    """
    src = _resolve_path(source)
    _is_in_workspace(src)  # 检查源路径是否在工作区路径内
    dst = _resolve_path(destination)
    _is_in_workspace(dst)  # 检查目标路径是否在工作区路径内

    try:
        if not src.exists():
            return ToolResult(success=False, content=f"源路径不存在: {src}")

        if src.is_dir():
            if not recursive:
                return ToolResult(
                    success=False,
                    error_type="invalid_argument",
                    content=(
                        f"{src} 是目录：recursive=False 只会建一个空目标目录、不复制任何内容，"
                        f"故不予执行。要复制目录及其内容请传 recursive=True（默认）"
                    ),
                )
            shutil.copytree(src, dst, dirs_exist_ok=True)
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
        # 与 read_file 同一套判定（先字节探测、再按文本解码）：UnicodeDecodeError 不是 OSError
        # 子类，原先它冒到 guard 会被误判成 internal_error（回执还写"无需重试"）——中文 Windows
        # 上 GBK 源文件很常见，模型会因此得出误导结论。二进制同样落在这里（解不开 UTF-8）。
        with open(file_path, "rb") as f:
            head = f.read(4096)
        if _is_binary(head):
            size = file_path.stat().st_size
            return ToolResult(
                success=True,
                content=f"{path} 是二进制文件，大小 {size} 字节，无法作为文本编辑",
            )
        text = file_path.read_text(encoding="utf-8")
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"读取文件失败: {e}")
    except UnicodeDecodeError:
        # 没过 NUL 探测但解不开 UTF-8：多半是 GBK 等本地编码的文本。措辞与 read_file 对齐，
        # 给可预期回执，不装作读到了、也不当内部错误。
        size = file_path.stat().st_size
        return ToolResult(
            success=True,
            content=(
                f"{path} 不是 UTF-8 编码的文本（可能是 GBK/latin-1 等编码），"
                f"大小 {size} 字节，无法按文本读取；如需内容可用终端命令转码"
            ),
        )

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


# ---------------- read_file：大文件门限 / 预览 / 流式区间 ----------------
# 文件超过 GATE 且无行区间：只回预览，不整读；带行区间：流式读到 end_line。
READ_FILE_GATE_BYTES = 64 * 1024
READ_FILE_PREVIEW_LINES = 200
READ_FILE_PREVIEW_CHARS = 16 * 1024
# 单行上限：区间读取时超长行截断到这么多字符，并跳过该行其余部分（内存有界）。
READ_FILE_LINE_MAX_CHARS = 16 * 1024


def _binary_receipt(path: str, size: int) -> ToolResult:
    return ToolResult(
        success=True,
        content=f"{path} 是二进制文件，大小 {size} 字节，无法作为文本读取",
    )


def _non_utf8_receipt(path: str, size: int) -> ToolResult:
    return ToolResult(
        success=True,
        content=(
            f"{path} 不是 UTF-8 编码的文本（可能是 GBK/latin-1 等编码），"
            f"大小 {size} 字节，无法按文本读取；如需内容可用终端命令转码"
        ),
    )


def _preview_large_file(file_path: Path, path: str, size: int) -> ToolResult:
    """大文件无行区间：只读前 N 行 / 前 M 字符，绝不整读。"""
    lines: list[str] = []
    used = 0
    hit_eof = False
    truncated = False
    try:
        with open(file_path, "r", encoding="utf-8", newline=None) as f:
            for _ in range(READ_FILE_PREVIEW_LINES):
                remaining = READ_FILE_PREVIEW_CHARS - used
                if remaining <= 0:
                    break
                line = f.readline(remaining + 1)
                if not line:
                    hit_eof = True
                    break
                if len(line) > remaining:
                    lines.append(line[:remaining] + "…\n")
                    truncated = True
                    break
                lines.append(line)
                used += len(line)
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"读取文件失败: {e}")
    except UnicodeDecodeError:
        return _non_utf8_receipt(path, size)

    if truncated:
        state = f"已显示前 {len(lines)} 行（最后一行过长，已截断）；后续请用 start_line/end_line 读取"
    elif hit_eof:
        state = f"已显示全文（{len(lines)} 行）"
    else:
        state = f"已显示前 {len(lines)} 行；后续请用 start_line/end_line 读取"
    header = f"[{path} 大小 {_format_size(size)}；{state}]"
    return ToolResult(success=True, content=header + "\n" + "".join(lines))


def _iter_capped_lines(f, cap: int):
    """逐逻辑行读取；单行超过 cap 字符时截断并跳过其余部分（内存有界）。

    产出 (line_no, text, truncated)。跳过的部分也要读完才能找到下一个换行，但只按
    `cap + 1` 字符一块地读，不会把整行一次性放进内存。
    """
    no = 0
    while True:
        first = f.readline(cap + 1)
        if not first:
            return
        no += 1
        if first.endswith("\n") or len(first) <= cap:
            yield no, first, False
            continue

        head = first[:cap]
        total = len(first)
        chunk = first
        while not chunk.endswith("\n"):
            chunk = f.readline(cap + 1)
            if not chunk:
                break
            total += len(chunk)
        content_len = total - 1 if chunk.endswith("\n") else total
        yield (
            no,
            head + f"…[该行过长，已截断，原长 {content_len} 字符]…\n",
            True,
        )


def _read_range_large(
    file_path: Path,
    path: str,
    size: int,
    start_line: int | None,
    end_line: int | None,
) -> ToolResult:
    """大文件行区间：流式读到 end_line，不整文件读入。

    - 显式给 end_line：按请求读；
    - 只给 start_line（end_line=None）：单次最多 READ_FILE_PREVIEW_LINES 行，避免隐式读到 EOF；
    - 单行超过 READ_FILE_LINE_MAX_CHARS：截断该行并在头部标注。
    """
    lo = start_line if start_line is not None else 1
    capped = end_line is None
    hard_end = end_line if end_line is not None else lo + READ_FILE_PREVIEW_LINES - 1

    kept: list[str] = []
    last_no = 0
    hit_eof = True
    truncated_any = False
    try:
        with open(file_path, "r", encoding="utf-8", newline=None) as f:
            for no, text, truncated in _iter_capped_lines(f, READ_FILE_LINE_MAX_CHARS):
                last_no = no
                if no < lo:
                    continue
                if no > hard_end:
                    hit_eof = False
                    break
                kept.append(text.rstrip("\n"))
                truncated_any = truncated_any or truncated
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"读取文件失败: {e}")
    except UnicodeDecodeError:
        return _non_utf8_receipt(path, size)

    if not kept:
        wanted = f"{lo}-{end_line}" if end_line is not None else f"{lo}-文件尾"
        return ToolResult(
            success=True,
            content=(
                f"（{path} 大小 {_format_size(size)}；请求的行区间 {wanted} "
                f"落在文件之外，无内容；文件共 {last_no} 行）"
            ),
        )

    hi = lo + len(kept) - 1
    header = f"{path} 第 {lo}-{hi} 行"
    if hit_eof:
        header += f" / 共 {last_no} 行"
    header += f"；文件大小 {_format_size(size)}"
    if capped and not hit_eof:
        header += (
            f"；只给 start_line 时大文件单次最多 {READ_FILE_PREVIEW_LINES} 行，"
            f"继续请设 start_line={hi + 1}"
        )
    if truncated_any:
        header += "；部分超长行已截断"

    body = "\n".join(kept)
    return ToolResult(success=True, content=f"[{header}]\n{body}")


@mcp.tool()
@guard
def read_file(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> ToolResult:
    """
    读取指定路径的文件，可选只读某段行区间，避免大文件整段塞满上下文。

    大文件处理：
    - 无行区间且文件超过 READ_FILE_GATE_BYTES（64 KB）：只返回预览（前 N 行 / 前 M 字符），
      头部带文件大小与续读提示，**不整读**；
    - 带行区间：流式读到 end_line，不整文件读入；大文件头部不带总行数；
    - 只给 start_line 时，大文件单次最多 READ_FILE_PREVIEW_LINES 行（继续请把下一行作为
      start_line）；单行超过 READ_FILE_LINE_MAX_CHARS 会截断并在头部标注。

    二进制文件（图片/压缩包等）与非 UTF-8 文本不会报错，返回说明性回执（类型 + 大小）。

    Args:
        path: 需要读取文件的路径。
        start_line: 可选，起始行号（从 1 计、含）；只传它则从该行往后读（大文件单次最多 200 行）。
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

    # 先以字节探测：二进制/编码错误要在整读之前识别；stat 拿大小决定走整读还是预览/流式。
    try:
        with open(file_path, "rb") as f:
            head = f.read(4096)
        size = file_path.stat().st_size
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"读取文件失败: {e}")

    if _is_binary(head):
        return _binary_receipt(path, size)

    # 无行区间：小文件保持"原样返回、无头部"；大文件只回预览，不整读。
    if start_line is None and end_line is None:
        if size > READ_FILE_GATE_BYTES:
            return _preview_large_file(file_path, path, size)
        try:
            with open(file_path, "rb") as f:
                data = f.read()
            # 与旧实现一致：universal newlines 会把 \r\n / \r 翻成 \n，编辑锚定依赖这一点。
            content = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except OSError as e:
            return ToolResult(success=False, error_type="io_error", content=f"读取文件失败: {e}")
        except UnicodeDecodeError:
            return _non_utf8_receipt(path, size)
        return ToolResult(success=True, content=content)

    # 带行区间：大文件流式读到 end_line；小文件保留整读以给出"共 N 行"。
    if size > READ_FILE_GATE_BYTES:
        return _read_range_large(file_path, path, size, start_line, end_line)

    try:
        with open(file_path, "rb") as f:
            data = f.read()
        content = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"读取文件失败: {e}")
    except UnicodeDecodeError:
        return _non_utf8_receipt(path, size)

    lines = content.splitlines()
    total = len(lines)
    lo = start_line if start_line is not None else 1
    hi = end_line if end_line is not None else total

    # lo ≥ 1（上游已校验），故空文件（total == 0）天然落在 lo > total 里
    if lo > total:
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


# ---------------- 目录列表/树：单目录条目上限与完整清单落盘 ----------------
MAX_DIR_ENTRIES = 150
DUMP_DIR_REL = Path(".theta") / "tmp"


def _create_dump_file(prefix: str):
    """在工作区 .theta/tmp 下创建 UTF-8 转储文件；失败返回 (None, None)。"""
    try:
        dump_dir = WORKSPACE_PATH / DUMP_DIR_REL
        dump_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f"{prefix}_", suffix=".txt", dir=dump_dir)
        os.close(fd)
        try:
            file = open(name, "w", encoding="utf-8")
        except OSError:
            Path(name).unlink(missing_ok=True)
            raise
        rel = Path(name).relative_to(WORKSPACE_PATH).as_posix()
        return file, rel
    except OSError:
        return None, None


def _discard_dump(dump_file, dump_rel) -> None:
    """关闭并删除转储文件（写失败或最终不需要时）。"""
    if dump_file is not None:
        try:
            dump_file.close()
        except OSError:
            pass
    if dump_rel:
        try:
            (WORKSPACE_PATH / dump_rel).unlink(missing_ok=True)
        except OSError:
            pass


@mcp.tool()
@guard
def list_dir(path: str) -> ToolResult:
    """
    列出指定目录下的文件与子目录(按名称排序，目录在前)。

    文件会附带大小（B/KB/MB），用于判断是否值得整文件读入；目录不带大小。
    单个目录最多显示 MAX_DIR_ENTRIES（150）项；超出时完整清单写入工作区
    `.theta/tmp/`，回执里给出可直接用于 read_file 的相对路径。

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
            if entry.is_dir():
                entries.append(f"[dir] {entry.name}")
                continue
            try:
                size = _format_size(entry.stat().st_size)
            except OSError:
                size = "?"
            entries.append(f"[file] {entry.name}  ({size})")

        if not entries:
            return ToolResult(success=True, content=f"目录: {dir_path}\n(空目录)")
        if len(entries) <= MAX_DIR_ENTRIES:
            return ToolResult(success=True, content=f"目录: {dir_path}\n" + "\n".join(entries))

        dump_file, dump_rel = _create_dump_file("list")
        if dump_file is not None:
            try:
                dump_file.write(f"目录: {dir_path}\n" + "\n".join(entries) + "\n")
                dump_file.close()
            except OSError:
                _discard_dump(dump_file, dump_rel)
                dump_rel = None

        shown = entries[:MAX_DIR_ENTRIES]
        remaining = len(entries) - MAX_DIR_ENTRIES
        note = f"完整清单见 {dump_rel}" if dump_rel else "完整清单落盘失败"
        content = (
            f"目录: {dir_path}\n"
            + "\n".join(shown)
            + f"\n... 还有 {remaining} 项（已省略；{note}）"
        )
        return ToolResult(success=True, content=content)
    except PermissionError:
        return ToolResult(success=False, content=f"无权限访问目录: {path}")
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=f"列目录失败: {e}")


# get_directory_tree 默认不展开的目录名（大小写不敏感）。与"隐藏目录"规则叠加：
# skip_hidden_dirs=True 时，这些目录与 '.' 开头的目录都只列一行、不递归。
# 这是一份保守的"依赖 / VCS / IDE / 构建产物"清单；项目特殊目录按需增删。
IGNORED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git", ".hg", ".svn",
        ".venv", "venv",
        "node_modules",
        ".idea", ".vscode", ".vs",
        "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        ".tox", ".nox", ".cache",
        "dist", "build", "target", ".next", ".nuxt",
    }
)


@mcp.tool()
@guard
def get_directory_tree(
    path: str,
    max_depth: int = 3,
    skip_hidden_dirs: bool = True,
    include_ignored_dirs: bool = False,
) -> ToolResult:
    """
    递归获取目录结构树，可用于了解项目/文件夹的整体布局。

    默认跳过不展开的目录：以 '.' 开头的子目录（.git / .venv / .idea 等）以及
    `IGNORED_DIR_NAMES` 黑名单（node_modules / __pycache__ / venv / dist 等）。它们只在
    树里列出目录本身、不递归进去；隐藏**文件**（如 .env）仍会列出。根目录不受此选项
    影响——显式传入 '.git' 作为根时仍会展开。

    两个开关的关系：
    - `skip_hidden_dirs=False`：隐藏目录与黑名单目录**全部**照常递归；
    - `include_ignored_dirs=True`：在 `skip_hidden_dirs=True` 时额外允许展开**黑名单目录**
      （包括 .git / .venv 这类同时在黑名单里的隐藏目录）；其他未列入黑名单的隐藏目录
      仍会跳过。

    文件会附带大小（B/KB/MB），用于判断是否值得整文件读入；目录不带大小。

    **单个目录最多显示 MAX_DIR_ENTRIES（150）个条目**；超出时该目录显示前 150 项 + 省略
    计数，并把完整树写入工作区 `.theta/tmp/`，回执里给出可直接用 read_file 读取的相对路径。

    Args:
        path: 根目录路径
        max_depth: 最大递归深度(默认 3，最小 1)
        skip_hidden_dirs: 是否跳过隐藏目录/黑名单（默认 True；False 时全部展开）
        include_ignored_dirs: 是否允许展开黑名单目录（默认 False；True 时黑名单不再跳过）
    """
    dump_file = None
    dump_rel = None
    try:
        root = _resolve_path(path)
        _is_in_workspace(root)  # 目录结构读取同样受工作区沙箱约束；越界抛给 guard，不被下方 OSError 吞掉
        if not root.is_dir():
            return ToolResult(success=False, content=f"路径不是目录或不存在: {root}")
        max_depth = max(1, int(max_depth))
        lines: list[str] = []
        omitted_any = False

        dump_file, dump_rel = _create_dump_file("tree")
        if dump_file is not None:
            try:
                dump_file.write(
                    f"# get_directory_tree: {path} (max_depth={max_depth}, "
                    f"skip_hidden_dirs={skip_hidden_dirs}, "
                    f"include_ignored_dirs={include_ignored_dirs})\n"
                )
            except OSError:
                _discard_dump(dump_file, dump_rel)
                dump_file, dump_rel = None, None

        def dump_write(text: str) -> None:
            nonlocal dump_file, dump_rel
            if dump_file is None:
                return
            try:
                dump_file.write(text)
            except OSError:
                _discard_dump(dump_file, dump_rel)
                dump_file, dump_rel = None, None

        root_line = f"{root}/"
        lines.append(root_line)
        dump_write(root_line + "\n")

        def walk(current: Path, depth: int, prefix: str, seen: set, render: bool) -> None:
            nonlocal omitted_any
            if depth > max_depth:
                marker = f"{prefix}  ... (超出深度 {max_depth})"
                if render:
                    lines.append(marker)
                dump_write(marker + "\n")
                return
            try:
                entries = sorted(
                    current.iterdir(),
                    key=lambda p: (not p.is_dir(), p.name.lower()),
                )
            except PermissionError:
                marker = f"{prefix}  (无权限访问)"
                if render:
                    lines.append(marker)
                dump_write(marker + "\n")
                return

            shown_here = 0
            omitted_here = 0
            for i, entry in enumerate(entries):
                last = i == len(entries) - 1
                branch = "└── " if last else "├── "
                is_dir = entry.is_dir()
                hidden = entry.name.startswith(".")
                ignored = entry.name.lower() in IGNORED_DIR_NAMES
                skip_expand = (
                    is_dir
                    and skip_hidden_dirs
                    and (hidden or ignored)
                    and not (ignored and include_ignored_dirs)
                )
                if skip_expand:
                    reason = "隐藏目录" if hidden else "黑名单目录"
                    suffix = f"  ({reason}，已跳过递归)"
                elif is_dir:
                    suffix = ""
                else:
                    try:
                        suffix = f"  ({_format_size(entry.stat().st_size)})"
                    except OSError:
                        suffix = "  (?)"
                line = f"{prefix}{branch}{entry.name}{'/' if is_dir else ''}{suffix}"
                dump_write(line + "\n")

                if render and shown_here < MAX_DIR_ENTRIES:
                    lines.append(line)
                    shown_here += 1
                    child_render = True
                else:
                    if render:
                        omitted_here += 1
                    child_render = False

                if is_dir:
                    if skip_expand:
                        continue
                    try:
                        real = entry.resolve()
                    except OSError:
                        continue
                    # entry.is_dir() 跟随符号链接：工作区里一个指向外部的目录链接，若直接
                    # 递归进去就会把工作区外的目录/文件名列进树里（内容读不到，但不该列）。
                    # 与 search_content 对候选路径的口径一致：resolve 后校验，越界即跳过。
                    if not real.is_relative_to(WORKSPACE_PATH):
                        marker = f"{prefix}    └── (指向工作区外，已跳过)"
                        if child_render:
                            lines.append(marker)
                        dump_write(marker + "\n")
                        continue
                    if real in seen:
                        marker = f"{prefix}    └── (循环引用，已跳过)"
                        if child_render:
                            lines.append(marker)
                        dump_write(marker + "\n")
                        continue
                    seen = seen | {real}
                    child_prefix = prefix + ("    " if last else "│   ")
                    walk(entry, depth + 1, child_prefix, seen, child_render)

            if render and omitted_here:
                omitted_any = True
                note = f"完整清单见 {dump_rel}" if dump_rel else "完整清单落盘失败"
                lines.append(f"{prefix}  ... 还有 {omitted_here} 项（已省略；{note}）")

        walk(root, 1, "", set(), True)

        if dump_file is not None:
            try:
                dump_file.close()
            except OSError:
                _discard_dump(dump_file, dump_rel)
                dump_rel = None
            else:
                if not omitted_any:
                    _discard_dump(None, dump_rel)
                    dump_rel = None
        if omitted_any and dump_rel is None:
            lines.append("... [完整清单落盘失败]")
        return ToolResult(success=True, content="\n".join(lines))
    except OSError as e:
        _discard_dump(dump_file, dump_rel)
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


# glob / search_content 的输出上限：防止模型传超大参数把上下文打爆。
MAX_GLOB_RESULTS = 500
MAX_SEARCH_RESULTS = 200
MAX_SEARCH_CONTEXT_LINES = 10
MAX_SEARCH_LINE_CHARS = 500
MAX_SEARCH_OUTPUT_CHARS = 32 * 1024
MAX_SEARCH_FILE_BYTES = 4 * 1024 * 1024


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

    **基准**：pattern **按 path 解释**（"src/**/*.ts" 是相对 path 的子路径），但返回的
    清单**一律相对工作区根**——这样结果可以直接喂给 read_file/edit_file（它们把相对路径
    按工作区根解析）。两者在 path="."（默认）时重合，只有显式收窄 path 时才分得开。

    Args:
        pattern: shell 通配模式，如 "*.py"、"test_*.py"、"src/**/*.ts"。
        path: 起始目录，默认整个工作区；须在工作区内。
        include_dirs: 是否也列出目录（目录会带尾 '/'），默认 False（只列文件）。
        max_results: 最多返回多少条命中（默认 200；硬上限 500，超出会被夹住）。
    """
    if not pattern or not pattern.strip():
        raise InvalidArgumentError("pattern 不能为空，给出要找的文件名/通配模式")

    root = _resolve_path(path)
    _is_in_workspace(root)  # 起始目录越界抛给 guard
    if not root.is_dir():
        return ToolResult(success=False, content=f"路径不是目录或不存在: {root}")

    requested = max(1, int(max_results))
    max_results = min(requested, MAX_GLOB_RESULTS)
    clamped = requested > MAX_GLOB_RESULTS
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
                    hits.append(entry.relative_to(WORKSPACE_PATH).as_posix() + "/")
                    if len(hits) >= max_results:
                        capped = True
                        break
        if capped:
            break
        for name in sorted(filenames):
            entry = Path(dirpath) / name
            if _match(entry):
                hits.append(entry.relative_to(WORKSPACE_PATH).as_posix())
                if len(hits) >= max_results:
                    capped = True
                    break
        if capped:
            break

    if not hits:
        msg = f"未在 {root} 中找到匹配 {pattern!r} 的路径"
        if clamped:
            msg += f"（max_results 已限制为 {MAX_GLOB_RESULTS}）"
        return ToolResult(success=True, content=msg)

    body = (
        f"命中过多，仅显示前 {max_results} 条（可加长 pattern 或收窄 path）：\n"
        if capped
        else f"共命中 {len(hits)} 条：\n"
    )
    if clamped:
        body += f"（max_results={requested} 已限制为 {MAX_GLOB_RESULTS}）\n"
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
    单文件超过 4 MB 会跳过并计数；单行超过 500 字符会截断；总输出超过 32 KB 会截断。

    Args:
        query: 检索词：纯文本子串（默认）或正则（use_regex=True）。
        path: 检索的起始目录，默认整个工作区；相对/绝对路径均可，须在工作区内。
        max_results: 最多返回多少条命中（默认 50；硬上限 200，超出会被夹住）。
        extensions: 只在这些后缀里检索（如 [".py", ".md"]，不带点也接受）；默认不限定。
        use_regex: query 是否按正则匹配，默认 False（纯子串）。
        case_sensitive: 是否区分大小写，默认 False（忽略）。
        context_lines: 每个命中附带的前后行数（默认 0；硬上限 10）。
    """
    root = _resolve_path(path)
    _is_in_workspace(root)
    if not root.is_dir():
        return ToolResult(success=False, content=f"路径不是目录或不存在: {root}")

    if not query or not query.strip():
        return ToolResult(success=False, content="query 不能为空")

    requested_results = max(1, int(max_results))
    max_results = min(requested_results, MAX_SEARCH_RESULTS)
    clamped_results = requested_results > MAX_SEARCH_RESULTS

    requested_ctx = int(context_lines or 0)
    context_lines = min(max(0, requested_ctx), MAX_SEARCH_CONTEXT_LINES)
    clamped_context = requested_ctx > MAX_SEARCH_CONTEXT_LINES

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
    output_chars = 0
    output_capped = False
    skipped_large = 0

    def _clip_line(line: str) -> str:
        if len(line) <= MAX_SEARCH_LINE_CHARS:
            return line
        return line[:MAX_SEARCH_LINE_CHARS] + f"…[本行原长 {len(line)} 字符]…"

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
                if real.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    skipped_large += 1
                    continue
                data = real.read_bytes()
            except OSError:
                continue
            if _is_binary(data):  # 粗略二进制探测，跳过（口径与 read_file 共享）
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
                new_lines = [
                    f"{rel}:{no}: {_clip_line(text_lines[no - 1])}" for no in sorted(shown)
                ]
            else:
                new_lines = [
                    f"{rel}:{no}: {_clip_line(text_lines[no - 1])}" for no in matched
                ]

            for line in new_lines:
                if output_chars + len(line) + 1 > MAX_SEARCH_OUTPUT_CHARS:
                    output_capped = True
                    break
                lines_out.append(line)
                output_chars += len(line) + 1
            if output_capped:
                break

            if capped:
                break
        if capped or output_capped:
            break

    notes: list[str] = []
    if clamped_results:
        notes.append(f"max_results 已限制为 {MAX_SEARCH_RESULTS}")
    if clamped_context:
        notes.append(f"context_lines 已限制为 {MAX_SEARCH_CONTEXT_LINES}")
    if skipped_large:
        notes.append(
            f"已跳过 {skipped_large} 个大于 {_format_size(MAX_SEARCH_FILE_BYTES)} 的文件"
        )

    if total_hits == 0:
        msg = f"未在 {root} 中找到包含 {query!r} 的文本"
        if notes:
            msg += "（" + "；".join(notes) + "）"
        return ToolResult(success=True, content=msg)

    if output_capped:
        notes.append("输出已截断，后续命中未显示；请收窄 query/path/extensions")
    ctx = f"（含每处前后 {context_lines} 行）" if context_lines else ""
    if output_capped:
        head = f"输出已截断，以下为已显示的部分命中{ctx}："
    elif capped:
        head = f"命中过多，仅显示前 {max_results} 条命中{ctx}："
    else:
        head = f"共命中 {total_hits} 条{ctx}："
    body = head
    if notes:
        body += "\n" + "；".join(notes)
    body += "\n" + "\n".join(lines_out)
    return ToolResult(success=True, content=body)


if __name__ == "__main__":
    mcp.run(transport="stdio")