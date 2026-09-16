"""终端命令免审的**端到端闭环**：真实图 + 真实 MCP 运行体 + 脚本化模型。

守的是那条链子接通之后的后果——命令解析（`_free_shell_verdict`）→ 审核（ReviewNode 决策）→
  **通过**：run_command 真的交给 terminal server 执行，结果作为 ToolMessage 回到 messages；
  **未通过**：图真的 interrupt 挂起，且命令**一次都没执行**。
单元层（tests/test_review_routing.py）只看决策字符串；"免审"若只免了标签而实际仍挂起，
或"要审"却被静默放行，只有跑真图才暴露得出来。

模型是脚本化的（evaluation.models.ReplayChatModel）：按序吐 AIMessage，不花 token、
不受提示词影响。模型经 get_main_agent_graph 的 `model=` 构造参量注入——评估回放走的就是这条缝。
与 evaluation/ 的分工：那边测模型行为，这边测机制闭环。

前提同 test_mcp_pool：.env 必须存在（import app.agent → graph → model 顶层调 get_settings）。
"""
import asyncio
import subprocess

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import get_main_agent_graph
from app.agent.mcp import _reset_pools_for_tests, close_session_pool
from app.platform.turn import build_turn_state
from evaluation.models import ReplayChatModel


def _tool_call(command: str, call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "run_command",
                "args": {"command": command, "description": "看一眼仓库状态"},
                "id": call_id,
            }
        ],
    )


async def _drive(workspace, session: str, model) -> dict:
    """建真图（真 MCP 运行体）→ 脚本模型驱动一轮 → 返回终态。

    全程在**同一个事件循环**里：运行体的建与关都只能发生在 owner task 里，跨 loop 关闭会炸
    （见 app/agent/mcp.py::_reset_pools_for_tests 注释）。
    """
    _reset_pools_for_tests()
    try:
        graph = await get_main_agent_graph(
            workspace_path=str(workspace), session_id=session, model=model
        )
        compiled = graph.compile(checkpointer=InMemorySaver())
        # 初始 state 用生产入口的同一个助手：手写 dict 会漏 session_id，工具集拿不到会话
        # （表现为"会话 None 没有核心工具缓存"的告警退回），测出来的就不是真实形态了。
        return await compiled.ainvoke(
            build_turn_state(session, "看一下仓库"),
            {"configurable": {"thread_id": session}},
        )
    finally:
        await close_session_pool(str(workspace), session)


def _run(tmp_path, session: str, command: str) -> dict:
    """把模型换成脚本，跑一轮。工作区先 git init，让 `git status` 走成功路径。"""
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)],
        capture_output=True, timeout=60, check=False,
    )
    script = ReplayChatModel(replies=[_tool_call(command), AIMessage(content="做完了")])
    return asyncio.run(_drive(tmp_path, session, script))


def test_readonly_command_runs_without_asking(tmp_path):
    """免审命令：图不挂起，命令**真的被执行**，回执作为 ToolMessage 回到对话。"""
    result = _run(tmp_path, "e2e-free", "git status --porcelain --branch")

    assert "__interrupt__" not in result, "只读命令不该挂起等审批"
    receipts = [
        m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "run_command"
    ]
    assert len(receipts) == 1
    # exit_code 0 = terminal server 真的 spawn 了 git 且它在 git init 过的仓库里跑成功
    assert "exit_code: 0" in receipts[0].content


def test_write_command_parks_the_run_and_executes_nothing(tmp_path):
    """需审批命令：图挂起、payload 带上命令原文，且**一次都没执行**（没有回执）。"""
    command = "git push https://example.com/o/r.git main"
    result = _run(tmp_path, "e2e-review", command)

    interrupts = result.get("__interrupt__")
    assert interrupts, "写命令必须挂起等人工审批"
    payload = interrupts[0].value
    assert payload["tool_name"] == "run_command"
    assert payload["tool_args"]["command"] == command

    # 挂起 = 还没执行：消息里不能出现 run_command 的执行回执
    receipts = [
        m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "run_command"
    ]
    assert receipts == []


@pytest.mark.parametrize(
    "command",
    [
        "git status --porcelain",  # 免审
        "git push https://example.com/o/r.git main",  # 要审
    ],
)
def test_free_and_review_only_differ_by_command_text(tmp_path, command):
    """同一个工具、同一张图：分岔只由命令文本决定（决策点唯一性的回归钉子）。"""
    result = _run(tmp_path, f"e2e-{abs(hash(command))}", command)
    assert ("__interrupt__" in result) is command.startswith("git push")
