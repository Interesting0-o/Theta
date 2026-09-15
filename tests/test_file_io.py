from mcp_service.file_io import (
    copy_path,
    create_file,
    delete_dir,
    delete_file,
    edit_file,
    get_directory_tree,
    glob,
    list_dir,
    read_file,
    search_content,
    write_file,
)


def test_create_file_then_edit_file_string_anchor(tmp_path):
    file_path = tmp_path / "demo.txt"

    result = create_file(str(file_path), "alpha\nbeta\ngamma\n")
    assert result.success is True

    result = edit_file(str(file_path), "beta", "delta")
    assert result.success is True
    assert file_path.read_text(encoding="utf-8") == "alpha\ndelta\ngamma\n"


def test_create_file_rejects_existing_file(tmp_path):
    file_path = tmp_path / "demo.txt"
    file_path.write_text("old", encoding="utf-8")

    result = create_file(str(file_path), "new")

    assert result.success is False
    assert "已存在" in result.content
    assert file_path.read_text(encoding="utf-8") == "old"  # 既不清空也不追加


def test_write_file_overwrites_whole_file(tmp_path):
    file_path = tmp_path / "demo.txt"
    file_path.write_text("old content", encoding="utf-8")

    result = write_file(str(file_path), "brand new")

    assert result.success is True
    assert file_path.read_text(encoding="utf-8") == "brand new"


def test_edit_file_replace_all(tmp_path):
    file_path = tmp_path / "demo.txt"
    file_path.write_text("x=1\nx=2\n", encoding="utf-8")

    result = edit_file(str(file_path), "x=", "y=", replace_all=True)

    assert result.success is True
    assert file_path.read_text(encoding="utf-8") == "y=1\ny=2\n"


def test_edit_file_multiple_matches_require_replace_all(tmp_path):
    file_path = tmp_path / "demo.txt"
    file_path.write_text("x=1\nx=2\n", encoding="utf-8")

    result = edit_file(str(file_path), "x=", "y=")

    assert result.success is False
    assert result.error_type == "invalid_argument"
    assert "2 处" in result.content
    assert file_path.read_text(encoding="utf-8") == "x=1\nx=2\n"  # 未改动


def test_edit_file_not_found_reports_invalid_argument(tmp_path):
    file_path = tmp_path / "demo.txt"
    file_path.write_text("hello", encoding="utf-8")

    result = edit_file(str(file_path), "不存在", "x")

    assert result.success is False
    assert result.error_type == "invalid_argument"
    assert file_path.read_text(encoding="utf-8") == "hello"  # 未改动


def test_edit_file_missing_file(tmp_path):
    result = edit_file(str(tmp_path / "nope.txt"), "a", "b")
    assert result.success is False


def test_edit_file_empty_old_string(tmp_path):
    file_path = tmp_path / "demo.txt"
    file_path.write_text("hello", encoding="utf-8")

    result = edit_file(str(file_path), "", "x")

    assert result.success is False
    assert result.error_type == "invalid_argument"


def test_edit_file_violation_carries_error_type():
    result = edit_file("/etc/coding_agent_edit_outside.txt", "a", "b")
    assert result.success is False
    assert result.error_type == "workspace_violation"


def test_copy_and_delete_dir(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "child").mkdir()
    (src_dir / "child" / "data.txt").write_text("hello", encoding="utf-8")

    dst_dir = tmp_path / "dst"
    result = copy_path(str(src_dir), str(dst_dir), recursive=True)
    assert result.success is True
    assert (dst_dir / "child" / "data.txt").exists()

    result = delete_dir(str(src_dir), recursive=True)
    assert result.success is True
    assert not src_dir.exists()

    result = delete_dir(str(dst_dir), recursive=True)
    assert result.success is True
    assert not dst_dir.exists()


def test_copy_path_directory_without_recursive_fails(tmp_path):
    """目录 + recursive=False：不许回"目录已复制"却一个文件都没复制。

    回归：旧实现在这条路上只 dst.mkdir() 就回 success=True，等于给模型一个假事实。
    """
    src_dir = tmp_path / "cp_src"
    src_dir.mkdir()
    (src_dir / "data.txt").write_text("hello", encoding="utf-8")
    dst_dir = tmp_path / "cp_dst"

    result = copy_path(str(src_dir), str(dst_dir), recursive=False)

    assert result.success is False
    assert result.error_type == "invalid_argument"
    assert not dst_dir.exists()  # 什么都别建
    assert "recursive" in result.content
    assert src_dir.exists()  # 源也没被动过


def test_delete_file(tmp_path):
    file_path = tmp_path / "remove_me.txt"
    file_path.write_text("remove me", encoding="utf-8")

    result = delete_file(str(file_path))
    assert result.success is True
    assert not file_path.exists()


