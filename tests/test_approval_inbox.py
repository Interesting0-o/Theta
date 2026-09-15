"""app/platform/approvals.py：待审请求队列（ApprovalInbox）+ 薄 HTTP 收件箱的测试。

本模块不 import app.agent → 无需 .env、无需 WORKSPACE_PATH。
HTTP 用 ApprovalInboxServer(start(port=0)) 在测试自己的事件循环里拉起，测试后 stop。
"""
import asyncio
import socket

import httpx
import pytest

from app.exception import ConfigError
from app.platform.approvals import ApprovalInbox, ApprovalInboxServer
from app.schema.approval_schema import DEFAULT_INBOX_HOST, DEFAULT_INBOX_PORT

PAYLOAD = {
    "worker_id": "worker-1",
    "tool_name": "run_command",
    "tool_args": {"command": "rm -rf dist", "description": "清理构建产物"},
    "description": "清理 dist",
}


def test_default_inbox_port_is_shared_constant(monkeypatch):
    """默认端口是主侧/worker **共用的一份常量**（两边各写字面量迟早会漂），且 env 仍可覆盖。"""
    monkeypatch.delenv("AGENT_INBOX_PORT", raising=False)
    assert ApprovalInboxServer().port == DEFAULT_INBOX_PORT

    monkeypatch.setenv("AGENT_INBOX_PORT", "12345")
    assert ApprovalInboxServer().port == 12345


def test_worker_side_default_url_uses_same_constant():
    """worker 回传地址的默认值也来自那份常量——主侧换端口时才只有一处要改。"""
    from mcp_service.sub_agent import _INBOX_DEFAULT_URL

    assert _INBOX_DEFAULT_URL == f"http://{DEFAULT_INBOX_HOST}:{DEFAULT_INBOX_PORT}"


def test_start_reports_busy_port_as_config_error():
    """端口被占 → 带地址与解法提示的 ConfigError（**而不是 `SystemExit: 1` 把进程带走**）。

    用真占住的端口来测：预检在起 uvicorn 之前就拦住，所以既确定、也不会有 SystemExit 逃出循环。
    """

    async def go():
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            server = ApprovalInboxServer(port=port)
            with pytest.raises(ConfigError) as err:
                await server.start()
            message = str(err.value)
            assert str(port) in message and "AGENT_INBOX_PORT" in message
            await server.stop()  # 没起成也要能安全收尾（清理路径不抛）
        finally:
            blocker.close()

    asyncio.run(go())


def test_worker_http_timeout_strictly_exceeds_server_block():
    """客户端 HTTP 超时必须 **严格大于** 主侧单次 block。

    主侧攥着连接最多 `INBOX_BLOCK_SECONDS` 才回 `{"status":"pending"}`；客户端若等得比它短，
    人还在思考时 worker 就先 ReadTimeout——那个异常在 driver 的循环里没被接，会让整条子任务
    失败，而人的决定变成无人领取（2026-09-15 审计发现：原先写死 60s < 主侧的 120s）。
    """
    from app.schema.approval_schema import INBOX_BLOCK_SECONDS
    from mcp_service.sub_agent import _APPROVAL_TIMEOUT_S, _HTTP_TIMEOUT_S

    assert _HTTP_TIMEOUT_S > INBOX_BLOCK_SECONDS
    # 单条决定的总等待上限还要留出重试的余地
    assert _APPROVAL_TIMEOUT_S > _HTTP_TIMEOUT_S


def test_decided_records_are_bounded_but_recent_stay_queryable():
    """已回填记录要有界回收，但**最近那些仍可查**（worker 拿到决定后可能再查一次状态）。"""
    from app.platform.approvals import _DECIDED_HISTORY

    async def go():
        queue = ApprovalInbox()
        ids = [queue.enqueue(PAYLOAD) for _ in range(_DECIDED_HISTORY + 10)]
        for approval_id in ids:
            queue.complete(approval_id, True)

        assert queue.status(ids[-1]) == {"status": "decided", "approved": True}
        assert queue.status(ids[0]) is None  # 更早的已回收——无界增长被挡住
        assert queue.pending() == []

    asyncio.run(go())


