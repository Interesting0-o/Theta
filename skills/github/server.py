"""GitHub 技能的**能力侧**：只读工具（仓库 / 目录树 / 文件 / 搜索 / issue / PR）。

由 `get_skill("github")` 按需拉起（`python -m skills.github.server`），不由模型直接执行。
本文件**不读 `SKILL.md`**（§2.2 的不变量）：正文永远由 host 侧读、注入系统提示；server 只管工具。
约束里"软"的那半（什么时候用、怎么写 PR 描述）在 `../SKILL.md`；"硬"的那半（哪些要审批、
哪些直接拒）在 `app/agent/tool.json` 与本文件的实现里。

**写操作（开 PR / 推送 / 合并 / 评论）尚未实现**——`SKILL.md` §1.2 列了它们，属下一期。

用 stdlib 的 `urllib.request` 而不是 PyGithub：只读 GET 用不上 SDK，而多一个三方依赖就要动
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


class _UpstreamError(Exception):
    """上游失败（HTTP 4xx/5xx、网络不通）——各工具把它翻成 `ToolResult(error_type="upstream_error")`。

    单独一个异常类型是为了让"参数非法"（`InvalidArgumentError`，调用方自己造成的）与
    "上游不给"（令牌过期、限流、仓库不存在）在回执里区分得开——两者的下一步动作完全不同。
    """

    def __init__(self, message: str, error_type: str = "upstream_error") -> None:
        super().__init__(message)
        self.error_type = error_type


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


def _request(path: str, params: dict | None = None, *, accept: str = "application/vnd.github+json"):
    """发一次 GET，返回 (解析后的 JSON, 响应头)。"""
    url = f"{_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": accept,
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
            headers = dict(response.headers)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        if exc.code == 404:
            raise _UpstreamError(f"GitHub 说没有这个东西（404）：{path} {detail}") from exc
        limited = _rate_limited_reason(exc, detail)
        if limited is not None:
            raise _UpstreamError(limited) from exc
        if exc.code in (401, 403):
            raise _UpstreamError(
                f"GitHub 拒绝了这次请求（{exc.code}，多半是令牌无效 / 过期，或权限不足"
                f"——例如读私有仓库需要 repo 权限）：{detail}"
            ) from exc
        raise _UpstreamError(f"GitHub 返回 {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise _UpstreamError(f"连不上 GitHub（网络问题）：{exc.reason}") from exc

    try:
        return json.loads(body), headers
    except json.JSONDecodeError as exc:
        raise _UpstreamError(f"GitHub 返回了非 JSON 内容（前 200 字）：{body[:200]}") from exc


def _clip(text: str, limit: int = _MAX_CHARS) -> str:
    """截断过长正文并注明——模型看得见"还有没给的"，才能决定要不要换参数再取。"""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…（已截断，完整内容共 {len(text)} 字符；可用参数缩小范围再取）"


def _fetch(path: str, render, params: dict | None = None) -> ToolResult:
    """统一的"取一次 + 渲染"：上游/网络失败翻成 ToolResult，成功交给 render 出正文。

    **`render` 必须在 try 内调用**：它也会抛 `_UpstreamError`（如 `github_file_read` 撞上
    解不开的 base64）。漏到 `_fetch` 外面就没人认得出这是上游问题——`guard` 会按"内部 bug"
    处理，模型收到的是 `internal_error` + "无需重试"，而事实恰恰相反（这个文件取不到，换参数
    或换个路径才有意义）。
    """
    try:
        data, _headers = _request(path, params)
        content = render(data)
    except _UpstreamError as exc:
        return ToolResult(success=False, error_type=exc.error_type, content=str(exc))
    return ToolResult(success=True, content=_clip(content))


def _require(value: str, what: str) -> str:
    """必填参数校验：空/空白就抛 InvalidArgumentError（guard 会翻成 error_type=invalid_argument）。

    参数非法**不要 raise 裸 ValueError**——那会被 guard 当成内部 bug（CLAUDE.md 的工具纪律）。
    """
    text = (value or "").strip()
    if not text:
        raise InvalidArgumentError(f"{what} 不能为空")
    return text


mcp = FastMCP("GitHub")


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
    owner, repo = _require(owner, "owner"), _require(repo, "repo")

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
    owner, repo = _require(owner, "owner"), _require(repo, "repo")
    ref = (ref or "").strip()

    if not ref:
        try:
            repo_data, _headers = _request(f"/repos/{owner}/{repo}")
        except _UpstreamError as exc:
            return ToolResult(success=False, error_type=exc.error_type, content=str(exc))
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

    return _fetch(f"/repos/{owner}/{repo}/git/trees/{urllib.parse.quote(ref)}", render, {"recursive": 1})


@mcp.tool()
@guard
def github_file_read(owner: str, repo: str, path: str, ref: str = "") -> ToolResult:
    """
    **不克隆就读远端单个文件的正文**（走 contents 接口）。大文件（>1MB）GitHub 不给正文，
    会回一个可直接下载的地址；子模块 / 符号链接会如实说明它是什么（不是文件读不到）。
    Args:
        owner: 仓库属主（用户或组织名），如 `octo`。
        repo: 仓库名，如 `demo`。
        path: 文件路径（相对仓库根，如 `src/agent/graph.py`）。给目录会退化成"列这一层目录"。
        ref: 可选，读哪个版本：分支名 / tag / commit SHA。留空 = 默认分支。
    """
    owner, repo = _require(owner, "owner"), _require(repo, "repo")
    path = _require(path, "path").strip("/")
    ref = (ref or "").strip()

    def render(data) -> str:
        if isinstance(data, list):  # 给的是目录 → API 返回条目数组
            entries = "\n".join(f"- {item.get('type')}: {item.get('path')}" for item in data)
            return f"{path} 是个目录，里面有：\n{entries}"
        kind = data.get("type")
        if kind == "file" and data.get("encoding") == "base64":
            try:
                content = base64.b64decode(data.get("content") or "").decode("utf-8", errors="replace")
            except (ValueError, TypeError) as exc:
                raise _UpstreamError(f"文件内容解不开（base64 解码失败）：{exc}") from exc
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
            # 大文件：GitHub 不给 base64（encoding=none），但给了一个可直接下载的地址
            return (
                f"{data.get('path')} 太大，API 不返回正文（{data.get('size')} 字节）。\n"
                f"可用下面这个地址取：{data.get('download_url')}"
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
    limit = max(1, min(int(limit or 10), 30))

    def render(data) -> str:
        items = data.get("items") or []
        if not items:
            return f"没有搜到匹配 {query!r} 的仓库"
        lines = [
            f"- {item['full_name']}（★{item['stargazers_count']}）："
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
    limit = max(1, min(int(limit or 10), 30))

    def render(data) -> str:
        items = data.get("items") or []
        if not items:
            return f"没有搜到匹配 {query!r} 的代码"
        lines = [
            f"- {item['repository']['full_name']}: {item['path']}" for item in items[:limit]
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
    owner, repo = _require(owner, "owner"), _require(repo, "repo")
    number = int(number or 0)
    if number <= 0:
        raise InvalidArgumentError("number 必须是正整数（issue / PR 编号）")

    def render(data) -> str:
        body = (data.get("body") or "").strip()
        return (
            f"#{data.get('number')} {data.get('title')}　[{data.get('state')}]\n"
            f"作者：{(data.get('user') or {}).get('login')}　评论：{data.get('comments')}　"
            f"更新：{data.get('updated_at')}\n"
            f"标签：{'、'.join(t['name'] for t in data.get('labels') or []) or '（无）'}\n\n{body}"
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
    owner, repo = _require(owner, "owner"), _require(repo, "repo")
    number = int(number or 0)
    if number <= 0:
        raise InvalidArgumentError("number 必须是正整数（PR 编号）")

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


if __name__ == "__main__":
    mcp.run(transport="stdio")
