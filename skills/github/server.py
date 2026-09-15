"""GitHub 技能的**能力侧**：14 个只读工具 + 3 个写工具（仓库 / 目录树 / 文件 / 搜索 / release / issue / PR / 评论与评审读取）。

由 `get_skill("github")` 按需拉起（`python -m skills.github.server`），不由模型直接执行。
本文件**不读 `SKILL.md`**（§2.2 的不变量）：正文永远由 host 侧读、注入系统提示；server 只管工具。
约束里"软"的那半（什么时候用、怎么写 PR 描述）在 `../SKILL.md`；"硬"的那半（哪些要审批、
哪些直接拒）在 `app/agent/tool.json` 与本文件的实现里。

写工具是 `github_pr_create` / `github_pr_review` / `github_issue_comment`（都要人批），越权的
动作在工具内**结构性拒绝**（`github_pr_review` 不接受 `APPROVE`）。
**没有"合并 PR"这个工具**：合并权留给人，最彻底的落点就是这个动作压根不存在——`github_pr_view`
回的"可自动合并：true/false"够模型说清状况。**推送**也不在这里：`git push` 走核心的
run_command（平台无关、不持有任何凭证，理由见 docs/SKILL_DESIGN.md §8.3）。

用 stdlib 的 `urllib.request` 而不是 PyGithub：读写都只是现成的 REST 端点，多一个三方依赖就要动
主 `pyproject.toml`（§9 已决：技能依赖进主 venv，不做隔离）——能不加就不加。
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.fastmcp import FastMCP

from app.exception import ConfigError, InvalidArgumentError
from app.schema.agent_schema import ToolResult
from mcp_service.utils import guard

# env 在 **import 时**校验（同 mcp_service/file_io.py 的 WORKSPACE_PATH）：缺凭证就让进程起不来，
# 由 host 侧翻成"技能加载失败：缺 GITHUB_TOKEN"——比起来了之后再逐个工具失败清楚得多。
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    raise ConfigError("GITHUB_TOKEN 未设置（GitHub 技能需要它，请在项目根的 .env 里配置）")

# API 根：默认官方 API，可用 `GITHUB_API_URL` 覆盖（GitHub Enterprise，或测试里指向本地桩）。
# ⚠️ 生产里**现在只有默认值这一条路**：host 侧只转发 `skill.json` 声明且落在白名单里的键
# （§13.5），而白名单映射的是 `Settings` 字段——`GITHUB_API_URL` 不在其中，所以这个 env 目前
# **只有测试会设**（子进程的 env 由 host 整体给，不继承父进程环境）。要让用户真能指向
# Enterprise，得先给 env 机制加"可选键 + 默认值"（现在是"声明了就必须非空"，声明它反而会让
# 默认路径加载失败），记在 §13.9。
_API = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
_TIMEOUT_SECONDS = 20
_USER_AGENT = "theta-agent"
# 单次返回给模型的正文字符上限：GitHub 的 diff / 文件 / 目录树动辄几十 KB，
# 整份塞进上下文既贵又没用（要更多可以再调一次、换参数）。
_MAX_CHARS = 6000
# 响应格式：默认走 JSON（`+json` 是 GitHub 推荐的稳定媒体类型）；PR 的区间 diff 是例外——
# 它要 `v3.diff` 的**纯文本** patch，硬 `json.loads` 会炸，所以走 `_fetch_text`。
_JSON_ACCEPT = "application/vnd.github+json"
_DIFF_ACCEPT = "application/vnd.github.v3.diff"


class _UpstreamError(Exception):
    """上游失败（HTTP 4xx/5xx、网络不通）——各工具把它翻成 `ToolResult(error_type="upstream_error")`。

    单独一个异常类型是为了让"参数非法"（`InvalidArgumentError`，调用方自己造成的）与
    "上游不给"（令牌过期、限流、仓库不存在）在回执里区分得开——两者的下一步动作完全不同。
    """


def _rate_limited_reason(exc: urllib.error.HTTPError, detail: str) -> str | None:
    """识别"限流"并给出可行动的说明；不是限流 → None。

    403 有两种截然相反的含义——"令牌没这个权限"（要换 token）与"配额用尽"（等一会儿就好），
    下一步动作完全不同；笼统说成"可能令牌不足、也可能限流"等于让模型猜。判据用**响应头**而不是
    猜正文：`X-RateLimit-Remaining: 0` 是主限流的确定信号；429 与正文里的 secondary rate limit
    是二级限流（短时高频触发，跟小时配额无关）。
    """
    headers = exc.headers if exc.headers is not None else {}
    remaining = headers.get("X-RateLimit-Remaining")
    secondary = "secondary rate limit" in detail.lower()
    if exc.code != 429 and remaining != "0" and not secondary:
        return None

    kind = "二级限流（短时请求过密）" if (exc.code == 429 or secondary) else "小时配额用尽"
    reset = str(headers.get("X-RateLimit-Reset") or "")
    when = ""
    if reset.isdigit():
        when = f"，约 {time.strftime('%H:%M', time.localtime(int(reset)))} 恢复"
    return (
        f"GitHub 限流：{kind}{when}（配额上限 {headers.get('X-RateLimit-Limit', '未知')}/小时）。"
        f"**不要原样重试**（重试只会继续失败）：等恢复后再调，或换一个 token。"
    )


def _http_error_message(exc: urllib.error.HTTPError, path: str, method: str) -> str:
    """把上游 HTTP 错误翻成"下一步该干什么"的说明。

    写操作（`method != "GET"`）多两种失败形态，且**下一步动作完全不同**，别混成一句"失败了"：
    403 还可能是"分支受保护 / 没有推送权限"，422 是"参数或目标状态不允许"（base 分支不存在、
    PR 已存在、issue 已关闭之类）。
    """
    detail = exc.read().decode("utf-8", errors="replace")[:200]
    if exc.code == 404:
        return f"GitHub 说没有这个东西（404）：{path} {detail}"
    limited = _rate_limited_reason(exc, detail)
    if limited is not None:
        return limited
    if exc.code in (401, 403):
        reason = (
            "令牌无效 / 过期，或权限不足（例如读私有仓库需要 repo 权限）"
            if method == "GET"
            else "令牌没有这个写权限，或目标分支受保护，或你没有推送权限"
        )
        return f"GitHub 拒绝了这次请求（{exc.code}，多半是{reason}）：{detail}"
    if exc.code == 422 and method != "GET":
        return (
            f"GitHub 不接受这次改动（422，参数或目标状态不允许——例如目标分支不存在、"
            f"同名 PR 已存在、issue 已关闭）：{detail}"
        )
    return f"GitHub 返回 {exc.code}：{detail}"


def _request(
    path: str,
    params: dict | None = None,
    *,
    accept: str = _JSON_ACCEPT,
    method: str = "GET",
    body: dict | None = None,
    raw: bool = False,
):
    """发一次 HTTP 请求，返回 (响应体, 响应头)。

    - `body` 非空 → 带 JSON 请求体，`method` 默认该给 `POST`（写操作用 `_submit`）。
    - `raw=True` → 响应体是**原文**（PR 的 diff 不是 JSON）；否则解析成 JSON。
    """
    url = f"{_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": accept,
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            text = response.read().decode("utf-8", errors="replace")
            status = getattr(response, "status", 200)
            response_headers = dict(response.headers)
    except urllib.error.HTTPError as exc:
        raise _UpstreamError(_http_error_message(exc, path, method)) from exc
    except urllib.error.URLError as exc:
        raise _UpstreamError(f"连不上 GitHub（网络问题）：{exc.reason}") from exc

    if raw:
        return text, response_headers
    try:
        return json.loads(text), response_headers
    except json.JSONDecodeError as exc:
        if not text.strip():
            raise _UpstreamError(
                f"GitHub 返回了空响应体（HTTP {status}），没有内容可解析。"
            ) from exc
        raise _UpstreamError(
            f"GitHub 返回了非 JSON 内容（HTTP {status}，前 200 字）：{text[:200]}"
        ) from exc


def _clip(text: str, limit: int = _MAX_CHARS) -> str:
    """截断过长正文并注明——模型看得见"还有没给的"，才能决定要不要换参数再取。"""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…（已截断，完整内容共 {len(text)} 字符；可用参数缩小范围再取）"


def _dispatch(
    path: str,
    render,
    params: dict | None = None,
    *,
    method: str = "GET",
    body: dict | None = None,
    accept: str = _JSON_ACCEPT,
    raw: bool = False,
) -> ToolResult:
    """统一的"发一次 + 渲染"：上游/网络失败翻成 ToolResult，成功交给 render 出正文。

    **`render` 必须在 try 内调用**：它也会抛 `_UpstreamError`（如 `github_file_read` 撞上
    解不开的 base64）。漏到这里外面就没人认得出这是上游问题——`guard` 会按"内部 bug"
    处理，模型收到的是 `internal_error` + "无需重试"，而事实恰恰相反（这个文件取不到，换参数
    或换个路径才有意义）。
    """
    try:
        data, _headers = _request(path, params, accept=accept, method=method, body=body, raw=raw)
        content = render(data)
    except _UpstreamError as exc:
        return ToolResult(success=False, error_type="upstream_error", content=str(exc))
    return ToolResult(success=True, content=_clip(content))


def _fetch(path: str, render, params: dict | None = None) -> ToolResult:
    """只读 GET + JSON 响应——只读工具都走这里。"""
    return _dispatch(path, render, params)


def _fetch_text(path: str, render, params: dict | None = None, *, accept: str) -> ToolResult:
    """只读 GET + **原文**响应：PR 的区间 diff 是 patch 文本，不是 JSON。"""
    return _dispatch(path, render, params, accept=accept, raw=True)


def _submit(path: str, render, body: dict, *, method: str = "POST") -> ToolResult:
    """写操作：发一次带 JSON 请求体的请求，把响应渲染成回执。

    走到这里说明人已经批过了（写工具在 `tool.json` 里全是 `need_review: true`）。
    """
    return _dispatch(path, render, body=body, method=method)


def _require(value: str, what: str) -> str:
    """必填参数校验：空/空白就抛 InvalidArgumentError（guard 会翻成 error_type=invalid_argument）。

    参数非法**不要 raise 裸 ValueError**——那会被 guard 当成内部 bug（CLAUDE.md 的工具纪律）。
    """
    text = (value or "").strip()
    if not text:
        raise InvalidArgumentError(f"{what} 不能为空")
    return text


def _require_slug(value: str, what: str) -> str:
    """`owner` / `repo` 这类**路径段**参数：非空，且不含 `/` 与空白。

    它们会被拼进 API 路径（`/repos/{owner}/{repo}`），含 `/` 就把路径段拆开、请求会指到别处。
    模型常见的错法是把整条 URL 当 owner 传（`https://github.com/octo`）——报错要直接点破这件事，
    否则它只会看到一个莫名其妙的 404。
    """
    text = _require(value, what)
    if "/" in text or any(char.isspace() for char in text):
        raise InvalidArgumentError(
            f"{what} 只能是名字本身（如 octo / demo），不能含 / 或空格——"
            f"不要给整条 URL。收到的是 {text!r}"
        )
    return text


def _one_of(value: str, what: str, allowed: tuple[str, ...], default: str, *, why: str = "") -> str:
    """枚举参数校验（`state` / `event` 这类）：只接受白名单里的值，其余一律 `InvalidArgumentError`。

    白名单不只是"替模型改错字"——`github_pr_review` 的 `event` 白名单就是 §4 的**结构性拒绝**：
    越权的动作（`APPROVE`）压根不该发生，所以这里直接拒、不问人。`why` 用来说清理由。
    """
    text = (value or default).strip() or default
    if text not in allowed:
        raise InvalidArgumentError(
            f"{what} 只能是 {' / '.join(allowed)}，收到的是 {text!r}。{why}".rstrip()
        )
    return text


def _as_int(value, what: str, default: int, *, low: int, high: int) -> int:
    """整数参数校验：解析失败 → `InvalidArgumentError`；越界**夹到**边界（上限是上游的限制）。

    别用裸 `int()`：它抛的 `ValueError` 会被 guard 当成**内部 bug**（模型收到的是"无需重试"），
    而这里明明是调用方参数不对、改一下就能重试。
    """
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidArgumentError(f"{what} 要是整数，收到的是 {value!r}") from exc
    return max(low, min(number, high))


def _positive_int(value, what: str) -> int:
    """编号类参数（issue / PR 编号）：必须是正整数。"""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidArgumentError(f"{what} 必须是正整数，收到的是 {value!r}") from exc
    if number <= 0:
        raise InvalidArgumentError(f"{what} 必须是正整数（issue / PR 编号），收到的是 {number}")
    return number


def _page(value) -> int:
    """页码（从 1 开始）。"""
    return _as_int(value, "page", 1, low=1, high=100)


def _as_bool(value, what: str, default: bool = False) -> bool:
    """布尔参数校验。

    别写 `bool(value)`：MCP 层可能把 `"false"` 当字符串递进来，而 `bool("false")` 是 **True**
    ——那会把"开成草稿 PR"这类开关悄悄反过来。
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    raise InvalidArgumentError(f"{what} 要是 true / false，收到的是 {value!r}")


