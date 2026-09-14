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
    monkeypatch.setattr(gh, "_request", lambda path, params=None, **_kw: ({}, {}))

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
        lambda path, params=None, **_kw: (
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
    def not_found(path, params=None, **_kw):
        raise gh._UpstreamError("GitHub 说没有这个东西（404）")

    monkeypatch.setattr(gh, "_request", not_found)

    out = gh._fetch("/x", lambda _d: "不该走到这里")

    assert out.success is False
    assert out.error_type == "upstream_error"
    assert "404" in out.content


def test_successful_fetch_still_clips(gh, monkeypatch):
    """成功路径不受影响：正文照旧按 _MAX_CHARS 截断并注明。"""
    monkeypatch.setattr(gh, "_request", lambda path, params=None, **_kw: ({}, {}))

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
        lambda path, params=None, **_kw: (
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
        lambda path, params=None, **_kw: (
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
        lambda path, params=None, **_kw: (
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
        gh, "_request", lambda path, params=None, **_kw: ({"tree": tree, "truncated": False}, {})
    )

    out = gh.github_tree("octo", "demo")

    assert out.success is True
    assert "共 305 个文件" in out.content
    assert "用 path 缩小" in out.content
    assert "- src" not in out.content


def test_tree_default_branch_lookup_failure_is_upstream_error(gh, monkeypatch):
    """`ref` 留空时要先问一次默认分支——那一步失败同样得是 `upstream_error`。

    回归防线：`_UpstreamError.error_type` 那个死参删掉后，这一处残留的 `exc.error_type` 会抛
    AttributeError，被 guard 认成"内部 bug（无需重试）"——而事实是上游读不到，换个 ref 或稍后
    重试才有意义。
    """

    def boom(path, params=None, **_kw):
        raise gh._UpstreamError("GitHub 说没有这个东西（404）")

    monkeypatch.setattr(gh, "_request", boom)

    out = gh.github_tree("octo", "demo")

    assert out.success is False
    assert out.error_type == "upstream_error"
    assert "404" in out.content


# ---------------------- 列表族：PR 过滤、翻页提示、脏数据 ----------------------


def test_issue_list_filters_out_pull_requests(gh, monkeypatch):
    """GitHub 的 issues 接口会把 **PR 一起返回**——不滤掉的话模型会把 PR 当 issue 数错。"""
    monkeypatch.setattr(
        gh,
        "_request",
        lambda path, params=None, **_kw: (
            [
                {
                    "number": 1,
                    "title": "真 issue",
                    "state": "open",
                    "user": {"login": "octo"},
                    "comments": 0,
                },
                {"number": 2, "title": "其实是 PR", "pull_request": {"url": "x"}},
            ],
            {},
        ),
    )

    out = gh.github_issue_list("octo", "demo")

    assert out.success is True
    assert "真 issue" in out.content
    assert "其实是 PR" not in out.content
    assert "1 条 PR 已略去" in out.content


def test_list_tool_hints_at_the_next_page(gh, monkeypatch):
    """本页条数正好装满 limit → 必须提示还有下一页，否则等于悄悄截断。"""
    items = [
        {"number": i, "title": f"t{i}", "state": "open", "user": {"login": "a"}, "comments": 0}
        for i in range(2)
    ]
    monkeypatch.setattr(gh, "_request", lambda path, params=None, **_kw: (items, {}))

    out = gh.github_pr_list("octo", "demo", limit=2)

    assert out.success is True
    assert "page=2" in out.content


def test_issue_list_survives_missing_upstream_fields(gh, monkeypatch):
    """上游缺字段（没有 labels / 没有 user）不许变成 KeyError → internal_error。"""
    monkeypatch.setattr(
        gh, "_request", lambda path, params=None, **_kw: ([{"number": 3, "title": "裸 issue"}], {})
    )

    out = gh.github_issue_list("octo", "demo")

    assert out.success is True
    assert "裸 issue" in out.content


# ---------------------- 参数校验：非法输入是 invalid_argument，不是 internal_error ----------------------


def test_non_numeric_limit_is_invalid_argument(gh):
    """`limit="abc"` 曾经抛裸 ValueError → guard 判成 internal_error（"无需重试"），与纪律相反。"""
    out = gh.github_search_repos("x", limit="abc")

    assert out.success is False
    assert out.error_type == "invalid_argument"


def test_non_numeric_number_is_invalid_argument(gh):
    out = gh.github_pr_view("octo", "demo", "abc")

    assert out.success is False
    assert out.error_type == "invalid_argument"


def test_owner_must_be_a_name_not_a_url(gh):
    """把整条 URL 当 owner 传是最常见的错法——要当场点破，而不是拼条坏 URL 去撞 404。"""
    out = gh.github_repo_view("https://github.com/octo", "demo")

    assert out.success is False
    assert out.error_type == "invalid_argument"
    assert "不要给整条 URL" in out.content


def test_branch_name_with_slash_is_escaped_in_tree_path(gh, monkeypatch):
    """分支名 `feature/foo` 要整体转义成**一个**路径段——`quote(ref)` 默认不转义 `/`，会把 URL 拆开。"""
    seen: list[str] = []
    monkeypatch.setattr(
        gh, "_request", lambda path, params=None, **_kw: (seen.append(path), {"tree": []})[1]
    )

    gh.github_tree("octo", "demo", ref="feature/foo")

    assert seen[0].endswith("/git/trees/feature%2Ffoo")


# ---------------------- PR diff / files ----------------------


def test_pr_diff_goes_through_the_raw_text_channel(gh, monkeypatch):
    """diff 不是 JSON：必须走原文通道，并带上 `v3.diff` 的 Accept（硬 json.loads 会炸）。"""
    seen: list[dict] = []
    monkeypatch.setattr(
        gh,
        "_request",
        lambda path, params=None, **kw: (seen.append(kw), ("diff --git a/x b/x\n+1", {}))[1],
    )

    out = gh.github_pr_diff("octo", "demo", 3)

    assert out.success is True
    assert seen[0]["raw"] is True
    assert seen[0]["accept"] == gh._DIFF_ACCEPT
    assert "diff --git" in out.content


def test_pr_files_clips_each_patch(gh, monkeypatch):
    """单个文件的 patch 太长只留开头一段——否则第一个文件就能吃掉整个回执预算。"""
    long_patch = "\n".join(f"+line {i}" for i in range(200))
    monkeypatch.setattr(
        gh,
        "_request",
        lambda path, params=None, **_kw: (
            [
                {
                    "filename": "a.py",
                    "additions": 200,
                    "deletions": 0,
                    "status": "modified",
                    "patch": long_patch,
                },
                {
                    "filename": "b.py",
                    "additions": 1,
                    "deletions": 1,
                    "status": "modified",
                    "patch": "+x\n-y",
                },
            ],
            {},
        ),
    )

    out = gh.github_pr_files("octo", "demo", 5)

    assert out.success is True
    assert "a.py" in out.content and "b.py" in out.content
    assert "已截断" in out.content


# ---------------------- 写工具：结构性拒绝、回执、上游翻译 ----------------------


def test_review_refuses_approve_without_sending_anything(gh, monkeypatch):
    """APPROVE 是 §4 的**结构性拒绝**：压根不该发请求（不是"发了再等人拒"）。"""
    calls: list[dict] = []
    monkeypatch.setattr(
        gh, "_request", lambda path, params=None, **kw: (calls.append(kw), ({}, {}))[1]
    )

    out = gh.github_pr_review("octo", "demo", 7, "看着不错", event="APPROVE")

    assert out.success is False
    assert out.error_type == "invalid_argument"
    assert "批准" in out.content
    assert calls == []


def test_review_posts_body_and_event(gh, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        gh,
        "_request",
        lambda path, params=None, **kw: (
            calls.append({"path": path, **kw}),
            ({"state": "CHANGES_REQUESTED", "html_url": "https://github.com/octo/demo/pull/7"}, {}),
        )[1],
    )

    out = gh.github_pr_review("octo", "demo", 7, "第 12 行缺边界检查", event="REQUEST_CHANGES")

    assert out.success is True
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/repos/octo/demo/pulls/7/reviews"
    assert calls[0]["body"] == {"body": "第 12 行缺边界检查", "event": "REQUEST_CHANGES"}


def test_pr_create_requires_a_body(gh):
    """空描述 PR 由工具结构性挡掉（§2.2 #5），不靠提示词。"""
    out = gh.github_pr_create("octo", "demo", "标题", "fix/x", "main", body="   ")

    assert out.success is False
    assert out.error_type == "invalid_argument"
    assert "body" in out.content


def test_pr_create_receipt_carries_number_and_url(gh, monkeypatch):
    """回执要带 PR 号与 URL——人得拿它去点合并。"""
    monkeypatch.setattr(
        gh,
        "_request",
        lambda path, params=None, **_kw: (
            {
                "number": 12,
                "title": "修登录",
                "html_url": "https://github.com/octo/demo/pull/12",
                "head": {"label": "fix/login"},
                "base": {"label": "main"},
                "state": "open",
                "draft": False,
            },
            {},
        ),
    )

    out = gh.github_pr_create("octo", "demo", "修登录", "fix/login", "main", body="动机：…")

    assert out.success is True
    assert "#12" in out.content
    assert "https://github.com/octo/demo/pull/12" in out.content
    assert "合并由人来做" in out.content


def test_draft_switched_off_as_a_string_stays_off(gh, monkeypatch):
    """MCP 层可能把 `"false"` 当字符串递进来——`bool("false")` 是 True，草稿开关会被悄悄打开。"""
    seen: list[dict] = []
    monkeypatch.setattr(
        gh,
        "_request",
        lambda path, params=None, **kw: (
            seen.append(kw["body"]),
            ({"number": 1, "html_url": "u", "state": "open"}, {}),
        )[1],
    )

    gh.github_pr_create("octo", "demo", "t", "b1", "main", body="动机", draft="false")

    assert seen[0]["draft"] is False


def test_write_422_is_translated_into_an_actionable_message(gh, monkeypatch):
    """写操作的 422 与读操作的失败**下一步不同**：要说清"参数或目标状态不允许"。"""
    monkeypatch.setattr(
        gh.urllib.request,
        "urlopen",
        _urlopen_raising(_http_error(422, '{"message":"Validation Failed"}')),
    )

    out = gh.github_issue_comment("octo", "demo", 9, "看这里")

    assert out.success is False
    assert out.error_type == "upstream_error"
    assert "422" in out.content
    assert "不接受这次改动" in out.content


def test_write_403_mentions_protected_branch_not_just_bad_token(gh, monkeypatch):
    """写操作的 403 还可能是"分支受保护 / 没推送权限"——只提 token 会把模型引向错方向。"""
    monkeypatch.setattr(
        gh.urllib.request,
        "urlopen",
        _urlopen_raising(
            _http_error(
                403, '{"message":"protected branch"}', {"X-RateLimit-Remaining": "4999"}
            )
        ),
    )

    out = gh.github_issue_comment("octo", "demo", 9, "看这里")

    assert out.success is False
    assert "受保护" in out.content
    assert "限流" not in out.content


# ---------------- 只读补齐：评论 / 评审读取；file_read 的二进制回执 ----------------


def test_issue_comments_render_floors(gh, monkeypatch):
    """对话楼层带序号、作者与正文；请求打到 issues 端点并带上分页参数。"""
    payload = [
        {"id": 11, "user": {"login": "alice"}, "created_at": "2026-09-14T10:00:00Z", "body": "第一层"},
        {"id": 12, "user": {"login": "bob"}, "created_at": "2026-09-14T11:00:00Z", "body": "第二层"},
    ]
    seen = {}

    def fake(path, params=None, **_kw):
        seen["path"], seen["params"] = path, params
        return payload, {}

    monkeypatch.setattr(gh, "_request", fake)

    out = gh.github_issue_comments("octo", "demo", 42)

    assert out.success is True
    assert seen["path"] == "/repos/octo/demo/issues/42/comments"
    assert seen["params"]["per_page"] == 20 and seen["params"]["page"] == 1
    assert "【楼层 1】alice" in out.content and "第一层" in out.content
    assert "【楼层 2】bob" in out.content and "第二层" in out.content


def test_issue_comments_empty_gives_guidance(gh, monkeypatch):
    """没有评论时不给空正文，指回正文读取工具。"""
    monkeypatch.setattr(gh, "_request", lambda path, params=None, **_kw: ([], {}))

    out = gh.github_issue_comments("octo", "demo", 7)

    assert out.success is True
    assert "没有对话评论" in out.content


def test_pr_reviews_translate_states_and_empty_body(gh, monkeypatch):
    """评审结论翻译成人话；无总评正文的轮次给说明，不输出空段。"""
    payload = [
        {
            "user": {"login": "carol"},
            "state": "CHANGES_REQUESTED",
            "submitted_at": "2026-09-14T09:00:00Z",
            "body": "这里会空指针",
        },
        {
            "user": {"login": "dave"},
            "state": "COMMENTED",
            "submitted_at": "2026-09-14T09:30:00Z",
            "body": "",
        },
    ]
    monkeypatch.setattr(gh, "_request", lambda path, params=None, **_kw: (payload, {}))

    out = gh.github_pr_reviews("octo", "demo", 5)

    assert out.success is True
    assert "要求修改" in out.content and "会空指针" in out.content
    assert "仅评论" in out.content and "意见可能写在具体代码行上" in out.content


def test_pr_reviews_empty_gives_guidance(gh, monkeypatch):
    monkeypatch.setattr(gh, "_request", lambda path, params=None, **_kw: ([], {}))

    out = gh.github_pr_reviews("octo", "demo", 5)

    assert out.success is True
    assert "没有任何评审" in out.content


def test_file_read_binary_returns_receipt_not_garbage(gh, monkeypatch):
    """远端 PNG：NUL 探测 + 类型回执（与本地 read_file 同款），不吐 replace 乱码。"""
    import base64 as b64

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    monkeypatch.setattr(
        gh,
        "_request",
        lambda path, params=None, **_kw: (
            {
                "type": "file",
                "encoding": "base64",
                "content": b64.b64encode(png).decode(),
                "path": "pic.png",
                "size": len(png),
            },
            {},
        ),
    )

    out = gh.github_file_read("octo", "demo", "pic.png")

    assert out.success is True
    assert "二进制文件" in out.content and "PNG" in out.content
    assert "�" not in out.content  # 没有替换字符乱码


def test_file_read_utf8_text_unaffected(gh, monkeypatch):
    """二进制回执不得波及正常路径：文本文件照常解出正文。"""
    import base64 as b64

    monkeypatch.setattr(
        gh,
        "_request",
        lambda path, params=None, **_kw: (
            {
                "type": "file",
                "encoding": "base64",
                "content": b64.b64encode("hello".encode()).decode(),
                "path": "a.txt",
                "size": 5,
            },
            {},
        ),
    )

    out = gh.github_file_read("octo", "demo", "a.txt")

    assert out.success is True
    assert "hello" in out.content
