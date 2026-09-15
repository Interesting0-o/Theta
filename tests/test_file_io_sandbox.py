"""文件工作区沙箱的逃逸测试（越界/遍历/符号链接）。

直接调用 file_io 原始函数（不经过 FastMCP），并在测试里把模块全局
`WORKSPACE_PATH` 替换为 pytest 的 tmp_path，从而与 WORKSPACE_PATH 的真实值
无关、跨平台确定：
- "工作区内" = tmp_path
- "工作区外" = tmp_path 的兄弟目录 / 同级文件

运行（import mcp_service.file_io 需要 WORKSPACE_PATH 存在，见 CLAUDE.md）：

    .venv/Scripts/python.exe -m pytest tests/test_file_io_sandbox.py

符号链接用例在无权限平台（如未开开发者模式的 Windows）自动 skip。
"""
from pathlib import Path

import pytest

import mcp_service.file_io as file_io
from mcp_service.file_io import (
    copy_path,
    create_dir,
    create_file,
    delete_dir,
    delete_file,
    get_directory_tree,
    glob,
    list_dir,
    read_file,
    search_content,
    write_file,
)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """把模块里的工作区根换成 tmp_path：tmp_path 内 = 工作区内。"""
    monkeypatch.setattr(file_io, "WORKSPACE_PATH", tmp_path)
    return tmp_path


def _make_symlink(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError:
        pytest.skip("当前环境无创建符号链接权限")


# ---------------- 越界：绝对路径 / ../ 遍历 ----------------

def test_all_tools_reject_outside_absolute_path(ws):
    """每个工具（含读操作）拿到工作区外绝对路径都必须报 workspace_violation。"""
    outside = ws.parent / "outside_target.txt"
    cases = [
        ("create_file", lambda: create_file(str(outside), "x")),
        ("create_dir", lambda: create_dir(str(outside))),
        ("delete_file", lambda: delete_file(str(outside))),
        ("delete_dir", lambda: delete_dir(str(outside))),
        ("write_file", lambda: write_file(str(outside), "x")),
        ("read_file", lambda: read_file(str(outside))),
        ("list_dir", lambda: list_dir(str(outside))),
        ("get_directory_tree", lambda: get_directory_tree(str(outside))),
        ("copy_path 源越界", lambda: copy_path(str(outside), str(ws / "dst"))),
        ("copy_path 目标越界", lambda: copy_path(str(ws / "src"), str(outside))),
        ("glob 起始目录越界", lambda: glob("*.py", path=str(outside))),
    ]
    for name, fn in cases:
        result = fn()
        assert result.success is False, name
        assert result.error_type == "workspace_violation", name


def test_relative_dotdot_escape_rejected(ws):
    result = create_file("../evil.txt", "x")
    assert result.success is False
    assert result.error_type == "workspace_violation"
    assert not (ws.parent / "evil.txt").exists()


# ---------------- 相对路径以工作区为基准解析 ----------------

def test_relative_path_resolves_against_workspace(ws):
    result = create_file("rel/demo.txt", "hi")
    assert result.success is True
    assert result.error_type is None
    assert (ws / "rel" / "demo.txt").exists()

    result = read_file("rel/demo.txt")
    assert result.success is True
    assert result.content == "hi"


# ---------------- 符号链接逃逸 ----------------

def test_symlink_to_outside_file_rejected(ws):
    secret = ws.parent / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = ws / "leak.txt"
    _make_symlink(link, secret)

    assert read_file(str(link)).error_type == "workspace_violation"
    assert write_file(str(link), "x").error_type == "workspace_violation"


def test_symlink_to_outside_dir_rejected(ws):
    outside_dir = ws.parent / "secret_dir"
    outside_dir.mkdir(exist_ok=True)
    (outside_dir / "data.txt").write_text("secret", encoding="utf-8")
    link = ws / "leakdir"
    _make_symlink(link, outside_dir, target_is_directory=True)

    # 越界路径校验在 IO 前触发，不会真去列/写外部目录
    assert list_dir(str(link)).error_type == "workspace_violation"
    assert write_file(str(link / "data.txt"), "x").error_type == "workspace_violation"


def test_symlink_to_inside_target_allowed(ws):
    """指向工作区内部的符号链接不被误伤，仍可正常读写。"""
    real = ws / "real.txt"
    real.write_text("inside", encoding="utf-8")
    link = ws / "inlink.txt"
    _make_symlink(link, real)

    result = read_file(str(link))
    assert result.success is True
    assert result.content == "inside"


# ---------------- 检索工具 search_content 的沙箱行为 ----------------

def test_search_content_default_root_is_workspace(ws):
    (ws / "app.py").write_text("def handle_query():\n    pass\n", encoding="utf-8")
    # 不传 path → 默认 "." 应解析到工作区根，而不是 cwd
    result = search_content("handle_query")
    assert result.success is True
    assert "app.py:1:" in result.content


def test_search_content_rejects_outside_start_path(ws):
    result = search_content("x", path=str(ws.parent))
    assert result.success is False
    assert result.error_type == "workspace_violation"


def test_search_content_does_not_leak_symlinked_outside_file(ws):
    secret = ws.parent / "secret_out.txt"
    secret.write_text("topsecret needle line", encoding="utf-8")
    link = ws / "leak.py"
    _make_symlink(link, secret)

    # 工作区内有一个指向外部的符号链接：检索不应把外部内容搜进来
    result = search_content("topsecret")
    assert result.success is True
    assert "未" in result.content