def _more_hint(count: int, limit: int, page: int) -> str:
    """本页条数**恰好等于** limit 时补一句翻页提示。

    不提示的话模型会默认"就这些"——列表只给一页却不说还有下一页，等于悄悄截断。
    """
    if count < limit:
        return ""
    return f"\n（本页正好 {limit} 条，可能还有更多：用 page={page + 1} 接着看）"


def _label_names(labels) -> str:
    """把 issue / PR 的标签数组渲染成 `a、b`；没有标签 → `（无）`。

    上游缺字段时不留 `None`（旧写法 `t['name']` 缺字段会 KeyError → 被 guard 当成内部 bug）。
    """
    names = [
        ((label.get("name") if isinstance(label, dict) else str(label)) or "").strip()
        for label in (labels or [])
    ]
    return "、".join(name for name in names if name) or "（无）"


def _patch_excerpt(patch: str, limit_lines: int = 40) -> str:
    """单个文件的 patch 片段（最多 limit_lines 行）。

    整份 PR 的 patch 动辄上千行，每个文件都全带会把回执挤爆（`_clip` 只会从后面一刀切）——
    所以每个文件只留开头一段，剩下的靠 `github_pr_diff` 取。
    """
    lines = patch.splitlines()
    if len(lines) <= limit_lines:
        return patch
    return "\n".join(lines[:limit_lines]) + f"\n…（这个文件的 patch 共 {len(lines)} 行，已截断）"


