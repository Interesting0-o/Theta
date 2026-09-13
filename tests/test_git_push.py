"""`mcp_service.git.git_push`：真推一次到**本地裸仓库**（不联网）。

`mcp_service/git.py` 与 `file_io.py` 同族——**import 时**就校验 `WORKSPACE_PATH` 存在，所以跑本
文件要 CLAUDE.md 里那套 env（`WSLENV` + `WORKSPACE_PATH` 指向能包住 pytest tmp 的 Windows 目录）。

覆盖：真推成功（远端真拿到那个提交）/ 地址里的凭据被拒 / 空地址被拒 / 本地没有该分支被拒 /
游离 HEAD 且没给分支被拒 / 非快进被 git 拒绝并回显 / **不设置上游跟踪**（不写 `.git/config`）。

仓库发现走真逻辑，只是把根缩到 tmp_path（`find_git_repos` 的默认参数在 def 期就绑定了进程启动
时的 WORKSPACE_PATH，所以只能这样把它指到临时目录——顺带让用例不与本机 %TEMP% 里的杂物耦合）。
"""
import asyncio
from pathlib import Path

import git
import pytest

import mcp_service.git as git_server


@pytest.fixture
def ws(tmp_path, monkeypatch) -> Path:
    """临时工作区：仓库发现只扫 tmp_path，缓存清空（进程级缓存会跨用例污染）。"""
    real_find = git_server.find_git_repos
    monkeypatch.setattr(git_server, "WORKSPACE_PATH", tmp_path)
    monkeypatch.setattr(git_server, "find_git_repos", lambda root=None: real_find(root or tmp_path))
    git_server._REPOS_PATH_CACHE.clear()
    git_server._REPOS_CACHE.clear()
    yield tmp_path
    git_server._REPOS_PATH_CACHE.clear()
    git_server._REPOS_CACHE.clear()


def _init_repos(root: Path) -> tuple[Path, Path]:
    """造一对：裸仓库 remote.git（推的目标）+ 一个有一条提交的本地仓库 work。"""
    remote = root / "remote.git"
    git.Repo.init(remote, bare=True)
    work = root / "work"
    repo = git.Repo.init(work)
    with repo.config_writer() as cfg:  # 只写这个仓库的 .git/config，不碰全局配置
        cfg.set_value("user", "name", "theta-test")
        cfg.set_value("user", "email", "test@theta.local")
    (work / "a.txt").write_text("hello\n", encoding="utf-8")
    repo.index.add(["a.txt"])
    repo.index.commit("c1")
    repo.git.branch("-M", "main")  # 统一叫 main，免得受本机 init.defaultBranch 影响
    return remote, work


def _push(work: Path, url: str, branch: str = ""):
    return asyncio.run(
        git_server.git_push(repo_path=str(work), url=url, branch=branch)
    )


# ---------------------- 正常路径 ----------------------


def test_push_branch_to_url(ws):
    """没配任何 remote，凭地址就能推；远端真拿到那个提交，且**不写本地配置**。"""
    remote, work = _init_repos(ws)

    out = _push(work, str(remote), "main")

    assert out.success is True, out.content
    assert "已推送分支 main" in out.content
    local_head = git.Repo(work).head.commit.hexsha
    assert git.Repo(remote).commit("main").hexsha == local_head
    # 不设上游：branch.<name>.remote / merge 都不该被写进 .git/config
    assert git.Repo(work).config_reader().has_section('branch "main"') is False


def test_push_current_branch_when_branch_omitted(ws):
    """`branch` 留空 = 推当前分支。"""
    remote, work = _init_repos(ws)

    out = _push(work, str(remote))

    assert out.success is True, out.content
    assert "已推送分支 main" in out.content
    assert "不设置上游跟踪" in out.content  # 这条后果必须如实告诉模型


# ---------------------- 参数与状态被拒 ----------------------


def test_push_rejects_credentials_in_url(ws):
    """地址里带用户名/token → 直接拒（它会进命令行与对话历史），且**一个字节都不推**。"""
    remote, work = _init_repos(ws)

    out = _push(work, "https://user:ghp_secret@github.com/octo/demo.git", "main")

    assert out.success is False
    assert out.error_type == "invalid_argument"
    assert "token" in out.content
    assert not git.Repo(remote).heads  # 远端仍然空着


def test_push_rejects_empty_url(ws):
    """空地址 → 拒，且回执点明"要完整地址、不是 remote 名"。"""
    _, work = _init_repos(ws)

    out = _push(work, "   ", "main")

    assert out.success is False
    assert out.error_type == "invalid_argument"
    assert "完整地址" in out.content


def test_push_refuses_unknown_local_branch(ws):
    """要推的分支本地不存在 → 拒（不给"凭名字凭空推一个"的口子）。"""
    remote, work = _init_repos(ws)

    out = _push(work, str(remote), "feature/nope")

    assert out.success is False
    assert out.error_type == "invalid_argument"
    assert "本地没有分支" in out.content
    assert not git.Repo(remote).heads


def test_push_refuses_detached_head_without_branch(ws):
    """游离 HEAD + 没给分支 → 拒并说明，而不是猜一个分支名推上去。"""
    remote, work = _init_repos(ws)
    git.Repo(work).git.checkout("--detach", "HEAD")

    out = _push(work, str(remote))

    assert out.success is False
    assert out.error_type == "invalid_argument"
    assert "游离 HEAD" in out.content


def test_push_refuses_repo_without_commits(ws):
    """一行提交都还没有的仓库：给明确原因，而不是把 git 的 `src refspec ... does not match any` 丢给模型。"""
    remote = ws / "remote.git"
    git.Repo.init(remote, bare=True)
    work = ws / "empty"
    git.Repo.init(work)

    out = _push(work, str(remote))  # branch 留空 → 走"当前分支"那一条

    assert out.success is False
    assert out.error_type == "invalid_argument"
    assert "还没有任何提交" in out.content
    assert not git.Repo(remote).heads


def test_push_reports_non_fast_forward(ws):
    """非快进（改写过的历史）→ git 拒绝，stderr 原样回显给模型而不是当内部 bug。"""
    remote, work = _init_repos(ws)
    assert _push(work, str(remote), "main").success is True

    repo = git.Repo(work)
    repo.git.commit("--amend", "-m", "c1-rewritten")  # 远端那条提交不再是新提交的祖先

    out = _push(work, str(remote), "main")

    assert out.success is False
    assert "push 失败" in out.content
    assert git.Repo(remote).commit("main").hexsha != repo.head.commit.hexsha  # 远端没被改写
