"""`skills/github/server.py` 的收口行为：上游失败与渲染失败都必须翻成 `upstream_error`。

不联网、不起子进程：`_request` 被换成假响应（技能 server 在 **import 期**就要求 GITHUB_TOKEN，
故夹具先把值塞进 os.environ 再导入）。本文件与 tests/test_guard.py 同族：守"异常分类"这条机制。
"""
import email.message
import importlib
import io
import sys
import urllib.error

import pytest


def _http_error(code: int, body: str, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    """造一个带响应头的 HTTPError（GitHub 的限流判据就在头里）。"""
    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "err", message, io.BytesIO(body.encode("utf-8"))
    )


@pytest.fixture
def gh(monkeypatch):
    """导入 github 技能的 server 模块（每个用例一份干净的 import）。"""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    sys.modules.pop("skills.github.server", None)
    return importlib.import_module("skills.github.server")


def test_render_failure_is_upstream_error(gh, monkeypatch):
    """render 抛 `_UpstreamError` 时必须当场翻成 ToolResult。

    回归防线：`_fetch` 曾经只把 `_request` 包在 try 里，render 在 try 外调用——异常漏出去后
    `guard` 认不出它是上游问题，按"内部 bug"处理：模型收到 `internal_error` +
    "工具内部错误（非输入问题），无需重试"，而正确动作恰恰是换个参数/路径再取。
    """
    monkeypatch.setattr(gh, "_request", lambda path, params=None: ({}, {}))

    def boom(_data):
        raise gh._UpstreamError("解码失败")

    out = gh._fetch("/x", boom)

    assert out.success is False
    assert out.error_type == "upstream_error"
    assert "解码失败" in out.content


def test_file_read_with_broken_base64_reports_upstream_error(gh, monkeypatch):
    """真走一遍工具：坏 base64 不能变成"工具内部错误（非输入问题），无需重试"。"""
    monkeypatch.setattr(
        gh,
        "_request",
        lambda path, params=None: (
            {
                "type": "file",
                "encoding": "base64",
                "content": "abcde",  # 长度不合法 → binascii.Error（ValueError 子类）
                "path": "a.txt",
                "size": 3,
            },
            {},
        ),
    )

    out = gh.github_file_read("owner", "repo", "a.txt")

    assert out.success is False
    assert out.error_type == "upstream_error"


def test_upstream_http_error_keeps_error_type(gh, monkeypatch):
    """上游 HTTP 失败（404 等）依旧是 upstream_error——`_translate` 那套分类没被上面的改动碰坏。"""
    def not_found(path, params=None):
        raise gh._UpstreamError("GitHub 说没有这个东西（404）")

    monkeypatch.setattr(gh, "_request", not_found)

    out = gh._fetch("/x", lambda _d: "不该走到这里")

    assert out.success is False
    assert out.error_type == "upstream_error"
    assert "404" in out.content


def test_successful_fetch_still_clips(gh, monkeypatch):
    """成功路径不受影响：正文照旧按 _MAX_CHARS 截断并注明。"""
    monkeypatch.setattr(gh, "_request", lambda path, params=None: ({}, {}))

    out = gh._fetch("/x", lambda _d: "x" * (gh._MAX_CHARS + 10))

    assert out.success is True
    assert "已截断" in out.content


# ---------------------- 限流 vs 权限：403 的两种含义不许混 ----------------------


def _urlopen_raising(error: Exception):
    """把 `urllib.request.urlopen` 换成"必抛"的假实现。

    ⚠️ 这里**不能**去 patch `gh._request`：要测的正是真 `_request` 里那段 HTTPError → 回执的
    翻译逻辑，patch 掉它就等于把被测代码绕过去了（HTTPError 会直接漏给 guard，变成 internal_error）。
    """

    def fake(*args, **kwargs):
        raise error

    return fake


def test_rate_limit_is_read_from_headers(gh, monkeypatch):
    """403 + `X-RateLimit-Remaining: 0` → 必须说成"限流"并给恢复时刻与"别重试"。

    回归防线：`_request` 曾经把 403 笼统说成"令牌无效 / 权限不足 / 也可能限流"——而限流是这个
    只读技能最常见的失败，且它有**确定答案**（就在响应头里）。让模型去猜三种可能，等于三种动作
    都做不对（换 token 没用、重试更没用）。
    """
    monkeypatch.setattr(
        gh.urllib.request,
        "urlopen",
        _urlopen_raising(
            _http_error(
                403,
                '{"message":"API rate limit exceeded"}',
                {
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Limit": "5000",
                    "X-RateLimit-Reset": "1893456000",
                },
            )
        ),
    )

    out = gh.github_repo_view("octo", "demo")

    assert out.success is False
    assert out.error_type == "upstream_error"
    assert "限流" in out.content
    assert "5000" in out.content  # 带上配额上限
    assert "恢复" in out.content and "不要原样重试" in out.content