mcp = FastMCP("GitHub")

# ---------------------- 远端二进制探测（与本地 read_file 同款防线） ----------------------
# 口径 = mcp_service/file_io.py::_is_binary 的 NUL 嗅探（4KB 内出现 NUL 即按二进制）。
# 只判"是不是二进制"，不再猜"是什么格式"（本地那份 _MAGIC_PREFIXES 已于 2026-09-15 删除，
# 这里同步）——路径里的后缀已随回执回显，猜格式交给模型。

# ---------------------- 只读工具（SKILL.md §1.1；全部免审批） ----------------------


@mcp.tool()
@guard
def github_repo_view(owner: str, repo: str) -> ToolResult:
    """
    看一个仓库的概览：描述、默认分支、主语言、星标、fork 数、开放 issue 数、是否私有。
    动手改之前用它确认默认分支（PR 的目标分支、推送的分支都由此决定）。
    Args:
        owner: 仓库属主（用户或组织名），如 `octo`。**不要**给整条 URL。
        repo: 仓库名，如 `demo`。与 owner 合成 `octo/demo`。
    """
    owner, repo = _require_slug(owner, "owner"), _require_slug(repo, "repo")

    def render(data) -> str:
        return (
            f"{data.get('full_name')}（{'私有' if data.get('private') else '公开'}）\n"
            f"描述：{data.get('description') or '（无）'}\n"
            f"默认分支：{data.get('default_branch')}　主语言：{data.get('language')}\n"
            f"星标：{data.get('stargazers_count')}　fork：{data.get('forks_count')}　"
            f"开放 issue：{data.get('open_issues_count')}\n"
            f"最后推送：{data.get('pushed_at')}"
        )

    return _fetch(f"/repos/{owner}/{repo}", render)


