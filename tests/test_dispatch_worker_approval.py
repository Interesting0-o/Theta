"""从主 agent 侧验证子 agent 工具审核的完整链路（主 → dispatch → worker 审核 → HTTP → resume → 回主）。

拼接 test_dispatch（主侧派发）与 test_sub_agent_approval（worker 审核回传）为一条从主侧起跑的闭环：
- 入口 = **派发 MCP 工具** `mcp_service/dispatch.py::dispatch_subtasks`（2026-09-21 从
  app/agent/tools.py 搬来：它是普通 MCP 工具、走 ToolNode，不再进编排队列）；
- worker_runner 注入为**真实 run_subtask_impl**（进程内跑真 worker 图，带假 model/假 need_review
  工具；不真 spawn 子进程、不碰真 LLM/网络），经 env AGENT_INBOX_URL 指向测试的 ApprovalInboxServer；
- 审核方 = 主侧收件箱 + 自动回填 queue.complete（对应 run_tui 的 drain_approvals 判定语义）。

断言：批准 → worker 联网工具真执行、结论回主汇总；拒绝 → 工具不执行、回灌 [approval_denied]。

不 import mcp_service.file_io → 无需 WORKSPACE_PATH env；工作区在**调用期**由 env 提供，测试里
monkeypatch 到 tmp_path。
"""
import asyncio

from langchain_core.messages import AIMessage, ToolMessage

import mcp_service.dispatch as dispatch_mod
from app.platform.approvals import ApprovalInboxServer
from app.schema.agent_schema import ToolResult
from app.schema.approval_schema import Decision
from mcp_service.sub_agent import run_subtask_impl

FINAL = "结论文本：调研完成"


class _FakeWebTool:
    """假联网工具：name 匹配 tool.json 的 web_search（need_review:true）。"""

    name = "web_search"

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, args: dict):
        self.calls += 1
        return ToolResult(success=True, content="（假联网回执）")


class _WebThenFinalModel:
    """假模型：尚未看到 ToolMessage → 发一次 web_search；已看到 → 给最终答复。"""

    def __init__(self) -> None:
        self.tool = _FakeWebTool()

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        if any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content=FINAL)
        return AIMessage(
            content="",
            tool_calls=[
                {"name": self.tool.name, "args": {"query": "调研目标"}, "id": "call_1", "type": "tool_call"}
            ],
        )


def _run_main_dispatch(monkeypatch, workspace, reviewer_result: bool):
    """跑主侧派发（真 worker runner + 真审批收件箱）。

    返回 (dispatch 的 ToolResult, 假工具实例)。reviewer_result 决定自动回填 True/False。
    """
    async def go():
        server = ApprovalInboxServer(port=0)
        try:
            port = await server.start()
            monkeypatch.setenv("AGENT_INBOX_URL", f"http://127.0.0.1:{port}")
            monkeypatch.setenv("WORKSPACE_PATH", workspace)

            model = _WebThenFinalModel()

            async def real_worker(task: str, ws: str) -> str:
                # 真实 worker 图：run_subtask_impl 进程内执行（注入假 model/假工具，不拉真 MCP/LLM）
                tr = await run_subtask_impl(task, ws, model=model, tools=[model.tool])
                return tr.content

            monkeypatch.setattr(dispatch_mod, "worker_runner", real_worker)

            async def reviewer():
                # 假审核方 = 主侧人审入口（drain_approvals → decide_approval → queue.complete 的语义）
                for _ in range(500):
                    pending = server.queue.pending()
                    if pending:
                        assert pending[0]["payload"]["tool_name"] == "web_search"
                        assert pending[0]["payload"]["worker_id"].startswith("worker-")
                        assert pending[0]["payload"]["subtask"] == "调研 X 的实现"
                        assert server.queue.complete(
                            pending[0]["approval_id"],
                            Decision(kind="approval", approved=reviewer_result),
                        )
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("收件箱一直没收到 worker 审批请求")

            rev = asyncio.create_task(reviewer())
            out = await dispatch_mod.dispatch_subtasks(["调研 X 的实现"])
            await rev
            return out, model.tool
        finally:
            await server.stop()

    return asyncio.run(go())


def test_approved_full_chain_from_main(tmp_path, monkeypatch):
    """批准：主侧派发 → worker 真 interrupt → HTTP 回主收件箱 → 回 True → resume → 工具执行 → 结论回主汇总。"""
    out, tool = _run_main_dispatch(monkeypatch, str(tmp_path), reviewer_result=True)
    assert isinstance(out, ToolResult) and out.success
    assert tool.calls == 1  # 批准 → 假联网工具真执行一次
    assert FINAL in out.content  # worker 结论回主 agent 并进汇总正文
    assert "调研 X 的实现" in out.content


def test_denied_full_chain_from_main(tmp_path, monkeypatch):
    """拒绝：主侧派发 → worker interrupt → 回 False → 工具不执行、回灌 [approval_denied] 并回主汇总。"""
    out, tool = _run_main_dispatch(monkeypatch, str(tmp_path), reviewer_result=False)
    assert isinstance(out, ToolResult) and out.success  # 派发本身成功，失败的是那一条被拒的调用
    assert tool.calls == 0  # 拒绝 → 不执行
    assert "[approval_denied]" in out.content  # 拒绝语义经 worker → dispatch 汇总回到主侧
