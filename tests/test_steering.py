"""运行中转向（消息队列）的**图侧机制闭环**：真图 + 真实 MCP 运行体 + 带钩子的脚本模型。

守 docs/TODO.md「运行中转向」定案里靠图自身成立的那几条契约：

1. **入口跳过**：信箱有货时 `llm_node` 该拍**零模型调用、零读盘**（画像/记忆/技能都不碰，
   peek 是 `__call__` 第一条语句）——返回 `is_inject=True` 后由既有 `route_after_llm` 收口；
2. **收口回合的消息尾部形状**：末条是 ToolMessage、无新增 AIMessage——这是基座呈现分流
   （`is_inject` → 专门提示，不把 ToolMessage 当答复）所依赖的契约；
3. **在途批次照常执行**（已过人批的跑完才收口）；**在途 ask_user 照常弹出、回答照常兑现**
   （钉成"刻意如此"，免得将来有人当 bug 修）；
4. **mailbox=None 时行为与从前完全一致**（worker / evaluation / 未接线的调用方）。

基座侧（race 第三路 / 信箱续轮 / 连锁空转 / 切会话丢弃）在 tests/test_platform_loop.py。

模型是带钩子的脚本（HookModel）：某拍生成前先执行 side effect（往信箱投行，模拟"用户在
模型思考/工具执行期间打字"）。模型经 get_main_agent_graph 的 `model=` 构造参量注入。
前提同 test_command_review_e2e：.env 必须存在（import app.agent → graph → model 顶层调
get_settings）。
"""
import asyncio
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import app.agent.nodes as nodes_module
from app.agent.graph import get_main_agent_graph
from app.agent.mcp import _reset_pools_for_tests, close_session_pool
from app.platform.runtime import UserMailbox
from app.platform.turn import build_turn_state


class HookModel(BaseChatModel):
    """脚本模型：某拍生成前先执行一次 `hook`（side effect），再按序吐 AIMessage。

    与 evaluation.models.ReplayChatModel 同款约定：`bind_tools` 返回自身、尾条粘住。
    """

    replies: list[AIMessage]
    hook: Any = None  # 一次性 callable：在下一拍 `_generate` 开头执行
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "hook-replay"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # noqa: ARG002
        self.calls += 1
        if self.hook is not None:
            effect, self.hook = self.hook, None  # 只执行一次
            effect()
        reply = self.replies[0] if len(self.replies) == 1 else self.replies.pop(0)
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools, **kwargs):  # noqa: ARG002 —— 只认脚本，不必真绑定
        return self


def _spy_disk_io(monkeypatch) -> dict[str, int]:
    """给 nodes 命名空间里的两处读盘（记忆 / 技能正文）装计数器。

    LLMNode 经模块级名字调用它们，patch `app.agent.nodes` 的绑定即命中。收口拍**一次都不
    该被调**——防止将来有人把 peek 挪到系统提示装配之后而不被发现（零模型调用 ≠ 零成本）。
    """
    counts = {"memory": 0, "skills": 0}
    real_memory_block = nodes_module.memory_block
    real_skills_block = nodes_module.skills_block

    def counting_memory_block(workspace_path):
        counts["memory"] += 1
        return real_memory_block(workspace_path)

    def counting_skills_block(names):
        counts["skills"] += 1
        return real_skills_block(names)

    monkeypatch.setattr(nodes_module, "memory_block", counting_memory_block)
    monkeypatch.setattr(nodes_module, "skills_block", counting_skills_block)
    return counts


def _list_dir_call(call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "list_dir", "args": {"path": "."}, "id": call_id}],
    )


def _ask_call() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "ask_user",
                "args": {
                    "why": "方向不明，猜错要重做",
                    "question": "按哪个方向继续？",
                    "options": ["继续A", "转B"],
                },
                "id": "call_1",
            }
        ],
    )