@mcp.tool()
@guard
def github_tree(owner: str, repo: str, ref: str = "", path: str = "") -> ToolResult:
    """
    **不克隆**列出一个仓库的文件树（走 GitHub 的 tree 接口，递归）。
    整棵大树会被截断（最多列 300 个文件、并注明总数）——想看清某个目录就用 `path` 收窄。
    Args:
        owner: 仓库属主（用户或组织名），如 `octo`。
        repo: 仓库名，如 `demo`。
        ref: 可选，看哪个版本：分支名 / tag / commit SHA 都行。留空 = 仓库的默认分支。
        path: 可选，只看某个子目录（相对仓库根的路径，如 `src/agent`）。留空 = 整棵树。
    """
    owner, repo = _require_slug(owner, "owner"), _require_slug(repo, "repo")
    ref = (ref or "").strip()

    if not ref:
        # 先问一次默认分支（同 `_dispatch` 的收口口径：上游失败一律 upstream_error）。
        try:
            repo_data, _headers = _request(f"/repos/{owner}/{repo}")
        except _UpstreamError as exc:
            return ToolResult(success=False, error_type="upstream_error", content=str(exc))
        ref = repo_data.get("default_branch") or "HEAD"

    def render(data) -> str:
        entries = data.get("tree") or []
        prefix = path.strip("/")
        paths: list[str] = []
        for item in entries:
            entry_path = item.get("path")
            if not entry_path or item.get("type") != "blob":
                continue
            if prefix and entry_path != prefix and not entry_path.startswith(f"{prefix}/"):
                continue
            paths.append(entry_path)
        if not paths:
            return f"{owner}/{repo}@{ref} 下没有匹配 {path or '(根)'} 的文件"
        head = "\n".join(f"- {p}" for p in paths[:300])
        more = (
            f"\n…（共 {len(paths)} 个文件，只列了前 300 个——用 path 缩小到某个子目录再看）"
            if len(paths) > 300
            else ""
        )
        truncated = "\n（GitHub 提示这棵树被截断了，内容可能不全）" if data.get("truncated") else ""
        return f"{owner}/{repo}@{ref} 的文件（{len(paths)} 个）：\n{head}{more}{truncated}"

    # ref 是**一个路径段**（分支名常含 `/`，如 feature/foo）：必须 safe="" 转义掉，
    # 否则 URL 会被拆成多段、指到别的端点上。下面 file_read 那处的 quote 保留 `/` 是对的
    # ——path 本来就是多段路径。
    return _fetch(
        f"/repos/{owner}/{repo}/git/trees/{urllib.parse.quote(ref, safe='')}",
        render,
        {"recursive": 1},
    )


@mcp.tool()
@guard
def github_file_read(owner: str, repo: str, path: str, ref: str = "") -> ToolResult:
    """
    **不克隆就读远端单个文件的正文**（走 contents 接口）。大文件（1–100MB）GitHub 不给正文、
    只回一个下载地址（超过 100MB 直接报错）；子模块 / 符号链接会如实说明它是什么（不是文件读不到）。
    Args:
        owner: 仓库属主（用户或组织名），如 `octo`。
        repo: 仓库名，如 `demo`。
        path: 文件路径（相对仓库根，如 `src/agent/graph.py`）。给目录会退化成"列这一层目录"。
        ref: 可选，读哪个版本：分支名 / tag / commit SHA。留空 = 默认分支。
    """
    owner, repo = _require_slug(owner, "owner"), _require_slug(repo, "repo")
    path = _require(path, "path").strip("/")
    ref = (ref or "").strip()

    def render(data) -> str:
        if isinstance(data, list):  # 给的是目录 → API 返回条目数组
            entries = "\n".join(f"- {item.get('type')}: {item.get('path')}" for item in data)
            return f"{path} 是个目录，里面有：\n{entries}"
        kind = data.get("type")
        if kind == "file" and data.get("encoding") == "base64":
            try:
                raw = base64.b64decode(data.get("content") or "")
            except (ValueError, TypeError) as exc:
                raise _UpstreamError(f"文件内容解不开（base64 解码失败）：{exc}") from exc
            # 二进制不能用 errors="replace" 硬解——那会把 PNG 变成一坨替换字符喂给模型（本地
            # read_file 2026-09-14 修过同款缺陷，这里是远端孪生）。NUL 嗅探口径与之一致。
            if b"\x00" in raw[:4096]:
                return (
                    f"{data.get('path')} 是二进制文件，"
                    f"大小 {data.get('size')} 字节，无法作为文本读取"
                )
            content = raw.decode("utf-8", errors="replace")
            return f"{data.get('path')}（{data.get('size')} 字节）：\n\n{content}"
        if kind == "submodule":
            # 曾经这里一律回"太大"——submodule 不是文件，那句话是错的，还会给一个 None 的下载地址
            return (
                f"{data.get('path')} 是**子模块**（submodule），不是文件：它指向 "
                f"{data.get('submodule_git_url')}，锁在提交 {str(data.get('sha') or '')[:12]}。\n"
                f"要看它的内容，用本技能的工具指到那个仓库去读（或在本地 git submodule update 后读）。"
            )
        if kind == "symlink":
            return (
                f"{data.get('path')} 是**符号链接**，指向 {data.get('target')}——"
                f"要读内容请直接读那个目标路径。"
            )
        if data.get("encoding") == "none":
            # 大文件：GitHub 不给 base64（encoding=none）。它通常会给一个可直接下载的地址，
            # 但那个字段**可能缺席**——旧写法会把 None 打进正文，模型就照着去取一个 "None"。
            where = data.get("download_url")
            hint = f"可用下面这个地址取：{where}" if where else "换个 ref，或用本地的 git 拿它。"
            return (
                f"{data.get('path')} 太大，API 不返回正文（{data.get('size')} 字节）。{hint}"
            )
        return (
            f"{data.get('path')} 读不到正文：GitHub 返回的条目类型是 {kind!r}（不是普通文件）。\n"
            f"可以先用 github_tree 看这一层都有什么、再挑具体文件读。"
        )

    params = {"ref": ref} if ref else None
    return _fetch(f"/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}", render, params)


