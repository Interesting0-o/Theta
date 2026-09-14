"""`mcp_service.git.git_clone`：真克隆一个**本地裸仓库**（不联网），以及它的沙箱边界。

与 `tests/test_git_push.py` 同族（同一套 env 前提，见那里与 CLAUDE.md）。这里守两件事：
① 克隆真能跑通、且新仓库立刻被 `list_repos` 认得；② **目标目录不许跑出工作区**——clone 是
git.py 里第一个"创建新目录"的工具，其它工具都靠 `_ensure_known_repo` 兜着，这条没有就是一条
"往工作区外写文件"的通路。
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


def _make_remote(root: Path, *, with_commit: bool = True) -> Path:
    """造一个裸仓库当"远端"；默认带一条提交（不带的那个用来验空仓库那条分支）。"""
    remote = root / "remote.git"
    git.Repo.init(remote, bare=True)
    if not with_commit:
        return remote

    seed = root / "_seed"
    repo = git.Repo.init(seed)
    with repo.config_writer() as cfg:
        cfg.set_value("user", "name", "theta-test")
        cfg.set_value("user", "email", "test@theta.local")
    (seed / "a.txt").write_text("hello\n", encoding="utf-8")
    repo.index.add(["a.txt"])
    repo.index.commit("c1")
    repo.git.branch("-M", "main")
    repo.git.push(str(remote), "main:main")
    # 裸仓库 `git init` 的 HEAD 默认指向 master；不指到 main 的话 `git clone` 会成功却**不检出**
    # 任何文件（远端 HEAD 指向不存在的分支），克隆出来的工作树是空的。
    git.Repo(remote).git.symbolic_ref("HEAD", "refs/heads/main")
    return remote


def _clone(url: str, dest: str = ""):
    return asyncio.run(git_server.git_clone(url=url, dest=dest))


# ---------------------- 正常路径 ----------------------


def test_clone_pulls_repo_and_refreshes_discovery(ws):
    """克隆成功 → 目录真的在那里、回执给分支/提交、且 `list_repos` 立刻认得它。"""
    remote = _make_remote(ws)

    out = _clone(str(remote))

    assert out.success is True, out.content
    target = ws / "remote"
    assert (target / "a.txt").read_text(encoding="utf-8") == "hello\n"
    assert "main" in out.content
    # 新仓库要被"已知仓库"清单认得——否则 list_repos / git_status 全用它查仓库会说不认识
    assert target.resolve() in git_server._get_repos()


def test_clone_uses_dest_when_given(ws):
    """显式 dest → 克隆到那个（工作区内的）目录。"""
    remote = _make_remote(ws)

    out = _clone(str(remote), dest="vendor/demo")

    assert out.success is True, out.content
    assert (ws / "vendor" / "demo" / "a.txt").is_file()


def test_clone_with_unset_remote_head_says_which_branches(ws):
    """远端 HEAD 指向不存在的分支（裸仓库没设默认分支）→ **不许**谎报"空仓库"。

    这时 `git clone` 会成功却**不检出**任何文件，只在 stderr 丢一句 warning；回执若说"远端还没有
    任何提交"，模型会以为白克隆了——而对象其实都在，切个分支就能用。（这条是写夹具时真踩到的：
    裸仓库 `git init` 的 HEAD 指向 master，而分支叫 main。）
    """
    remote = _make_remote(ws)
    git.Repo(remote).git.symbolic_ref("HEAD", "refs/heads/不存在")

    out = _clone(str(remote))

    assert out.success is True, out.content
    assert "空仓库" not in out.content
    assert "main" in out.content  # 告诉它能切到哪个分支
    assert (ws / "remote" / ".git").is_dir()


def test_clone_empty_remote_is_reported(ws):
    """远端还没有提交 → 克隆成功但说清"这是个空仓库"，而不是抛内部错误。"""
    remote = _make_remote(ws, with_commit=False)

    out = _clone(str(remote))

    assert out.success is True, out.content
    assert "空仓库" in out.content


# ---------------------- 沙箱与参数边界 ----------------------


def test_clone_refuses_target_outside_workspace(ws):
    """目标目录跑出工作区 → 拒（clone 是唯一会"创建新路径"的 git 工具，必须自查）。"""
    remote = _make_remote(ws)

    out = _clone(str(remote), dest="../跑出去")

    assert out.success is False
    assert out.error_type == "invalid_argument"
    assert "工作区" in out.content
    assert not (ws.parent / "跑出去").exists()


def test_clone_refuses_non_empty_target(ws):
    """目标已存在且非空 → 拒（不覆盖别人的东西），且一个字节都没动。"""
    remote = _make_remote(ws)
    (ws / "remote").mkdir()
    (ws / "remote" / "keep.txt").write_text("别动我", encoding="utf-8")

    out = _clone(str(remote))

    assert out.success is False
    assert "非空" in out.content
    assert (ws / "remote" / "keep.txt").read_text(encoding="utf-8") == "别动我"


def test_clone_rejects_empty_and_credential_urls(ws):
    """空地址 / 带凭据的地址都在动手之前拒掉（后者会进命令行与对话历史）。"""
    assert _clone("   ").error_type == "invalid_argument"

    remote = _make_remote(ws)
    out = _clone(f"https://user:ghp_secret@{remote}")

    assert out.success is False
    assert out.error_type == "invalid_argument"
    assert "token" in out.content
    assert not any(p.name.startswith("remote") and p.is_dir() for p in ws.iterdir() if p.name != "remote.git")
