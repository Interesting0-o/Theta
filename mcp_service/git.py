"""git MCP 服务器：在工作区（WORKSPACE_PATH）内向下查找 git 仓库。

与 file_io 一致，本模块在 import 时校验 WORKSPACE_PATH；但不要求工作区本身是
git 仓库——工作区可能是一个"项目集合"目录，仓库散落在任意子目录里，也可能
一个都没有（此时 list_repos 返回"未找到"的说明，由模型决定如何降级）。

工具清单：
- list_repos   列出工作区内所有 git 仓库（绝对路径清单），免审批只读
- git_status   某仓库的分支信息 + 改动/未跟踪清单（中文渲染），免审批只读
- git_branches 某仓库的本地/远程分支清单并标出当前分支，免审批只读
- git_diff     某仓库的暂存区/工作区改动 diff（可选 path 缩小范围），免审批只读
- git_log      某仓库最近提交历史（条数可选），免审批只读
- git_fetch    从远程拉取对象到本地远程跟踪引用（不动工作树），免审批只读
- git_add      把文件加入暂存区（写操作，需审批）
- git_commit   提交（写操作，需审批；身份沿用设备 git 配置，提交信息末尾自动
  追加 Co-authored-by: Coding Agent 合作者尾注）
- git_switch   切换当前分支（写操作，需审批）
- git_pull     从远程拉取并合并到当前分支（写操作，需审批）

查找规则：
- 从工作区根向下遍历，含 .git 条目的目录即视为一个仓库（.git 是文件也认，
  兼容 submodule / worktree 的 gitdir 引用形态），命中后不再向该仓库内部钻；
- 遍历时跳过 _IGNORED_DIRS 中的目录（虚拟环境 / 缓存 / 依赖等），这些目录
  庞杂且不可能藏业务仓库，跳过可避免把 .venv 里 pip 装的包（个别自带 .git）
  误报成工作区项目；
- 不跟随符号链接（防循环）。
"""
import asyncio
import os
import git
from git.exc import GitCommandError
import re
from pathlib import Path
from typing import List
from mcp.server.fastmcp import FastMCP


from app.exception import ConfigError, InvalidArgumentError
from app.schema.agent_schema import ToolResult
from mcp_service.utils import guard

#----------------环境变量注入处理---------------#

_workspace_path = os.environ.get("WORKSPACE_PATH")

if _workspace_path is None:
    raise ConfigError("WORKSPACE_PATH 未设置")

WORKSPACE_PATH = Path(_workspace_path).resolve()

if not WORKSPACE_PATH.exists():
    raise ConfigError(f"agent工作区路径{WORKSPACE_PATH} 不存在")


#----------------git 子进程防挂起---------------#

# git 子进程没有 TTY：凭据未缓存时 git 会弹交互式登录把工具调用挂死。
# 从源头禁用终端提示，认证失败改为快速报错（stderr 回显给模型）；
# SSH 侧建议用 ssh-agent 提供凭据，避免 passphrase 交互。
os.environ["GIT_TERMINAL_PROMPT"] = "0"


#----------------向下查询 git 仓库---------------#

# 遍历时跳过的目录名：虚拟环境 / 缓存 / 依赖 / 编辑器产物等，
# 这些目录又大又杂，不可能藏业务仓库。
_IGNORED_DIRS = frozenset({
    ".git",
    ".venv", "venv",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "node_modules", "bower_components",
    ".claude", ".deepseek", ".ide", ".idea", ".vscode",
    ".mvn", "target", "dist", "build",
})

_REPOS_PATH_CACHE: List[Path] = []
_REPOS_CACHE: dict[str, git.Repo] = {}


def _get_repos(refresh: bool = False) -> list[Path]:
    """已发现仓库清单的进程级缓存：首次扫描后复用，refresh=True 强制重扫。

    list_repos / git_status 共用此缓存；缓存只在 git_status 未命中时刷新，
    因此运行期间新建的仓库要等一次未命中触发后才会出现在清单里。
    """
    if not _REPOS_PATH_CACHE or refresh:
        _REPOS_PATH_CACHE[:] = find_git_repos()
    return list(_REPOS_PATH_CACHE)