def test_list_dir(tmp_path):
    (tmp_path / "alpha.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "beta.txt").write_text("b", encoding="utf-8")

    result = list_dir(str(tmp_path))
    assert result.success is True
    assert "[dir] sub" in result.content
    assert "[file] alpha.txt" in result.content

    # 目录在前
    assert result.content.index("[dir] sub") < result.content.index("[file] alpha.txt")


def test_get_directory_tree(tmp_path):
    (tmp_path / "alpha.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "beta.txt").write_text("b", encoding="utf-8")

    result = get_directory_tree(str(tmp_path), max_depth=3)
    assert result.success is True
    assert "alpha.txt" in result.content
    assert "sub/" in result.content
    assert "beta.txt" in result.content


def test_get_directory_tree_invalid_path(tmp_path):
    result = get_directory_tree(str(tmp_path / "not_exists"), max_depth=3)
    assert result.success is False


def test_create_file_success_has_no_error_type(tmp_path):
    result = create_file(str(tmp_path / "guarded.txt"), "hi")
    assert result.success is True
    assert result.error_type is None


def test_create_file_violation_carries_error_type():
    # 绝对路径不在 WORKSPACE_PATH(=/tmp) 内，越界校验应在任何 IO 之前触发
    result = create_file("/etc/coding_agent_outside_test.txt", "x")
    assert result.success is False
    assert result.error_type == "workspace_violation"
    assert "coding_agent_outside_test.txt" in result.content


def _populate_search_fixture(tmp_path):
    (tmp_path / "mod.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (tmp_path / "doc.md").write_text("# ALPHA doc\n", encoding="utf-8")
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01hello\n")


def test_search_content_case_insensitive_substring(tmp_path):
    _populate_search_fixture(tmp_path)
    result = search_content("ALPHA", path=str(tmp_path))
    assert result.success is True
    assert "mod.py:1:" in result.content
    assert "doc.md:1:" in result.content


def test_search_content_extension_filter(tmp_path):
    _populate_search_fixture(tmp_path)
    result = search_content("alpha", path=str(tmp_path), extensions=[".py"])
    assert result.success is True
    assert "mod.py" in result.content
    assert "doc.md" not in result.content


def test_search_content_no_match(tmp_path):
    _populate_search_fixture(tmp_path)
    result = search_content("根本不存在的内容", path=str(tmp_path))
    assert result.success is True
    assert "未" in result.content


def test_search_content_truncates_and_marks(tmp_path):
    lines = "\n".join(f"row {i} needle" for i in range(5))
    (tmp_path / "a.txt").write_text(lines, encoding="utf-8")
    result = search_content("needle", path=str(tmp_path), max_results=2)
    assert result.success is True
    assert "仅显示前 2 条" in result.content


def test_search_content_skips_binary(tmp_path):
    _populate_search_fixture(tmp_path)
    # "hello" 只出现在二进制 bin.dat 里，应被跳过而搜不到
    result = search_content("hello", path=str(tmp_path))
    assert result.success is True
    assert "未" in result.content


# ---------------- read_file 行号分段读取 ----------------

def _write_5_lines(path):
    path.write_text("\n".join(f"line{i}" for i in range(1, 6)) + "\n", encoding="utf-8")


def test_read_file_whole_file_unchanged(tmp_path):
    target = tmp_path / "whole.txt"
    target.write_text("a\nb\n", encoding="utf-8")
    result = read_file(str(target))
    assert result.success is True
    assert result.content == "a\nb\n"  # 整文件原样返回，不加行号标题


def test_read_file_range(tmp_path):
    target = tmp_path / "range.txt"
    _write_5_lines(target)
    result = read_file(str(target), start_line=2, end_line=4)
    assert result.success is True
    assert "第 2-4 行 / 共 5 行" in result.content
    assert "line2" in result.content and "line4" in result.content
    assert "line1" not in result.content
    assert "line5" not in result.content


def test_read_file_start_only_reads_to_end(tmp_path):
    target = tmp_path / "tail.txt"
    _write_5_lines(target)
    result = read_file(str(target), start_line=4)
    assert result.success is True
    assert "line1" not in result.content
    assert "line4" in result.content and "line5" in result.content


def test_read_file_range_out_of_bounds_reports_empty(tmp_path):
    target = tmp_path / "short.txt"
    _write_5_lines(target)
    result = read_file(str(target), start_line=10)
    assert result.success is True
    assert "共 5 行" in result.content


# ---------------- read_file 二进制 / 非 UTF-8 的可预期回执 ----------------


def test_read_file_binary_png_returns_receipt(tmp_path):
    """读 PNG 给可预期回执（判定为二进制 + 大小），不再让 UnicodeDecodeError 冒成 internal_error。

    回执只说"是二进制、读不了"，不再猜"是什么格式"——路径里的 .png 已随回执回显。
    """
    target = tmp_path / "pic.png"
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    target.write_bytes(payload)

    result = read_file(str(target))

    assert result.success is True
    assert "二进制文件" in result.content
    assert str(len(payload)) in result.content


def test_read_file_binary_receipt_carries_the_path(tmp_path):
    """回执带 path：后缀在里面，模型据此自己判断格式（这是删掉魔数表的依据）。"""
    target = tmp_path / "archive.tar.gz"
    target.write_bytes(b"\x1f\x8b" + b"\x00" * 8)

    result = read_file(str(target))

    assert result.success is True
    assert "二进制文件" in result.content
    assert "archive.tar.gz" in result.content


def test_read_file_non_utf8_text_receipt(tmp_path):
    """GBK 文本（无 NUL、NUL 探测放行）：给"不是 UTF-8"的回执而非 internal_error。"""
    target = tmp_path / "gbk.txt"
    target.write_bytes("你好，世界".encode("gbk"))

    result = read_file(str(target))

    assert result.success is True
    assert "不是 UTF-8" in result.content


def test_read_file_utf8_text_unaffected(tmp_path):
    """修复不得波及正常路径：UTF-8 文本原样返回（编辑锚定依赖原文）。"""
    target = tmp_path / "a.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")

    result = read_file(str(target))

    assert result.success is True
    assert result.content == "hello\nworld\n"


def test_read_file_binary_with_line_range_still_receipt(tmp_path):
    """对二进制传行区间同样拿到回执（行区间对二进制无意义，不该走行逻辑）。"""
    target = tmp_path / "pic.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    result = read_file(str(target), start_line=1, end_line=2)

    assert result.success is True
    assert "二进制文件" in result.content


def test_read_file_invalid_bounds_are_invalid_argument(tmp_path):
    target = tmp_path / "x.txt"
    _write_5_lines(target)
    for kwargs in ({"start_line": 0}, {"end_line": -1}, {"start_line": 3, "end_line": 2}):
        result = read_file(str(target), **kwargs)
        assert result.success is False
        assert result.error_type == "invalid_argument", kwargs


# ---------------- edit_file 二进制 / 非 UTF-8 的可预期回执 ----------------
# 与 read_file 同一套口径：UnicodeDecodeError 不是 OSError 子类，漏了它就会冒到 guard
# 被误判成 internal_error（回执还写"无需重试"），模型会因此得出误导结论。


def test_edit_file_binary_returns_receipt(tmp_path):
    target = tmp_path / "pic.png"
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    target.write_bytes(payload)

    result = edit_file(str(target), "x", "y")

    assert result.success is True
    assert result.error_type is None
    assert "二进制文件" in result.content
    assert target.read_bytes() == payload  # 原文件没被动过


def test_edit_file_non_utf8_text_receipt(tmp_path):
    """GBK 文本（无 NUL、NUL 探测放行）：给"不是 UTF-8"的回执而非 internal_error。"""
    target = tmp_path / "gbk.txt"
    target.write_bytes("你好，世界".encode("gbk"))

    result = edit_file(str(target), "你好", "再见")

    assert result.success is True
    assert result.error_type is None
    assert "不是 UTF-8" in result.content


def test_edit_file_utf8_text_unaffected(tmp_path):
    """修复不得波及正常路径：UTF-8 文本照常编辑。"""
    target = tmp_path / "a.txt"
    target.write_text("hello world\n", encoding="utf-8")

    result = edit_file(str(target), "world", "theta")

    assert result.success is True
    assert "已编辑" in result.content
    assert target.read_text(encoding="utf-8") == "hello theta\n"


# ---------------- glob 按文件名/通配定位 ----------------

def _populate_glob_tree(tmp_path):
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "b.txt").write_text("", encoding="utf-8")
    (tmp_path / ".env.example").write_text("", encoding="utf-8")
    (tmp_path / ".hidden_dir").mkdir()
    (tmp_path / ".hidden_dir" / "e.py").write_text("", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.py").write_text("", encoding="utf-8")
    (tmp_path / "sub" / "d.txt").write_text("", encoding="utf-8")


def test_glob_basename_pattern_matches_any_depth(tmp_path):
    _populate_glob_tree(tmp_path)
    result = glob("*.py", path=str(tmp_path))
    assert result.success is True
    assert "a.py" in result.content
    assert "sub/c.py" in result.content
    assert "b.txt" not in result.content
    assert ".hidden_dir/e.py" not in result.content  # 隐藏目录不钻入


def test_glob_dotfile_matches_when_named(tmp_path):
    _populate_glob_tree(tmp_path)
    result = glob(".env.example", path=str(tmp_path))
    assert result.success is True
    assert ".env.example" in result.content


def test_glob_slash_pattern_and_double_star(tmp_path):
    _populate_glob_tree(tmp_path)
    result = glob("sub/*.py", path=str(tmp_path))
    assert result.success is True
    assert "sub/c.py" in result.content
    assert "sub/d.txt" not in result.content

    result = glob("**/*.txt", path=str(tmp_path))
    assert result.success is True
    assert "b.txt" in result.content and "sub/d.txt" in result.content

    # 中段 **（零或多级目录）与结尾 **（该前缀下任意深度）
    result = glob("sub/**/c.py", path=str(tmp_path))
    assert result.success is True
    assert "sub/c.py" in result.content

    result = glob("sub/**", path=str(tmp_path))
    assert result.success is True
    assert "sub/c.py" in result.content and "sub/d.txt" in result.content


def test_glob_include_dirs_suffix(tmp_path):
    _populate_glob_tree(tmp_path)
    result = glob("sub", path=str(tmp_path), include_dirs=True)
    assert result.success is True
    assert "sub/" in result.content


def test_glob_paths_are_relative_to_workspace_root(tmp_path):
    """收窄 path 时，清单仍以**工作区根**为基准——否则结果喂给 read_file 会读错文件。

    回归：旧实现用 entry.relative_to(root)（root = path 参数），传 path="src" 时回 "a.py"，
    而 read_file/edit_file 把相对路径按**工作区根**解析 → 读到另一个同名文件或报不存在。
    pattern 仍按 path 解释（"src/**/*.ts" 那种写法），只有**输出**的基准是工作区根。
    """
    from mcp_service import file_io as file_io_module

    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "a.py").write_text("x", encoding="utf-8")

    result = glob("*.py", path=str(sub))

    assert result.success is True
    expected = (sub / "a.py").relative_to(file_io_module.WORKSPACE_PATH).as_posix()
    assert expected in result.content
    assert "\na.py" not in result.content  # 不能再是相对 path 的裸名


def test_glob_no_match_and_cap(tmp_path):
    _populate_glob_tree(tmp_path)
    result = glob("*.rs", path=str(tmp_path))
    assert result.success is True
    assert "未" in result.content

    result = glob("*.py", path=str(tmp_path), max_results=1)
    assert result.success is True
    assert "仅显示前 1 条" in result.content


def test_glob_empty_pattern_is_invalid_argument(tmp_path):
    result = glob("", path=str(tmp_path))
    assert result.success is False
    assert result.error_type == "invalid_argument"


# ---------------- search_content 正则 / 大小写 / 上下文 ----------------

def test_search_content_regex_and_invalid(tmp_path):
    (tmp_path / "log.txt").write_text("error 404\nok\nerror 500\n", encoding="utf-8")
    result = search_content(r"error \d+", path=str(tmp_path), use_regex=True)
    assert result.success is True
    assert "log.txt:1:" in result.content
    assert "log.txt:3:" in result.content

    # 不带 use_regex：按子串匹配字面 "error \d+"
    result = search_content(r"error \d+", path=str(tmp_path))
    assert result.success is True
    assert "未" in result.content

    result = search_content("(unclosed", path=str(tmp_path), use_regex=True)
    assert result.success is False
    assert result.error_type == "invalid_argument"


def test_search_content_case_sensitive(tmp_path):
    (tmp_path / "words.txt").write_text("alpha said hi\nALPHA said hey\n", encoding="utf-8")
    # 默认忽略大小写：两行都命中
    result = search_content("alpha", path=str(tmp_path))
    assert result.success is True
    assert "words.txt:1:" in result.content
    assert "words.txt:2:" in result.content
    # 区分大小写：只有小写那行命中
    result = search_content("alpha", path=str(tmp_path), case_sensitive=True)
    assert result.success is True
    assert "words.txt:1:" in result.content
    assert "words.txt:2:" not in result.content


def test_search_content_context_lines(tmp_path):
    lines = ["head", "needle", "tail1", "needle2", "tail2"]
    (tmp_path / "ctx.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = search_content("needle", path=str(tmp_path), context_lines=1)
    assert result.success is True
    content = result.content
    # 上下文行与命中行都带前缀出现
    assert "head" in content
    assert "tail1" in content
    assert "needle2" in content
    assert "tail2" in content
    assert "ctx.txt:2:" in content
