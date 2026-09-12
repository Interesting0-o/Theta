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

_API = "https://api.github.com"
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
        if exc.code in (401, 403):
            raise _UpstreamError(
                f"GitHub 拒绝了这次请求（{exc.code}，可能是令牌无效/过期/权限不足，或触发了限流）："
                f"{detail}"
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
    """统一的"取一次 + 渲染"：上游/网络失败翻成 ToolResult，成功交给 render 出正文。"""
    try:
        data, _headers = _request(path, params)
    except _UpstreamError as exc:
        return ToolResult(success=False, error_type=exc.error_type, content=str(exc))
    return ToolResult(success=True, content=_clip(render(data)))


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
def github_auth_status() -> ToolResult:
    """检查 GitHub 凭证是否可用，并回报当前账号与剩余配额。排障第一步。"""
    try:
        data, headers = _request("/user")
    except _UpstreamError as exc:
        return ToolResult(success=False, error_type=exc.error_type, content=str(exc))

    remaining, limit = headers.get("X-RateLimit-Remaining"), headers.get("X-RateLimit-Limit")
    quota = f"\nAPI 配额：{remaining}/{limit}" if remaining is not None else ""
    return ToolResult(
        success=True,
        content=(
            f"已认证：{data.get('login')}（{data.get('name') or '未填姓名'}）\n"
            f"类型：{data.get('type')}　公开仓库：{data.get('public_repos')}{quota}"
        ),
    )


@mcp.tool()
@guard
def github_repo_view(owner: str, repo: str) -> ToolResult:
    """看一个仓库的概览：描述、默认分支、语言、星标、开放 issue 数、是否私有。"""
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
    """列仓库的文件树（不需要克隆）。ref 缺省用默认分支；path 只看某个子目录。"""
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
        paths = [
            item["path"]
            for item in entries
            if item.get("type") == "blob"
            and (not prefix or item["path"] == prefix or item["path"].startswith(f"{prefix}/"))
        ]
        if not paths:
            return f"{owner}/{repo}@{ref} 下没有匹配 {path or '(根)'} 的文件"
        head = "\n".join(f"- {p}" for p in paths[:300])
        more = f"\n…（共 {len(paths)} 个文件，只列了前 300 个）" if len(paths) > 300 else ""
        truncated = "\n（GitHub 提示这棵树被截断了，内容可能不全）" if data.get("truncated") else ""
        return f"{owner}/{repo}@{ref} 的文件（{len(paths)} 个）：\n{head}{more}{truncated}"

    return _fetch(f"/repos/{owner}/{repo}/git/trees/{urllib.parse.quote(ref)}", render, {"recursive": 1})


@mcp.tool()
@guard
def github_file_read(owner: str, repo: str, path: str, ref: str = "") -> ToolResult:
    """**不克隆就读远端文件正文**（只读）。ref 缺省用默认分支；目录会退化成列目录。"""
    owner, repo = _require(owner, "owner"), _require(repo, "repo")
    path = _require(path, "path").strip("/")
    ref = (ref or "").strip()

    def render(data) -> str:
        if isinstance(data, list):  # 给的是目录 → API 返回条目数组
            entries = "\n".join(f"- {item.get('type')}: {item.get('path')}" for item in data)
            return f"{path} 是个目录，里面有：\n{entries}"
        if data.get("type") == "file" and data.get("encoding") == "base64":
            try:
                content = base64.b64decode(data.get("content") or "").decode("utf-8", errors="replace")
            except (ValueError, TypeError) as exc:
                raise _UpstreamError(f"文件内容解不开（base64 解码失败）：{exc}") from exc
            return f"{data.get('path')}（{data.get('size')} 字节）：\n\n{content}"
        # 大文件：GitHub 不给 base64（encoding=none），但给了一个可直接下载的地址
        return (
            f"{data.get('path')} 太大，API 不返回正文（{data.get('size')} 字节）。\n"
            f"可用下面这个地址取：{data.get('download_url')}"
        )

    params = {"ref": ref} if ref else None
    return _fetch(f"/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}", render, params)


@mcp.tool()
@guard
def github_search_repos(query: str, limit: int = 10) -> ToolResult:
    """按关键词搜仓库（GitHub 搜索语法可用，如 `language:python stars:>100`）。"""
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
    """在代码里搜（需要凭证；`repo:owner/name` 限定仓库，`in:file` 等语法可用）。"""
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
    """看一个 issue（或 PR）的正文与状态、标签、评论数。"""
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
    """看一个 PR 的状态：源/目标分支、是否可合并、增删行数、正文。"""
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