def _resolve_repo_path(repo_path: str) -> Path:
    """把 repo_path 归一化成绝对路径：绝对路径原样，相对路径以工作区根为基准。"""
    path = Path(repo_path).expanduser()
    if not path.is_absolute():
        path = WORKSPACE_PATH / path
    return path.resolve()


def _ensure_known_repo(path: Path) -> None:
    """校验 path 是扫描发现的仓库；不是则 raise InvalidArgumentError（可修正重试）。

    命中才放行：仓库必须是扫描发现的（顺带保证路径落在工作区内）。缓存可能
    过期（扫描之后又新建了仓库），未命中时重扫一次再判断。
    """
    if path in _get_repos() or path in _get_repos(refresh=True):
        return
    raise InvalidArgumentError(f"{path} 不是工作区内有效的 git 仓库路径")


async def _get_repo(path: Path) -> git.Repo:
    """按绝对路径取 Repo 对象（进程级缓存，首次访问才构造）。"""
    key = str(path)
    repo = _REPOS_CACHE.get(key)
    if repo is None:
        repo = await asyncio.to_thread(git.Repo, path, search_parent_directories=False)
        _REPOS_CACHE[key] = repo
    return repo

def find_git_repos(root: Path = WORKSPACE_PATH) -> list[Path]:
    """向下查找 root 下所有 git 仓库根目录，按路径排序返回绝对路径列表。

    本地仓库本身没有"名字"，目录名只是目录名，因此只返回事实性的绝对路径。
    判定规则：目录含 .git 条目即视为仓库；命中后不再进入该仓库内部继续向下钻。
    遍历跳过 _IGNORED_DIRS 且不跟随符号链接，避免扫进噪声目录与符号链接环。
    """
    root = Path(root).resolve()
    repos: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _IGNORED_DIRS)
        if os.path.lexists(os.path.join(dirpath, ".git")):
            repos.append(Path(dirpath))
            dirnames[:] = []
    return sorted(repos)

mcp = FastMCP("Git")

@mcp.tool()
@guard
def list_repos() -> ToolResult:
    """
    列出工作区内找到的所有 git 仓库，无需参数。
    返回各仓库根目录的绝对路径清单；工作区里没有任何仓库时返回明确说明而非报错。
    """
    repos = _get_repos()
    if not repos:
        return ToolResult(success=True, content="工作区内未找到任何 git 仓库。")
    lines = [str(path) for path in repos]
    return ToolResult(success=True, content="工作区内 git 仓库清单：\n" + "\n".join(lines))

#----------------git_status 渲染---------------#

# porcelain v1 的 XY 双字符状态码 → 中文标签（X=暂存区，Y=工作区）
_STATUS_TAGS = {
    "M ": "已暂存修改",
    "MM": "已暂存，工作区又有新修改",
    "A ": "已暂存新文件",
    "D ": "已暂存删除",
    "R ": "已暂存重命名",
    "C ": "已暂存复制",
    " M": "工作区修改，未暂存",
    " D": "工作区删除，未暂存",
    " T": "文件类型变化，未暂存",
    "??": "未跟踪",
}


def _render_branch_header(header: str) -> str:
    """渲染 porcelain --branch 的 ## 头行（分支/上游/领先落后）。"""
    if not header.startswith("## "):
        return header
    info = header[3:]
    if info == "HEAD (no branch)":
        return "HEAD 处于游离状态（未关联任何分支）"
    if info.startswith("No commits yet on "):
        return info[len("No commits yet on "):] + "（尚无任何提交）"
    branch, sep, upstream = info.partition("...")
    if not sep:
        return f"{branch}（无上游跟踪）"
    upstream_name = upstream.split(" [", 1)[0] if " [" in upstream else upstream
    flags = []
    m_ahead = re.search(r"ahead (\d+)", upstream)
    m_behind = re.search(r"behind (\d+)", upstream)
    if m_ahead:
        flags.append(f"领先 {m_ahead.group(1)}")
    if m_behind:
        flags.append(f"落后 {m_behind.group(1)}")
    if " [gone]" in upstream:
        flags.append("上游已删除")
    suffix = ("，" + "，".join(flags)) if flags else ""
    return f"{branch}（跟踪 {upstream_name}{suffix}）"