async def _drive(workspace, session: str, model, mailbox, input_text: str) -> dict:
    """建真图（mailbox 接线）→ 脚本模型驱动一轮 → 返回终态。

    全程同一个事件循环（运行体的建与关只能发生在 owner task 里，见 mcp.py）。
    """
    _reset_pools_for_tests()
    try:
        graph = await get_main_agent_graph(
            workspace_path=str(workspace), session_id=session, model=model, mailbox=mailbox
        )
        compiled = graph.compile(checkpointer=InMemorySaver())
        return await compiled.ainvoke(
            build_turn_state(session, input_text),
            {"configurable": {"thread_id": session}},
        )
    finally:
        await close_session_pool(str(workspace), session)


def test_steering_skips_llm_and_seals_turn(tmp_path, monkeypatch):
    """信箱在第一拍之后有货：第二拍整体跳过（零模型调用、零读盘），收口且尾部是 ToolMessage。"""
    counts = _spy_disk_io(monkeypatch)
    mailbox = UserMailbox()
    # 第一拍生成**期间**用户敲入转向行（模拟真基座的 race 投递）；第二拍若没被跳过就会吃掉
    # 第二条脚本答复——用 calls==1 钉住"跳过确实发生了"。
    model = HookModel(
        replies=[_list_dir_call(), AIMessage(content="这条不该被消费")],
        hook=lambda: mailbox.put("改走 B"),
    )

    result = asyncio.run(_drive(tmp_path, "steer-skip", model, mailbox, "先按 A 干"))

    assert result["is_inject"] is True
    assert model.calls == 1, "收口拍必须整体跳过（零模型调用）"
    assert counts == {"memory": 1, "skills": 0}, "收口拍零读盘：首拍读一次记忆，之后不再碰"

    # 收口回合的消息尾部形状（基座呈现分流依赖的契约）：无新增 AIMessage，末条是在途批回执
    messages = result["messages"]
    assert [type(m).__name__ for m in messages] == ["HumanMessage", "AIMessage", "ToolMessage"]
    assert isinstance(messages[-1], ToolMessage) and messages[-1].name == "list_dir"
    assert messages[-1].content, "在途批必须真的执行完（免审读工具、真回执）才收口"


def test_steering_with_inflight_ask_user_still_asks(tmp_path):
    """在途批次里混着 ask_user 时转向：提问面板照常弹、回答照常兑现，然后下一边界收口。

    这是"在途的照常跑完"语义对**提问面板**的推广——不破语义，但要钉成刻意行为，
    免得将来有人把它当 bug 修掉。
    """
    mailbox = UserMailbox()
    model = HookModel(replies=[_ask_call(), AIMessage(content="这条不该被消费")])

    async def scenario() -> dict:
        _reset_pools_for_tests()
        try:
            graph = await get_main_agent_graph(
                workspace_path=str(tmp_path), session_id="steer-ask", model=model, mailbox=mailbox
            )
            compiled = graph.compile(checkpointer=InMemorySaver())
            config = {"configurable": {"thread_id": "steer-ask"}}
            parked = await compiled.ainvoke(
                build_turn_state("steer-ask", "先问清楚再干"), config
            )
            assert parked.get("__interrupt__"), "ask_user 必须挂起等回答"
            # 面板期间用户敲了转向行（真基座里由 race 投递；测试直接塞，等价时序）
            mailbox.put("其实直接转 B")
            return await compiled.ainvoke(
                Command(resume={"option_indexes": [0], "supplement": None}), config
            )
        finally:
            await close_session_pool(str(tmp_path), "steer-ask")

    result = asyncio.run(scenario())

    assert result["is_inject"] is True
    assert model.calls == 1, "提问兑现后进入收口拍，不再调模型"
    receipts = [
        m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "ask_user"
    ]
    assert len(receipts) == 1, "提问照常弹、回答照常兑现（不因转向而跳过）"
    assert "继续A" in receipts[0].content  # 回执含用户选项（[user_answer] 格式）


def test_without_mailbox_no_skip(tmp_path):
    """mailbox=None（worker / evaluation / 未接线调用方的形态）：无跳过，行为与从前一致。"""
    model = HookModel(replies=[_list_dir_call(), AIMessage(content="做完了")])

    result = asyncio.run(_drive(tmp_path, "steer-none", model, None, "看一下目录"))

    assert not result.get("is_inject")
    assert model.calls == 2  # 两拍都真调了模型
    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].content == "做完了"
