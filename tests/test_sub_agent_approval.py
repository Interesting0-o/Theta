"""worker 审核回传闭环测试：worker 图里 need_review 工具的 interrupt 真经 HTTP 走到主侧 broker。

固化契约（docs/MULTI_AGENT.md §6 回传通道 / §9）：
- worker 复用主图 QueueNode/ReviewNode；调 need_review 工具（如 web_search）→ ReviewNode
  interrupt() → driver 把中断 POST 进 ApprovalInboxServer → 假审核方 queue.complete 回填 →
  长轮询取回决定 → Command(resume) 续跑：
    * 批准 → 该工具真被执行一次、run_subtask 返回最终结论；
    * 拒绝 → 工具不执行、结论正文带 [approval_denied]（ReviewNode 原样兑现悬空 tool_call）。
- 只读工具（need_review:false）不 interrupt、不发起任何 HTTP（inbox 用关闭端口 fail-fast）。
- 不拉真实 MCP 孙进程 / 不碰真实 LLM（注入假 model/假工具）；无需 WORKSPACE_PATH。
- 运行本文件要求 .env 存在（run_subtask_impl 内部懒加载触发 get_settings，见 test_sub_agent）。
"""
import asyncio

from langchain_core.messages import AIMessage, ToolMessage

from app.schema.agent_schema import ToolResult
from app.platform.approvals import ApprovalInboxServer
from mcp_service.sub_agent import run_subtask_impl

FINAL = "结论：联网调研完成"

# 关闭端口：仅用于断言"不该联网时绝不联网"（若有 interrupt 会立刻 ConnectionRefused 失败）
_CLOSED_URL = "http://127.0.0.1:1"


class _FakeTool:
    """假工具：name 匹配 tool.json（web_search / read_file），记录被调用次数。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def ainvoke(self, args: dict):
        self.calls += 1
        return ToolResult(success=True, content="（假工具回执）")


class _ToolThenFinalModel:
    """假模型：尚未看到 ToolMessage → 发一次 self.tool 的调用；已看到 → 给最终答复。"""

    def __init__(self, tool: _FakeTool) -> None:
        self.tool = tool

    def bind_tools(self, tools):
        return self  # worker 图会 bind_tools；直接返回自身

    async def ainvoke(self, messages):
        if any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content=FINAL)
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": self.tool.name,
                    "args": {"query": "调研目标"},
                    "id": "call_1",
                    "type": "tool_call",
                }
            ],
        )


def _run(coro):
    return asyncio.run(coro)


def test_approved_closes_chain(tmp_path):
    """批准闭合：worker 中断 POST 入队 → 主侧回 True → resume → web 工具执行一次 → 返回结论。"""

    async def go():
        server = ApprovalInboxServer(port=0)
        try:
            port = await server.start()
            base = f"http://127.0.0.1:{port}"

            async def reviewer():
                # 假审核方 = 主侧人审入口（decide_approval 收 y/n 后 queue.complete 的语义）
                for _ in range(500):
                    pending = server.queue.pending()
                    if pending:
                        assert pending[0]["payload"]["tool_name"] == "web_search"
                        # 子任务原文随审批一起到主侧：人审时才有"这个 worker 在做什么"的上下文
                        assert pending[0]["payload"]["subtask"] == "查 x"
                        assert server.queue.complete(pending[0]["approval_id"], True)
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("收件箱一直没收到 worker 审批请求")

            rev = asyncio.create_task(reviewer())
            tool = _FakeTool("web_search")
            res = await run_subtask_impl(
                "查 x", str(tmp_path), model=_ToolThenFinalModel(tool), tools=[tool], inbox_url=base
            )
            await rev
            assert res.success is True
            assert tool.calls == 1  # 批准 → 假 web 工具真被执行一次
            assert res.content == FINAL
        finally:
            await server.stop()

    _run(go())


def test_denied_closes_chain(tmp_path):
    """拒绝闭合：主侧回 False → 工具不执行、结论正文带 [approval_denied]。"""

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
                raise AssertionError("收件箱一直没收到 worker 审批请求")

            rev = asyncio.create_task(reviewer())
            tool = _FakeTool("web_search")
            res = await run_subtask_impl(
                "查 x", str(tmp_path), model=_ToolThenFinalModel(tool), tools=[tool], inbox_url=base
            )
            await rev
            assert res.success is True
            assert tool.calls == 0  # 拒绝 → 不执行
            assert "[approval_denied]" in res.content  # ReviewNode 拒绝语义回灌
        finally:
            await server.stop()

    _run(go())


def test_read_only_tool_never_interrupts(tmp_path):
    """休眠回归：只读工具(need_review:false)全程不 interrupt、不发 HTTP，正常执行返回结论。"""

    async def go():
        tool = _FakeTool("read_file")
        # inbox 指向关闭端口：若错误 interrupt 走 HTTP → 立刻 ConnectionRefused 失败
        res = await run_subtask_impl(
            "读 a", str(tmp_path), model=_ToolThenFinalModel(tool), tools=[tool], inbox_url=_CLOSED_URL
        )
        assert res.success is True
        assert tool.calls == 1  # 只读工具照常执行
        assert res.content == FINAL

    _run(go())