def _render_entry(line: str) -> str:
    """给单条 porcelain 记录加中文标签（如 " M x.py  ← 工作区修改，未暂存"）。

    porcelain v1 行格式固定为"两字符状态码 + 空格 + 路径"，状态码在行首
    （含前导空格，如 " M"），不能用 partition 取第一个词。
    """
    code = line[:2]
    if "U" in code:
        return f"{line}  ← 合并冲突未解决"
    tag = _STATUS_TAGS.get(code)
    return f"{line}  ← {tag}" if tag else line


def _render_status(raw: str) -> str:
    """把 porcelain --branch 输出渲染成模型友好的中文状态文本。

    只保留事实：分支/跟踪/领先落后 + 每条改动的状态标签；git 默认长格式里的
    操作提示等模板废话不渲染（省 token，且不受 locale 影响）。
    """
    lines = raw.splitlines()
    if not lines:
        return "（git status 无输出）"
    rendered = [_render_branch_header(lines[0])]
    body = [_render_entry(line) for line in lines[1:] if line.strip()]
    rendered.extend(body if body else ["工作区干净，无改动。"])
    return "\n".join(rendered)


@mcp.tool()
@guard
async def git_status(repo_path: str) -> ToolResult:
    """
    获取指定 git 仓库的工作区状态（分支信息 + 改动/未跟踪文件清单，中文渲染）。
    Args:
        repo_path: 仓库根目录路径；传 list_repos 给出的绝对路径或相对工作区根
            的相对路径均可。
    """
    path = _resolve_repo_path(repo_path)
    _ensure_known_repo(path)

    repo = await _get_repo(path)
    raw = await asyncio.to_thread(repo.git.status, "--porcelain", "--branch")
    return ToolResult(success=True, content=_render_status(raw))

@mcp.tool()
@guard
async def git_diff(repo_path: str, path: str = "") -> ToolResult:
    """
    查看指定 git 仓库的改动内容：暂存区（相对 HEAD）与工作区（未暂存）的 diff。
    注意：未跟踪文件（?? 状态）不包含在 git diff 输出里，先看 git_status 确认全貌。
    Args:
        repo_path: 仓库根目录路径；传 list_repos 给出的绝对路径或相对工作区根
            的相对路径均可。
        path: 可选，只显示某个文件或目录的改动（相对 repo_path 的路径）；
            留空表示整个仓库。改动量大时建议用它缩小范围。
    """
    repo_root = _resolve_repo_path(repo_path)
    _ensure_known_repo(repo_root)

    target: str | None = None
    if path:
        resolved = (repo_root / path).resolve()
        if not resolved.is_relative_to(repo_root):
            raise InvalidArgumentError(f"{path} 不在仓库 {repo_root} 内")
        target = path

    repo = await _get_repo(repo_root)

    def _diffs() -> tuple[str, str]:
        if target:
            return repo.git.diff("--staged", "--", target), repo.git.diff("--", target)
        return repo.git.diff("--staged"), repo.git.diff()

    staged, unstaged = await asyncio.to_thread(_diffs)

    lines: List[str] = []
    if staged.strip():
        lines.append("── 暂存区改动（相对 HEAD）──")
        lines.append(staged.strip())
    else:
        lines.append("暂存区无改动。")
    if unstaged.strip():
        lines.append("")
        lines.append("── 工作区未暂存改动 ──")
        lines.append(unstaged.strip())
    else:
        lines.append("工作区无未暂存改动。")
    return ToolResult(success=True, content="\n".join(lines))


