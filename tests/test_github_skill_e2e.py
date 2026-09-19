"""`get_skill("github")` 的端到端：**真拉起** `skills/github/server.py` → 17 个工具进本会话 → 真调用。

三块测试的分工（少一块就有一段链路没人验证）：

- `tests/test_github_skill.py`：**进程内**覆盖技能 server 的实现细节（收口 / 截断 / 坏 base64），
  不 spawn、不联网；
- `tests/test_skill_runtime.py`：覆盖加载通道、四道校验与对账；其中唯一真 spawn 的那条用的是
  echo 夹具（无 env、无网络、无凭证）；
- **本文件**补中间那段**真技能**的链路：真 spawn 子进程 → 凭证经 `skill_env` 转发进子进程
  （server 在 **import 期**就要求 `GITHUB_TOKEN`，缺了它进程直接起不来——所以"加载成功"本身
  就是"凭证转发通了"的证明）→ server 真暴露的工具名与技能 `skill.json` 的声明**逐字一致**
  → 工具真能经 shim 取到数据、上游失败真被分类成 `upstream_error` 回传给模型、写工具真发得出 POST。

**不联网**：`GITHUB_API_URL` 与体检端点都指向本进程起的 stdlib HTTP 桩。技能 server 在**子进程**里，进程内
monkeypatch `_request` 打不到它（那正是上面第一条测试走的另一条路），所以只能从"地址"这一侧
给缝。**用真的 `skills/` 源与真的 `skill.json`**——那两样正是要验证的东西，一条都不许假掉。
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

import app.agent.tools as tools_module
import app.config as config
from app.agent import mcp, skills
from app.agent.tools import drop_skill, get_skill
from app.agent.utils import format_tool_result
from app.schema.agent_schema import SkillPreflight

_SESSION = "github-e2e"
_SKILL_JSON = Path(tools_module.__file__).parent.parent.parent / "skills" / "github" / "skill.json"

# 桩返回的仓库元信息：字段与 `github_repo_view` 的 render 一一对应
_REPO_BODY = {
    "full_name": "octo/demo",
    "description": "桩仓库",
    "default_branch": "main",
    "language": "Python",
    "stargazers_count": 42,
    "forks_count": 7,
    "open_issues_count": 3,
    "private": False,
    "pushed_at": "2026-09-13T00:00:00Z",
}


def _expected_github_tools() -> set[str]:
    """技能 `skill.json` 里声明的工具名（真表，不假——2026-09-19 起审批声明随技能走）。"""
    cfg = json.loads(_SKILL_JSON.read_text(encoding="utf-8"))
    return set(cfg.get("tools") or {})


@pytest.fixture
def github_api():
    """本地桩 GitHub API：`/repos/octo/demo` → 200，`/user` → 401，其余 → 404。

    每个分支都对着 `_request` 的一条异常翻译路径（200 正常 / 401 令牌 / 404 不存在），
    所以"上游失败被分类回传"也是端到端验证的，而不是只测了成功路径。
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 —— BaseHTTPRequestHandler 的约定命名
            path = self.path.split("?", 1)[0]
            if path == "/repos/octo/demo":
                self._send(200, json.dumps(_REPO_BODY))
            elif path == "/repos/octo/forbidden":
                self._send(401, '{"message":"Bad credentials"}')
            elif path == "/user":  # 加载时体检打的端点
                self._send(200, json.dumps({"login": "octo-bot", "type": "User"}))
            else:
                self._send(404, '{"message":"Not Found"}')

        def do_POST(self):  # noqa: N802
            """写工具的端点。**桩自己校验 method/路径/请求体**：对不上就回 422。

            这样"POST 真的发出去了、body 真的是工具填的那份"就成了回执成功与否的一部分，
            不必再往夹具外面递请求日志（夹具的返回值是个地址字符串，别为一条断言改它的形状）。
            """
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if path == "/repos/octo/demo/issues/9/comments" and payload.get("body") == "e2e 留言":
                self._send(201, json.dumps({"html_url": "https://example.test/octo/demo/issues/9#c1"}))
            else:
                self._send(422, json.dumps({"message": "unexpected write request"}))

        def _send(self, code: int, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:
            pass  # 别把访问日志打到测试输出里

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_get_skill_github_spawns_real_server_and_serves_tools(github_api, tmp_path, monkeypatch):
    """整条链路一次走完：加载 → 工具进表 → 真调用（成功 / 401 / 404）→ 卸载 → 子进程被收。"""
    mcp._reset_pools_for_tests()
    # GITHUB_TOKEN 走**真 `skill_env`**（白名单 → Settings → 非空校验），只是取值是假 token
    # （桩不校验它，也绝不碰真 GitHub）。
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(
            GITHUB_TOKEN=SecretStr("fake-token"),
            TAVILY_API_KEY=SecretStr(""),  # 空 → 不拉 web_search，少一个子进程
        ),
    )
    # 桩地址得**额外**塞进去：`skill_env` 只转发 `skill.json` 里声明过的键，而 GITHUB_API_URL
    # 声明不得（一声明就变成"必须非空"，默认路径反而加载不上，见 server.py 里那段说明）。
    # 所以这里包一层——真逻辑照跑，只多给一个键。
    real_skill_env = tools_module.skill_env
    monkeypatch.setattr(
        tools_module,
        "skill_env",
        lambda declared: {**real_skill_env(declared), "GITHUB_API_URL": github_api},
    )
    # 加载时体检的端点在 skill.json 里是写死的 `https://api.github.com/user`——这里换成桩地址，
    # 否则这条用例会真打 GitHub（假 token 换回一个真 401，结果还依赖网络）。
    monkeypatch.setattr(
        skills,
        "read_preflight",
        lambda meta: SkillPreflight(url=f"{github_api}/user", bearer_env="GITHUB_TOKEN"),
    )
    workspace = str(tmp_path)

    async def main():
        try:
            await mcp.load_mcp_tool(workspace, _SESSION)  # 构图期的第一步

            out = await get_skill.coroutine(
                name="github",
                state={"session_id": _SESSION, "loaded_skills": []},
                tool_call_id="c1",
                workspace=workspace,
            )
            assert out["loaded_skills"] == ["github"], out["messages"][0].content
            # 加载期体检的结论就在回执里（以前是模型得自己调的 github_auth_status）
            receipt = out["messages"][0].content
            assert "凭证体检" in receipt and "可用" in receipt, receipt

            # ① 技能真暴露的工具名 == skill.json 的声明（§13.3 最后一关"工具名"的端到端版本：
            #    加工具忘了声明、或声明了不存在的工具，都在这里红）
            loaded_names = {n for n in mcp.session_tool_names(workspace, _SESSION) if n.startswith("github_")}
            assert loaded_names == _expected_github_tools()
            assert len(loaded_names) == 17

            tools = {t.name: t for t in mcp.session_tools(workspace, _SESSION)}

            # ② 真调一次：走 shim → owner 会话 → 子进程 → 渲染
            ok = format_tool_result(await tools["github_repo_view"].ainvoke({"owner": "octo", "repo": "demo"}))
            assert "octo/demo" in ok and "Python" in ok
            assert not ok.startswith("["), ok  # 成功不带 error_type 前缀

            # ③ 上游 404：分类成 upstream_error 回传（而不是被 guard 判成内部 bug）
            missing = format_tool_result(
                await tools["github_repo_view"].ainvoke({"owner": "octo", "repo": "nope"})
            )
            assert missing.startswith("[upstream_error] "), missing
            assert "404" in missing

            # ④ 上游 401（令牌无效）：同一条翻译路径，但正文指向"令牌"
            denied = format_tool_result(
                await tools["github_repo_view"].ainvoke({"owner": "octo", "repo": "forbidden"})
            )
            assert denied.startswith("[upstream_error] "), denied
            assert "401" in denied

            # ⑤ 写工具：真发一次 POST。桩只在"路径 + 请求体都对"时才回 201，所以这一条同时
            #    验了"方法真是 POST、body 真是工具拼的那份、回执渲染了响应里的 URL"。
            comment = format_tool_result(
                await tools["github_issue_comment"].ainvoke(
                    {"owner": "octo", "repo": "demo", "number": 9, "body": "e2e 留言"}
                )
            )
            assert not comment.startswith("["), comment
            assert "example.test/octo/demo/issues/9" in comment

            worker_key = (mcp._normalize_workspace(workspace), _SESSION, "skills/github")
            assert worker_key in mcp._WORKERS  # 懒起：首次真调用才建运行体

            await drop_skill.coroutine(
                name="github",
                state={"session_id": _SESSION, "loaded_skills": ["github"]},
                tool_call_id="c2",
                workspace=workspace,
            )
            assert not any(
                n.startswith("github_") for n in mcp.session_tool_names(workspace, _SESSION)
            )
            assert worker_key not in mcp._WORKERS  # 运行体与子进程一起被收掉
        finally:
            # 断言中途失败也要把子进程收干净：它的 cwd 就是 tmp_path，Windows 上留着就删不掉目录。
            # 必须在**同一个 loop** 里关（跨 loop 关闭会炸，见 mcp._reset_pools_for_tests 的说明）。
            await mcp.close_session_pool(workspace, _SESSION)

    asyncio.run(main())