@mcp.tool()
@guard
def github_search_repos(query: str, limit: int = 10) -> ToolResult:
    """
    按关键词搜仓库（GitHub 的仓库搜索语法可用）。
    Args:
        query: 搜索词，可带限定符，如 `language:python stars:>100`、`topic:llm`。
        limit: 最多列几条，1–30（超出会被夹到 30）。
    """
    query = _require(query, "query")
    limit = _as_int(limit, "limit", 10, low=1, high=30)

    def render(data) -> str:
        items = data.get("items") or []
        if not items:
            return f"没有搜到匹配 {query!r} 的仓库"
        lines = [
            f"- {item.get('full_name')}（★{item.get('stargazers_count')}）："
            f"{(item.get('description') or '').strip()[:100]}"
            for item in items[:limit]
        ]
        return f"搜到 {data.get('total_count')} 个仓库，前 {len(lines)} 个：\n" + "\n".join(lines)

    return _fetch("/search/repositories", render, {"q": query, "per_page": limit})


@mcp.tool()
@guard
def github_search_code(query: str, limit: int = 10) -> ToolResult:
    """
    在代码里搜（走 GitHub 的 code search）。只覆盖**已索引的默认分支**，超大文件搜不到；
    配合 `repo:` / `language:` 缩小范围最有用。
    Args:
        query: 搜索词 + 限定符，如 `repo:octo/demo parse_config`、`language:python "def main"`。
        limit: 最多列几条，1–30（超出会被夹到 30）。
    """
    query = _require(query, "query")
    limit = _as_int(limit, "limit", 10, low=1, high=30)

    def render(data) -> str:
        items = data.get("items") or []
        if not items:
            return f"没有搜到匹配 {query!r} 的代码"
        lines = [
            f"- {(item.get('repository') or {}).get('full_name')}: {item.get('path')}"
            for item in items[:limit]
        ]
        return f"搜到 {data.get('total_count')} 处代码，前 {len(lines)} 处：\n" + "\n".join(lines)

    return _fetch("/search/code", render, {"q": query, "per_page": limit})


@mcp.tool()
@guard
def github_issue_view(owner: str, repo: str, number: int) -> ToolResult:
    """
    看一个 issue 的正文、状态、标签、评论数与作者。PR 也能用这个接口读（issue 与 PR
    共用编号空间），但要看分支 / 可合并性请用 `github_pr_view`。
    Args:
        owner: 仓库属主（用户或组织名），如 `octo`。
        repo: 仓库名，如 `demo`。
        number: issue 编号（正整数），如 `42`。
    """
    owner, repo = _require_slug(owner, "owner"), _require_slug(repo, "repo")
    number = _positive_int(number, "number")

    def render(data) -> str:
        body = (data.get("body") or "").strip()
        return (
            f"#{data.get('number')} {data.get('title')}　[{data.get('state')}]\n"
            f"作者：{(data.get('user') or {}).get('login')}　评论：{data.get('comments')}　"
            f"更新：{data.get('updated_at')}\n"
            f"标签：{_label_names(data.get('labels'))}\n\n{body}"
        )

    return _fetch(f"/repos/{owner}/{repo}/issues/{number}", render)


@mcp.tool()
@guard
def github_pr_view(owner: str, repo: str, number: int) -> ToolResult:
    """
    看一个 PR 的状态、源/目标分支、是否可自动合并、增删行数、改动文件数、正文。
    Args:
        owner: 仓库属主（用户或组织名），如 `octo`。
        repo: 仓库名，如 `demo`。
        number: PR 编号（正整数）。
    """
    owner, repo = _require_slug(owner, "owner"), _require_slug(repo, "repo")
    number = _positive_int(number, "number")

    def render(data) -> str:
        head = data.get("head") or {}
        base = data.get("base") or {}
        body = (data.get("body") or "").strip()
        mergeable = data.get("mergeable")
        return (
            f"#{data.get('number')} {data.get('title')}　[{data.get('state')}"
            f"{'，已合并' if data.get('merged') else ''}]\n"
            f"{head.get('label')} → {base.get('label')}　"
            f"可自动合并：{mergeable if mergeable is not None else '（计算中）'}\n"
            f"改动：+{data.get('additions')} / -{data.get('deletions')} 行，"
            f"{data.get('changed_files')} 个文件　评论：{data.get('comments')}\n"
            f"作者：{(data.get('user') or {}).get('login')}　更新：{data.get('updated_at')}\n\n{body}"
        )

    return _fetch(f"/repos/{owner}/{repo}/pulls/{number}", render)


@mcp.tool()
@guard
def github_issue_comments(
    owner: str, repo: str, number: int, limit: int = 20, page: int = 1
) -> ToolResult:
    """
    读一个 issue / PR 下的**对话楼层**（每条评论的作者、时间与正文）——issue 与 PR 共用
    编号空间。挂在具体代码行上的评审评论不在这里；评审结论用 `github_pr_reviews`。
    Args:
        owner: 仓库属主（用户或组织名），如 `octo`。
        repo: 仓库名，如 `demo`。
        number: issue / PR 编号（正整数）。
        limit: 一页最多几条，1–100（默认 20）。
        page: 第几页，从 1 开始；本页正好装满时回执会提示还有下一页。
    """
    owner, repo = _require_slug(owner, "owner"), _require_slug(repo, "repo")
    number = _positive_int(number, "number")
    limit = _as_int(limit, "limit", 20, low=1, high=100)
    page = _page(page)

    def render(data) -> str:
        items = data if isinstance(data, list) else []
        if not items:
            return f"#{number} 下没有对话评论（issue/PR 本身的正文用 github_issue_view / github_pr_view）"
        lines = []
        for offset, item in enumerate(items):
            floor = (page - 1) * limit + offset + 1
            lines.append(
                f"【楼层 {floor}】{(item.get('user') or {}).get('login')}　{item.get('created_at')}\n"
                f"{(item.get('body') or '').strip()}"
            )
        return "\n\n".join(lines) + _more_hint(len(items), limit, page)

    params = {"per_page": limit, "page": page}
    return _fetch(f"/repos/{owner}/{repo}/issues/{number}/comments", render, params)