@mcp.tool()
@guard
async def git_log(repo_path: str, max_count: int = 20) -> ToolResult:
    """
    查看指定 git 仓库的最近提交历史（当前分支，从最新往前）。
    Args:
        repo_path: 仓库根目录路径；传 list_repos 给出的绝对路径或相对工作区根
            的相对路径均可。
        max_count: 最多返回的提交条数，默认 20，上限 500。
    """
    repo_root = _resolve_repo_path(repo_path)
    _ensure_known_repo(repo_root)

    if max_count <= 0:
        raise InvalidArgumentError(f"max_count 必须是正整数，收到 {max_count!r}")
    if max_count > 500:
        raise InvalidArgumentError("max_count 过大（上限 500）")

    repo = await _get_repo(repo_root)

    if not await asyncio.to_thread(repo.head.is_valid):
        return ToolResult(success=True, content="该仓库尚无任何提交（HEAD 未诞生）。")

    def _commits() -> list[str]:
        return [
            f"{c.hexsha[:8]} {c.committed_datetime.strftime('%Y-%m-%d')} {c.summary}"
            for c in repo.iter_commits("HEAD", max_count=max_count)
        ]

    commits = await asyncio.to_thread(_commits)
    if not commits:
        return ToolResult(success=True, content="该仓库尚无任何提交。")
    return ToolResult(success=True, content="最近提交（新 → 旧）：\n" + "\n".join(commits))


# 远程同步操作的超时（秒）：网络 / 认证卡住时放弃等待，避免工具调用永久挂起。
# 注意 wait_for 只能放弃等待、杀不掉 GitPython 已在跑的子进程，
# 因此防挂起主要靠上面的 GIT_TERMINAL_PROMPT=0，超时只是兜底。
_SYNC_TIMEOUT = 60


@mcp.tool()
@guard
async def git_fetch(repo_path: str, remote: str = "origin") -> ToolResult:
    """
    从远程仓库拉取最新对象到本地远程跟踪引用（不动工作树与当前分支）。
    需要设备上已配置好对应远程的认证（SSH key / 凭据管理器），失败会回显原因。
    Args:
        repo_path: 仓库根目录路径；传 list_repos 给出的绝对路径或相对工作区根
            的相对路径均可。
        remote: 远程名，默认 origin。
    """
    repo_root = _resolve_repo_path(repo_path)
    _ensure_known_repo(repo_root)

    if not remote or not remote.strip():
        raise InvalidArgumentError("remote 不能为空")

    repo = await _get_repo(repo_root)

    try:
        await asyncio.wait_for(
            asyncio.to_thread(repo.git.fetch, remote),
            timeout=_SYNC_TIMEOUT,
        )
    except TimeoutError:
        return ToolResult(success=False, content="fetch 超时（60 秒），疑似网络或认证问题。")
    except GitCommandError as exc:
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        return ToolResult(success=False, content=f"fetch 失败：{stderr}")
    return ToolResult(success=True, content=f"已从远程 {remote} 拉取最新对象（工作树与当前分支未受影响）。")


@mcp.tool()
@guard
async def git_pull(repo_path: str, remote: str = "origin", branch: str = "") -> ToolResult:
    """
    从远程拉取并合并到当前分支（写操作，需人工审批）。
    工作树有未提交改动且与拉取内容冲突时 git 会拒绝合并（stderr 会说明）；
    分支分叉时可能产生合并提交。
    Args:
        repo_path: 仓库根目录路径；传 list_repos 给出的绝对路径或相对工作区根
            的相对路径均可。
        remote: 远程名，默认 origin。
        branch: 要拉取的远程分支；留空则拉取当前分支配置的上游分支
            （未配置上游会失败并回显 git 提示）。
    """
    repo_root = _resolve_repo_path(repo_path)
    _ensure_known_repo(repo_root)

    if not remote or not remote.strip():
        raise InvalidArgumentError("remote 不能为空")

    repo = await _get_repo(repo_root)

    try:
        if branch and branch.strip():
            await asyncio.wait_for(
                asyncio.to_thread(repo.git.pull, remote, branch),
                timeout=_SYNC_TIMEOUT,
            )
        else:
            await asyncio.wait_for(
                asyncio.to_thread(repo.git.pull, remote),
                timeout=_SYNC_TIMEOUT,
            )
    except TimeoutError:
        return ToolResult(success=False, content="pull 超时（60 秒），疑似网络或认证问题。")
    except GitCommandError as exc:
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        return ToolResult(success=False, content=f"pull 失败：{stderr}")

    def _after() -> str:
        if repo.head.is_detached:
            return f"当前处于游离 HEAD（{repo.head.commit.hexsha[:12]}）"
        commit = repo.head.commit
        return f"当前分支 {repo.active_branch.name}，最新提交 {commit.hexsha[:8]} {commit.summary}"

    return ToolResult(success=True, content="拉取成功。" + await asyncio.to_thread(_after))


