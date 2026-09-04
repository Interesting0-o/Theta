from mcp_service.file_io import (
    copy_path,
    create_dir,
    create_file,
    delete_dir,
    delete_file,
    edit_file,
    get_directory_tree,
    list_dir,
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