@mcp.tool()
@guard
def github_pr_reviews(
    owner: str, repo: str, number: int, limit: int = 20, page: int = 1
) -> ToolResult:
    """
    读一个 PR 的**评审结论**（每一轮评审：谁、结论、正文）——"别人为什么要求修改"在这里。
    结论是只读展示：APPROVED 的评审只能由人做出（本技能的 `github_pr_review` 只接受
    `COMMENT` / `REQUEST_CHANGES`）。对话楼层用 `github_issue_comments`。
    Args:
        owner: 仓库属主（用户或组织名），如 `octo`。
        repo: 仓库名，如 `demo`。
        number: PR 编号（正整数）。
        limit: 一页最多几条，1–100（默认 20）。
        page: 第几页，从 1 开始；本页正好装满时回执会提示还有下一页。
    """
    owner, repo = _require_slug(owner, "owner"), _require_slug(repo, "repo")
    number = _positive_int(number, "number")
    limit = _as_int(limit, "limit", 20, low=1, high=100)
    page = _page(page)

    state_labels = {
        "APPROVED": "批准（人的决定）",
        "CHANGES_REQUESTED": "要求修改",
        "COMMENTED": "仅评论",
        "DISMISSED": "已驳回",
        "PENDING": "待提交（仅作者可见）",
    }

    def render(data) -> str:
        items = data if isinstance(data, list) else []
        if not items:
            return f"#{number} 还没有任何评审（PR 下的讨论楼层用 github_issue_comments）"
        lines = []
        for offset, item in enumerate(items):
            state = str(item.get("state") or "")
            label = state_labels.get(state, state or "未知")
            body = (item.get("body") or "").strip()
            if not body:
                body = "（本轮无总评正文——意见可能写在具体代码行上）"
            lines.append(
                f"【评审 {(page - 1) * limit + offset + 1}】{(item.get('user') or {}).get('login')}"
                f"　[{label}]　{item.get('submitted_at')}\n{body}"
            )
        return "\n\n".join(lines) + _more_hint(len(items), limit, page)

    params = {"per_page": limit, "page": page}
    return _fetch(f"/repos/{owner}/{repo}/pulls/{number}/reviews", render, params)


# ---------------------- 只读：列表族（SKILL.md §1.1；全部免审批） ----------------------


@mcp.tool()
@guard
def github_issue_list(
    owner: str,
    repo: str,
    state: str = "open",
    labels: str = "",
    limit: int = 20,
    page: int = 1,
) -> ToolResult:
    """
    列一个仓库的 issue，按最近更新排序。
    ⚠️ GitHub 的这个接口会把 **PR 也当成 issue 一起返回**——本工具已经替你滤掉了，你看到的就是
    纯 issue；要找 PR 请用 `github_pr_list`。
    Args:
        owner: 仓库属主（用户或组织名），如 `octo`。
        repo: 仓库名，如 `demo`。
        state: `open` / `closed` / `all`，默认 `open`。
        labels: 可选，按标签过滤，多个用逗号分隔（如 `bug,help wanted`）。
        limit: 一页最多几条，1–100（默认 20）。
        page: 第几页，从 1 开始；本页正好装满时回执会提示还有下一页。
    """
    owner, repo = _require_slug(owner, "owner"), _require_slug(repo, "repo")
    state = _one_of(state, "state", ("open", "closed", "all"), "open")
    limit = _as_int(limit, "limit", 20, low=1, high=100)
    page = _page(page)
    labels = (labels or "").strip()

    def render(data) -> str:
        raw = data if isinstance(data, list) else []
        items = [item for item in raw if "pull_request" not in item]
        hidden = len(raw) - len(items)
        note = f"，另有 {hidden} 条 PR 已略去（要 PR 用 github_pr_list）" if hidden else ""
        if not items:
            return f"{owner}/{repo} 没有 {state} 状态的 issue{note}"
        lines = []
        for item in items:
            labels_txt = _label_names(item.get("labels"))
            tag = f"　标签：{labels_txt}" if labels_txt != "（无）" else ""
            lines.append(
                f"- #{item.get('number')} {item.get('title')}　[{item.get('state')}]　"
                f"{(item.get('user') or {}).get('login')}　评论 {item.get('comments')}　"
                f"更新 {item.get('updated_at')}{tag}"
            )
        return (
            f"{owner}/{repo} 的 {state} issue（本页 {len(items)} 条{note}）：\n"
            + "\n".join(lines)
            + _more_hint(len(raw), limit, page)
        )

    params = {"state": state, "per_page": limit, "page": page}
    if labels:
        params["labels"] = labels
    return _fetch(f"/repos/{owner}/{repo}/issues", render, params)