@mcp.tool()
@guard
async def git_add(repo_path: str, files: List[str]) -> ToolResult:
    """
    将文件添加到指定仓库的暂存区（写操作，需人工审批）。
    Args:
        repo_path: 仓库根目录路径；传 list_repos 给出的绝对路径或相对工作区根
            的相对路径均可。
        files: 要暂存的文件/目录路径列表；相对 repo_path 的路径，不允许越过仓库边界。
    """
    path = _resolve_repo_path(repo_path)
    _ensure_known_repo(path)

    if not files:
        raise InvalidArgumentError("files 不能为空，至少给出一个要暂存的文件")

    # 逐个校验：相对 repo_path 解析后仍须落在仓库内，拦截 ../ 逃逸与仓库外绝对路径
    for file in files:
        target = (path / file).resolve()
        if not target.is_relative_to(path):
            raise InvalidArgumentError(f"{file} 不在仓库 {path} 内")

    repo = await _get_repo(path)
    await asyncio.to_thread(repo.index.add, files)

    staged_files = await asyncio.to_thread(repo.git.diff, "--staged", "--name-only")
    staged_list = [line for line in staged_files.splitlines() if line.strip()]

    msg = f"已把 {len(files)} 个文件加入暂存区。"
    if staged_list:
        msg += "\n暂存区当前文件清单：\n" + "\n".join(staged_list)
    return ToolResult(success=True, content=msg)

@mcp.tool()
@guard
async def git_branches(repo_path: str) -> ToolResult:
    """
    列出指定 git 仓库的本地与远程分支，并标出当前分支（游离 HEAD 时单独说明）。
    Args:
        repo_path: 仓库根目录路径；传 list_repos 给出的绝对路径或相对工作区根
            的相对路径均可。
    """
    path = _resolve_repo_path(repo_path)
    _ensure_known_repo(path)

    repo = await _get_repo(path)

    local_raw = await asyncio.to_thread(repo.git.branch, "--list")
    remote_raw = await asyncio.to_thread(repo.git.branch, "--remotes", "--list")

    def _head_info() -> tuple[str | None, bool]:
        if repo.head.is_detached:
            return None, True
        return repo.active_branch.name, False

    current_name, detached = await asyncio.to_thread(_head_info)

    lines: List[str] = []
    if detached:
        head_commit = await asyncio.to_thread(lambda: repo.head.commit)
        lines.append(f"当前处于游离 HEAD（{head_commit.hexsha[:12]}），未关联任何分支")
    else:
        lines.append(f"当前分支：{current_name}")

    local_names = [line.strip().lstrip("* ").strip() for line in local_raw.splitlines() if line.strip()]
    lines.append("本地分支：")
    for name in local_names:
        lines.append(f"- {name}{'（当前）' if name == current_name else ''}")

    remote_names = [line.strip() for line in remote_raw.splitlines() if line.strip()]
    if remote_names:
        lines.append("远程分支：")
        lines.extend(f"- {name}" for name in remote_names)
    else:
        lines.append("远程分支：（无）")

    return ToolResult(success=True, content="\n".join(lines))


