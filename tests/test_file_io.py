from mcp_service.file_io import (
    copy_path,
    create_dir,
    create_file,
    delete_dir,
    delete_file,
    get_directory_tree,
    list_dir,
    write_file,
)


def test_create_file_and_write_incremental_lines(tmp_path):
    file_path = tmp_path / "demo.txt"

    result = create_file(str(file_path), "alpha\nbeta\ngamma\n")
    assert result.success is True

    result = write_file(str(file_path), "delta\n", start_line=2, end_line=2)
    assert result.success is True
    assert file_path.read_text(encoding="utf-8") == "alpha\ndelta\ngamma\n"


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