@mcp.tool()
@guard
def github_pr_list(
    owner: str, repo: str, state: str = "open", limit: int = 20, page: int = 1
) -> ToolResult:
    """
    列一个仓库的 PR，按最近更新排序（"有哪些在等我审 / 谁还在等合并"先看它）。
    Args:
        owner: 仓库属主（用户或组织名），如 `octo`。
        repo: 仓库名，如 `demo`。
        state: `open` / `closed` / `all`，默认 `open`。
        limit: 一页最多几条，1–100（默认 20）。
        page: 第几页，从 1 开始。
    """
    owner, repo = _require_slug(owner, "owner"), _require_slug(repo, "repo")
    state = _one_of(state, "state", ("open", "closed", "all"), "open")
    limit = _as_int(limit, "limit", 20, low=1, high=100)
    page = _page(page)

    def render(data) -> str:
        items = data if isinstance(data, list) else []
        if not items:
            return f"{owner}/{repo} 没有 {state} 状态的 PR"
        lines = [
            f"- #{item.get('number')} {item.get('title')}　[{item.get('state')}"
            f"{'，草稿' if item.get('draft') else ''}]　"
            f"{(item.get('head') or {}).get('label')} → {(item.get('base') or {}).get('label')}　"
            f"{(item.get('user') or {}).get('login')}　更新 {item.get('updated_at')}"
            for item in items
        ]
        return (
            f"{owner}/{repo} 的 {state} PR（本页 {len(items)} 条）：\n"
            + "\n".join(lines)
            + _more_hint(len(items), limit, page)
        )

    return _fetch(
        f"/repos/{owner}/{repo}/pulls",
        render,
        {"state": state, "per_page": limit, "page": page},
    )


@mcp.tool()
@guard
def github_pr_diff(owner: str, repo: str, number: int) -> ToolResult:
    """
    看一个 PR 的**完整 diff**（`base…head` 区间、所有改动文件）。
    这是本地 `git diff` 给不了的：它只知道你自己的工作树，看不到两个分支之间的**区间**，
    更看不到别人的 PR。正文太长会被截断；只想先知道"动了哪些文件、各增删几行"用
    `github_pr_files` 更省。
    Args:
        owner: 仓库属主（用户或组织名），如 `octo`。
        repo: 仓库名，如 `demo`。
        number: PR 编号（正整数）。
    """
    owner, repo = _require_slug(owner, "owner"), _require_slug(repo, "repo")
    number = _positive_int(number, "number")

    def render(text: str) -> str:
        if not text.strip():
            return f"PR #{number} 的 diff 是空的（两个分支内容相同，或这个 PR 没有改动）"
        return f"PR #{number} 的 diff（{owner}/{repo}）：\n\n{text}"

    return _fetch_text(f"/repos/{owner}/{repo}/pulls/{number}", render, accept=_DIFF_ACCEPT)


@mcp.tool()
@guard
def github_pr_files(
    owner: str, repo: str, number: int, limit: int = 20, page: int = 1
) -> ToolResult:
    """
    列一个 PR 改了哪些文件、各增删多少行，并附每个文件 patch 的开头一段。
    想知道"这个 PR 动了什么"先看它；要看完整 diff 用 `github_pr_diff`。
    Args:
        owner: 仓库属主（用户或组织名），如 `octo`。
        repo: 仓库名，如 `demo`。
        number: PR 编号（正整数）。
        limit: 一页最多几个文件，1–100（默认 20）。
        page: 第几页，从 1 开始。
    """
    owner, repo = _require_slug(owner, "owner"), _require_slug(repo, "repo")
    number = _positive_int(number, "number")
    limit = _as_int(limit, "limit", 20, low=1, high=100)
    page = _page(page)

    def render(data) -> str:
        items = data if isinstance(data, list) else []
        if not items:
            return f"PR #{number} 没有改动文件（或这一页没有更多了）"
        lines: list[str] = []
        for item in items:
            lines.append(
                f"- {item.get('filename')}（+{item.get('additions')} / -{item.get('deletions')}，"
                f"{item.get('status')}）"
            )
            patch = (item.get("patch") or "").strip()
            if patch:
                excerpt = _patch_excerpt(patch).replace("\n", "\n    ")
                lines.append(f"    ```diff\n    {excerpt}\n    ```")
        return (
            f"PR #{number} 改了 {len(items)} 个文件：\n"
            + "\n".join(lines)
            + _more_hint(len(items), limit, page)
        )

    return _fetch(
        f"/repos/{owner}/{repo}/pulls/{number}/files",
        render,
        {"per_page": limit, "page": page},
    )


@mcp.tool()
@guard
def github_release_list(owner: str, repo: str, limit: int = 10, page: int = 1) -> ToolResult:
    """
    列一个仓库的 release（"这库现在什么版本、最近发了什么"），从新到旧。
    Args:
        owner: 仓库属主（用户或组织名），如 `octo`。
        repo: 仓库名，如 `demo`。
        limit: 一页最多几条，1–100（默认 10）。
        page: 第几页，从 1 开始。
    """
    owner, repo = _require_slug(owner, "owner"), _require_slug(repo, "repo")
    limit = _as_int(limit, "limit", 10, low=1, high=100)
    page = _page(page)

    def render(data) -> str:
        items = data if isinstance(data, list) else []
        if not items:
            return f"{owner}/{repo} 还没有发布 release"
        lines = []
        for item in items:
            kind = "草稿" if item.get("draft") else ("预发布" if item.get("prerelease") else "正式")
            lines.append(
                f"- {item.get('tag_name')}　{item.get('name') or '（无标题）'}　[{kind}]　"
                f"发布 {item.get('published_at') or item.get('created_at')}"
            )
        return (
            f"{owner}/{repo} 的 release（本页 {len(items)} 条）：\n"
            + "\n".join(lines)
            + _more_hint(len(items), limit, page)
        )

    return _fetch(f"/repos/{owner}/{repo}/releases", render, {"per_page": limit, "page": page})


