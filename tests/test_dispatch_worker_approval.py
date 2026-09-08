"""从主 agent 侧验证子 agent 工具审核的完整链路（主 → dispatch → worker 审核 → HTTP → resume → 回主）。

拼接 test_dispatch（主侧编排）与 test_sub_agent_approval（worker 审核回传）为一条从主侧起跑的闭环：
- 入口 = 主图编排节点 OrchestrateNode 执行 dispatch_subtasks（source=dispatch，source=编排层）；
- worker_runner 注入为**真实 run_subtask_impl**（进程内跑真 worker 图，带假 model/假 need_review 工具；
  不真 spawn 子进程、不碰真 LLM/网络），经 env AGENT_INBOX_URL 指向测试的 ApprovalInboxServer；
- 审核方 = 主侧收件箱 + 自动回填 queue.complete（对应 run_tui 的 drain_approvals 判定语义）。

断言：批准 → worker 联网工具真执行、结论回主 agent 汇总；拒绝 → 工具不执行、回灌 [approval_denied]。

不 import mcp_service.file_io → 无需 WORKSPACE_PATH；但 import app.agent.* 触发 get_settings()，
需 .env 存在（CLAUDE.md 前提）。
"""
import asyncio

from langchain_core.messages import AIMessage, ToolMessage

from app.agent.nodes import OrchestrateNode
from app.schema.agent_schema import ToolResult
from app.tui.approval_inbox import ApprovalInboxServer

import app.agent.tools as tools_mod
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
    """跑主侧编排执行 dispatch_subtasks（真 worker runner + 真审批收件箱）。

    返回 (dispatch 输出 dict, 假工具实例)。reviewer_result 决定自动回填 True/False。
    """
    async def go():
        server = ApprovalInboxServer(port=0)
        try:
            port = await server.start()
            monkeypatch.setenv("AGENT_INBOX_URL", f"http://127.0.0.1:{port}")

            model = _WebThenFinalModel()

            async def real_worker(task: str, ws: str) -> str:
                # 真实 worker 图：run_subtask_impl 进程内执行（注入假 model/假工具，不拉真 MCP/LLM）
                tr = await run_subtask_impl(task, ws, model=model, tools=[model.tool])
                return tr.content

            monkeypatch.setattr(tools_mod, "worker_runner", real_worker)

            async def reviewer():
                # 假审核方 = 主侧人审入口（drain_approvals → decide_approval → queue.complete 的语义）
                for _ in range(500):
                    pending = server.queue.pending()
                    if pending:
                        assert pending[0]["payload"]["tool_name"] == "web_search"
                        assert pending[0]["payload"]["worker_id"].startswith("worker-")
                        assert server.queue.complete(pending[0]["approval_id"], reviewer_result)
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("收件箱一直没收到 worker 审批请求")

            rev = asyncio.create_task(reviewer())

            node = OrchestrateNode([tools_mod.dispatch_tool[0]], workspace_path=workspace)
            state = {
                "session_id": "s",
                "messages": [],
                "approved_orchestrate_calls": [
                    {"name": "dispatch_subtasks", "args": {"sub_tasks": ["调研 X 的实现"]}, "id": "cc1"}
                ],
                "current_plan": [],
            }
            out = await node(dict(state))
            await rev
            return out, model.tool
        finally:
            await server.stop()

    return asyncio.run(go())


def test_approved_full_chain_from_main(tmp_path, monkeypatch):
    """批准：主侧派发 → worker 真 interrupt → HTTP 回主收件箱 → 回 True → resume → 工具执行 → 结论回主汇总。"""
    out, tool = _run_main_dispatch(monkeypatch, str(tmp_path), reviewer_result=True)
    content = out["messages"][0].content
    assert out["approved_orchestrate_calls"] == []
    assert tool.calls == 1  # 批准 → 假联网工具真执行一次
    assert FINAL in content  # worker 结论回主 agent 并进汇总正文
    assert "调研 X 的实现" in content


def test_denied_full_chain_from_main(tmp_path, monkeypatch):
    """拒绝：主侧派发 → worker interrupt → 回 False → 工具不执行、回灌 [approval_denied] 并回主汇总。"""
    out, tool = _run_main_dispatch(monkeypatch, str(tmp_path), reviewer_result=False)
    content = out["messages"][0].content
    assert tool.calls == 0  # 拒绝 → 不执行
    assert "[approval_denied]" in content  # 拒绝语义经 worker → dispatch 汇总回到主侧