def test_secondary_rate_limit_429_is_named(gh, monkeypatch):
    """429 / 正文提到 secondary rate limit → 说"二级限流"（短时过密，与小时配额无关）。"""
    monkeypatch.setattr(
        gh.urllib.request,
        "urlopen",
        _urlopen_raising(
            _http_error(
                429,
                '{"message":"You have exceeded a secondary rate limit"}',
                {"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
            )
        ),
    )

    out = gh.github_repo_view("octo", "demo")

    assert out.success is False
    assert "二级限流" in out.content
    assert "不要原样重试" in out.content


def test_forbidden_with_quota_left_is_permission_not_rate_limit(gh, monkeypatch):
    """配额还有剩的 403 → 是权限问题，**不许**被误报成限流（否则模型会白等）。"""
    monkeypatch.setattr(
        gh.urllib.request,
        "urlopen",
        _urlopen_raising(
            _http_error(
                403,
                '{"message":"Resource not accessible by personal access token"}',
                {"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
            )
        ),
    )

    out = gh.github_repo_view("octo", "demo")

    assert out.success is False
    assert "权限" in out.content
    assert "限流" not in out.content


# ---------------------- contents 接口的几种"不是文件" ----------------------


def test_submodule_is_not_reported_as_too_large(gh, monkeypatch):
    """子模块曾一律被说成"太大，可用下面这个地址取：None"——类型不对，话就不对。"""
    monkeypatch.setattr(
        gh,
        "_request",
        lambda path, params=None: (
            {
                "type": "submodule",
                "path": "vendor/lib",
                "sha": "abc1234567890",
                "submodule_git_url": "https://github.com/octo/lib.git",
                "size": 0,
            },
            {},
        ),
    )

    out = gh.github_file_read("octo", "demo", "vendor/lib")

    assert out.success is True  # 如实说明同样是一次成功的回答
    assert "子模块" in out.content
    assert "octo/lib.git" in out.content
    assert "太大" not in out.content


def test_symlink_is_reported_as_symlink(gh, monkeypatch):
    """符号链接要指到目标路径去读，而不是当文件解 base64。"""
    monkeypatch.setattr(
        gh,
        "_request",
        lambda path, params=None: (
            {"type": "symlink", "path": "link.py", "target": "real/file.py", "size": 12},
            {},
        ),
    )

    out = gh.github_file_read("octo", "demo", "link.py")

    assert out.success is True
    assert "符号链接" in out.content
    assert "real/file.py" in out.content


def test_large_file_still_points_at_download_url(gh, monkeypatch):
    """大文件（encoding=none）那条既有路径没被上面的改动碰坏。"""
    monkeypatch.setattr(
        gh,
        "_request",
        lambda path, params=None: (
            {
                "type": "file",
                "path": "big.bin",
                "encoding": "none",
                "size": 5_000_000,
                "download_url": "https://raw.githubusercontent.com/octo/demo/main/big.bin",
            },
            {},
        ),
    )

    out = gh.github_file_read("octo", "demo", "big.bin")

    assert out.success is True
    assert "太大" in out.content
    assert "raw.githubusercontent.com" in out.content


# ---------------------- tree 的截断提示与脏数据 ----------------------


def test_tree_truncation_hint_and_dirty_entries(gh, monkeypatch):
    """超过 300 个文件要给可行动出路；缺 path 的条目与目录条目都不该把整次调用搞崩。"""
    tree = [{"type": "blob", "path": f"f{i}.py"} for i in range(305)]
    tree.append({"type": "blob"})  # 缺 path：曾经 item["path"] 会 KeyError → internal_error
    tree.append({"type": "tree", "path": "src"})  # 目录不该出现在文件列表里
    monkeypatch.setattr(
        gh, "_request", lambda path, params=None: ({"tree": tree, "truncated": False}, {})
    )

    out = gh.github_tree("octo", "demo")

    assert out.success is True
    assert "共 305 个文件" in out.content
    assert "用 path 缩小" in out.content
    assert "- src" not in out.content
