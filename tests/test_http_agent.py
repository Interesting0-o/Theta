"""mcp_service/http_agent.py 的注册烟囱 + interrupt 回传往返测试。

固化契约：
- 工具表为 {chat, request_approval}（chat 单次 AI 回复；request_approval 模拟 interrupt 回传）；
- 模块 import **不**触发 app.agent.model（chat 内懒加载 get_chat_model）→ 冷启动无需 .env；
- request_approval_impl 与 ApprovalInboxServer(port=0) 同事件循环往返：入队 → 假审核方回填
  决定 → 长轮询取回（批准 / [denied]）；description 空白 → InvalidArgumentError。

真实 HTTP 往返（streamable-http ListTools + CallTool 拿到回复）用冒烟命令人工验证：
    .venv/Scripts/python.exe -m mcp_service.http_agent   # 起服务(默认 127.0.0.1:8000/mcp)
    # 另一进程用 langchain_mcp_adapters MultiServerMCPClient({"http_agent":
    #   {"transport":"streamable_http","url":"http://127.0.0.1:8000/mcp"}}) 调 chat / request_approval
"""
import asyncio
import subprocess
import sys

import pytest

import mcp_service.http_agent as ha
from app.tui.approval_inbox import ApprovalInboxServer
from app.exception import InvalidArgumentError


def _tool_names():
    async def _names():
        res = await ha.mcp.list_tools()
        return [t.name for t in res]

    return asyncio.run(_names())


def test_tools_registered():
    assert sorted(_tool_names()) == ["chat", "request_approval"]


def test_import_does_not_load_model():
    # app.agent.model 顶层 get_settings() 需要 .env；模块 import 期必须不触发它。
    # 进程内断言会被同进程其它用例污染的 sys.modules 干扰，故用子进程隔离验证。
    probe = (
        "import sys; "
        "import mcp_service.http_agent; "
        "print('app.agent.model' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


def test_request_approval_impl_blank_description():
    with pytest.raises(InvalidArgumentError):
        asyncio.run(ha.request_approval_impl("", "http://127.0.0.1:8010"))


def test_request_approval_roundtrip_approved():
    """批准往返：impl POST 入队 → 假审核方回填 True → 长轮询取回"已批准"。"""

    async def go():
        server = ApprovalInboxServer(port=0)
        try:
            port = await server.start()
            base = f"http://127.0.0.1:{port}"

            async def reviewer():
                # 假审核方 = 主侧人审入口：等请求入队后 complete(批准)
                # （对应 main.decide_approval 收 y/n 后 queue.complete 的语义）
                for _ in range(500):
                    pending = server.queue.pending()
                    if pending:
                        assert server.queue.complete(pending[0]["approval_id"], True)
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("收件箱一直没收到请求")

            rev = asyncio.create_task(reviewer())
            res = await ha.request_approval_impl(
                "清理构建产物", base, tool_name="run_command",
                tool_args={"command": "rm -rf dist"},
            )
            await rev
            assert res.success
            assert "已批准" in res.content and "run_command" in res.content
        finally:
            await server.stop()

    asyncio.run(go())


def test_request_approval_roundtrip_denied():
    """拒绝往返：回填 False → 长轮询取回 [denied]，内容提示不执行。"""

    async def go():
        server = ApprovalInboxServer(port=0)
        try:
            port = await server.start()
            base = f"http://127.0.0.1:{port}"

            async def reviewer():
                for _ in range(500):
                    pending = server.queue.pending()
                    if pending:
                        assert server.queue.complete(pending[0]["approval_id"], False)
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("收件箱一直没收到请求")

            rev = asyncio.create_task(reviewer())
            res = await ha.request_approval_impl(
                "清理构建产物", base, tool_name="write_file",
                tool_args={"path": "x.md"},
            )
            await rev
            assert res.success
            assert "[denied]" in res.content and "不执行" in res.content
        finally:
            await server.stop()

    asyncio.run(go())