@mcp.tool()
@guard
async def git_switch(repo_path: str, branch: str) -> ToolResult:
    """
    切换指定仓库的当前分支（写操作，需人工审批）。
    目标分支不在本地时，若恰好一个远程有同名跟踪分支会自动创建并切换（git switch 行为）。
    注意：有未提交改动且会被覆盖时 git 会拒绝切换，stderr 会说明原因。
    Args:
        repo_path: 仓库根目录路径；传 list_repos 给出的绝对路径或相对工作区根
            的相对路径均可。
        branch: 要切换到的分支名。
    """
    path = _resolve_repo_path(repo_path)
    _ensure_known_repo(path)

    if not branch or not branch.strip():
        raise InvalidArgumentError("branch 不能为空，给出要切换到的分支名")

    repo = await _get_repo(path)

    try:
        await asyncio.to_thread(repo.git.switch, branch)
    except GitCommandError as exc:
        # 分支不存在 / 本地改动会被覆盖 / 合并未完成等 git 层失败：stderr 回显
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        return ToolResult(success=False, content=f"切换分支失败：{stderr}")

    def _after() -> str:
        if repo.head.is_detached:
            return f"已切换到提交 {repo.head.commit.hexsha[:12]}（游离状态）"
        return f"已切换到分支 {repo.active_branch.name}"

    return ToolResult(success=True, content=await asyncio.to_thread(_after))


# git 提交的合作者尾注：提交信息末尾自动追加一行，标明本次提交由 CodingAgent 协助完成。
CO_AUTHOR_TRAILER = "Co-authored-by: Coding Agent <codingagent@local>"


@mcp.tool()
@guard
async def git_commit(repo_path: str, message: str, all_changes: bool = False) -> ToolResult:
    """
    在指定仓库创建一次 git 提交（写操作，需人工审批）。
    提交身份沿用设备上的 git 配置（仓库级 → 全局 user.name/user.email）；
    提交信息末尾会自动追加 Co-authored-by: Coding Agent 合作者尾注。
    Args:
        repo_path: 仓库根目录路径；传 list_repos 给出的绝对路径或相对工作区根
            的相对路径均可。
        message: 提交信息。
        all_changes: 是否连带提交所有已跟踪文件的修改（对应 git commit -a）。
            注意：不含未跟踪文件；精确提交请先用 git_add 选择文件。
    """
    path = _resolve_repo_path(repo_path)
    _ensure_known_repo(path)

    if not message or not message.strip():
        raise InvalidArgumentError("message 不能为空，至少给出一个提交信息")

    # 末尾追加合作者尾注；已含该尾注则不重复追加
    if CO_AUTHOR_TRAILER.split(":")[0] not in message:
        message = f"{message.rstrip()}\n\n{CO_AUTHOR_TRAILER}"

    repo = await _get_repo(path)

    # 提交前用 porcelain 判定改动范围：暂存区（X 列非空）或已跟踪文件的工作区
    # 修改（Y 列 M/D/T）。用 porcelain 而非 git diff 是避免无 HEAD 的新仓库报错。
    porcelain = await asyncio.to_thread(repo.git.status, "--porcelain")
    lines = [line for line in porcelain.splitlines() if line.strip()]
    has_staged = any(line[0] != " " and line[:2] != "??" for line in lines)
    has_tracked = any(line[1] in "MDT" for line in lines)

    if not (has_staged or (all_changes and has_tracked)):
        scope = "工作区（含暂存区）" if all_changes else "暂存区"
        return ToolResult(success=False, content=f"{scope}没有任何改动，未创建提交。")

    try:
        if all_changes:
            await asyncio.to_thread(repo.git.commit, "-a", "-m", message)
        else:
            await asyncio.to_thread(repo.git.commit, "-m", message)
    except GitCommandError as exc:
        # hook 拒绝 / 锁冲突等 git 层失败：把 stderr 回显给模型，不当内部 bug
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        return ToolResult(success=False, content=f"git 提交失败：{stderr}")

    commit = await asyncio.to_thread(lambda: repo.head.commit)
    return ToolResult(
        success=True,
        content=f"提交成功：{commit.hexsha[:12]} {commit.summary}",
    )  


if __name__ == "__main__":
    mcp.run(transport="stdio")