# ---------------------- 写工具（SKILL.md §1.2；全部 need_review: true） ----------------------
#
# §4 的**结构性拒绝**（最硬的那类：压根不问人）落在这里：
# - `github_pr_review` 的 event 只认 COMMENT / REQUEST_CHANGES——APPROVE 是人的权力；
# - **没有"合并 PR"这个工具**——合并权留给人，最彻底的落点是这个动作压根不存在。
# 审批闸门（第二类）在 app/agent/tool.json：三个都是 need_review: true。


_ALLOWED_REVIEW_EVENTS = ("COMMENT", "REQUEST_CHANGES")


@mcp.tool()
@guard
def github_pr_create(
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str,
    body: str,
    draft: bool = False,
) -> ToolResult:
    """
    开一个 PR（**写操作，需人工审批**）。`head` 必须是你**已经推上去**的分支（先用 `git push` 推上去），
    `base` 是要合进的目标分支（通常是默认分支，用 `github_repo_view` 确认）。
    `body` **必填**：空描述的 PR 等于把成本转嫁给 reviewer——写清 ① 改动的动机
    ② 怎么验证的（跑了什么、结果如何）③ 遗留事项 / 要 reviewer 特别看的地方。
    一轮里只做一个远端改动（做一步、验一步、报一步），别批量开 PR。
    Args:
        owner: 仓库属主（用户或组织名），如 `octo`。
        repo: 仓库名，如 `demo`。
        title: PR 标题（一行说清改了什么）。
        head: 源分支名（远端已有的那个，如 `fix/login`；不要写成 `owner:branch`）。
        base: 目标分支名（如 `main`）。
        body: PR 描述（必填，见上）。
        draft: 是否开成草稿 PR，默认 False。
    """
    owner, repo = _require_slug(owner, "owner"), _require_slug(repo, "repo")
    title = _require(title, "title")
    head = _require(head, "head")
    base = _require(base, "base")
    body = (body or "").strip()
    if not body:
        raise InvalidArgumentError(
            "body 不能为空：PR 描述要写清 ① 动机 ② 怎么验证的 ③ 遗留事项——"
            "空描述的 PR 把成本转嫁给了 reviewer。真只是占位就先别开 PR。"
        )
    if head == base:
        raise InvalidArgumentError("head 与 base 不能是同一个分支——PR 得有可比的改动")
    draft = _as_bool(draft, "draft")

    def render(data) -> str:
        return (
            f"已开 PR #{data.get('number')}：{data.get('title')}\n"
            f"{data.get('html_url')}\n"
            f"{(data.get('head') or {}).get('label')} → {(data.get('base') or {}).get('label')}　"
            f"状态 {data.get('state')}{'（草稿）' if data.get('draft') else ''}\n"
            f"合并由人来做——把这个链接给用户，别自己去合。"
        )

    payload = {"title": title, "head": head, "base": base, "body": body, "draft": draft}
    return _submit(f"/repos/{owner}/{repo}/pulls", render, payload)


@mcp.tool()
@guard
def github_pr_review(
    owner: str, repo: str, number: int, body: str, event: str = "COMMENT"
) -> ToolResult:
    """
    给一个 PR 提交评审意见（**写操作，需人工审批**）。
    `event` 只能是 `COMMENT`（留意见）或 `REQUEST_CHANGES`（要求修改）——**"批准"（APPROVE）
    不在本工具的能力范围内**：批准与合并一样是人的权力，传 APPROVE 会被直接拒绝。
    意见要具体：指到文件与行、说清"这里为什么有问题"，别只写"看起来不错"。
    Args:
        owner: 仓库属主（用户或组织名），如 `octo`。
        repo: 仓库名，如 `demo`。
        number: PR 编号（正整数）。
        body: 评审意见正文。
        event: `COMMENT` / `REQUEST_CHANGES`，默认 `COMMENT`。
    """
    owner, repo = _require_slug(owner, "owner"), _require_slug(repo, "repo")
    number = _positive_int(number, "number")
    event = _one_of(
        event,
        "event",
        _ALLOWED_REVIEW_EVENTS,
        "COMMENT",
        why="批准是人的权力——APPROVE 不在本工具的能力范围内，也别绕道用别的工具去批。",
    )
    body = _require(body, "body")

    def render(data) -> str:
        return (
            f"已在 PR #{number} 留下评审意见（{data.get('state') or event}）："
            f"{data.get('html_url')}\n这只是意见——能不能合并由人决定。"
        )

    return _submit(
        f"/repos/{owner}/{repo}/pulls/{number}/reviews",
        render,
        {"body": body, "event": event},
    )


@mcp.tool()
@guard
def github_issue_comment(owner: str, repo: str, number: int, body: str) -> ToolResult:
    """
    在 issue 或 PR 下留言（**写操作，需人工审批**）。GitHub 里 PR 就是 issue，所以给 PR 留言
    也走这个工具；但要指出"哪一行有什么问题"的**评审意见**请用 `github_pr_review`。
    Args:
        owner: 仓库属主（用户或组织名），如 `octo`。
        repo: 仓库名，如 `demo`。
        number: issue / PR 编号（正整数）。
        body: 留言正文。说清"为什么"，别做"我看看"这种没有信息量的回复。
    """
    owner, repo = _require_slug(owner, "owner"), _require_slug(repo, "repo")
    number = _positive_int(number, "number")
    body = _require(body, "body")

    def render(data) -> str:
        return f"已留言（{data.get('html_url')}）：\n{body}"

    return _submit(f"/repos/{owner}/{repo}/issues/{number}/comments", render, {"body": body})


if __name__ == "__main__":
    mcp.run(transport="stdio")