def test_deny_pending_rejects_everything_undecided():
    """收尾时未决请求一律按拒绝回填：远端 worker 不必白等到超时（且不会把没人应答记成网络错误）。"""

    async def go():
        queue = ApprovalInbox()
        first, second, decided = (queue.enqueue(PAYLOAD) for _ in range(3))
        queue.complete(decided, True)

        assert queue.deny_pending() == 2
        assert queue.status(first) == {"status": "decided", "approved": False}
        assert queue.status(second)["approved"] is False
        assert queue.status(decided)["approved"] is True  # 已决定的不被改动
        assert queue.deny_pending() == 0  # 幂等

    asyncio.run(go())


def test_queue_enqueue_pending_complete_wait():
    async def go():
        queue = ApprovalInbox()
        assert queue.pending() == []
        approval_id = queue.enqueue(PAYLOAD)
        assert queue.new_pending.is_set()
        assert [p["approval_id"] for p in queue.pending()] == [approval_id]

        # wait 阻塞到 complete
        waiter = asyncio.ensure_future(queue.wait(approval_id))
        await asyncio.sleep(0)
        assert not waiter.done()

        assert queue.complete(approval_id, True) is True
        assert (await waiter) is True
        assert queue.status(approval_id) == {"status": "decided", "approved": True}

        # 幂等：已回填再 complete 无效；未知 id 也无效
        assert queue.complete(approval_id, False) is False
        assert queue.complete("nope", True) is False

        # 已回填后 wait 立即返回
        assert await queue.wait(approval_id) is True

    asyncio.run(go())


def test_queue_wait_timeout():
    async def go():
        queue = ApprovalInbox()
        approval_id = queue.enqueue(PAYLOAD)
        with pytest.raises(asyncio.TimeoutError):
            await queue.wait(approval_id, timeout=0.05)
        assert queue.status(approval_id)["status"] == "pending"

    asyncio.run(go())


def test_queue_clear_new():
    async def go():
        queue = ApprovalInbox()
        queue.enqueue(PAYLOAD)
        assert queue.new_pending.is_set()
        queue.clear_new()
        assert not queue.new_pending.is_set()

    asyncio.run(go())


def test_http_roundtrip_enqueue_approve():
    async def go():
        server = ApprovalInboxServer(port=0)
        try:
            port = await server.start()
            assert port > 0
            base = f"http://127.0.0.1:{port}"
            async with httpx.AsyncClient(base_url=base) as client:
                # worker POST 入队 → 拿到 id
                r = await client.post("/requests", json=PAYLOAD)
                assert r.status_code == 200
                body = r.json()
                approval_id = body["approval_id"]
                assert body["status"] == "pending"

                # 立即查 → pending
                s = await client.get(f"/requests/{approval_id}")
                assert s.json() == {"status": "pending", "approved": None}

                # 长轮询等回填（模拟 worker 阻塞等待）
                async def block_wait():
                    rr = await client.get(f"/requests/{approval_id}?block=1", timeout=10)
                    return rr.json()

                waiter = asyncio.create_task(block_wait())
                await asyncio.sleep(0.1)  # 确保 worker 已挂起

                # 审核方回填（决定由审核节点/人审入口给出）
                d = await client.post(
                    f"/requests/{approval_id}/decision", json={"approved": True}
                )
                assert d.status_code == 200

                got = await asyncio.wait_for(waiter, timeout=5)
                assert got == {"status": "decided", "approved": True}
        finally:
            await server.stop()

    asyncio.run(go())


def test_http_rejects_missing_fields():
    async def go():
        server = ApprovalInboxServer(port=0)
        try:
            port = await server.start()
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                r = await client.post("/requests", json={"worker_id": "w1"})
                assert r.status_code == 400
                assert "tool_name" in r.json()["error"]
                r2 = await client.post("/requests", json={"tool_name": "run_command"})
                assert r2.status_code == 400
        finally:
            await server.stop()

    asyncio.run(go())


def test_http_unknown_request_404():
    async def go():
        server = ApprovalInboxServer(port=0)
        try:
            port = await server.start()
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                assert (await client.get("/requests/nope")).status_code == 404
        finally:
            await server.stop()

    asyncio.run(go())
