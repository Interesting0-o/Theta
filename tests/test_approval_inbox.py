"""app/tui/approval_inbox.py：待审请求队列（ApprovalInbox）+ 薄 HTTP 收件箱的测试。

本模块不 import app.agent → 无需 .env、无需 WORKSPACE_PATH。
HTTP 用 ApprovalInboxServer(start(port=0)) 在测试自己的事件循环里拉起，测试后 stop。
"""
import asyncio

import httpx
import pytest

from app.tui.approval_inbox import ApprovalInbox, ApprovalInboxServer

PAYLOAD = {
    "worker_id": "worker-1",
    "tool_name": "run_command",
    "tool_args": {"command": "rm -rf dist", "description": "清理构建产物"},
    "description": "清理 dist",
}


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
