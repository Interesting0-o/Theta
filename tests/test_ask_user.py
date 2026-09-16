"""agent 提问机制（闸门 type=ask_user）的机制测试：闸门分流 + 工具体 + 端到端闭环。

分工与本仓库既有约定一致：
- **闸门/解析**：monkeypatch `app.agent.nodes.interrupt`，不起真图、不花钱；
- **工具体**：直接调 `ask_user.func`（编排工具的执行器就是这么调底层函数的）；
- **端到端**：真图 + 真 MCP 运行体 + 脚本化模型，验"挂起 → 两段回答 → ToolMessage 回模型"闭环。

前提同 test_command_review_e2e：.env 必须存在（import app.agent.graph → model 顶层调 get_settings）。
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import app.agent.nodes as nodes_module
from app.agent.nodes import _MAX_ASK_OPTIONS, ReviewNode, _ask_request, _normalize_ask_answer
from app.agent.tools import ask_user, worker_tools
from app.schema.approval_schema import GATE_ASK_USER
from evaluation.models import ReplayChatModel

# ------------------------- 夹具 -------------------------


def _ask_call(question="改哪个？", options=None, why="两个同名函数", call_id="call_ask"):
    return {
        "name": "ask_user",
        "args": {
            "why": why,
            "question": question,
            "options": options if options is not None else ["a.py 里的", "c.py 里的"],
        },
        "id": call_id,
    }


def _state(pending):
    return {
        "pending_tool_calls": pending,
        "approved_tool_calls": [],
        "approved_orchestrate_calls": [],
    }


def _run(node, state):
    return asyncio.run(node(state))


@pytest.fixture
def captured_interrupts(monkeypatch):
    """捕获闸门发出的 interrupt payload，并让 interrupt 返回给定回答。"""

    def install(answer):
        seen: list[dict] = []
        monkeypatch.setattr(
            nodes_module, "interrupt", lambda payload: seen.append(payload) or answer
        )
        return seen

    return install


# ------------------------- 闸门的拦截与分流 -------------------------


def test_ask_call_parks_the_gate_with_question_and_options(captured_interrupts):
    """source=ask 的调用要**在闸门处挂起**，payload 带齐 why/question/options 交前端。"""
    seen = captured_interrupts({"option_index": 1, "supplement": "别动 b.py"})

    out = _run(ReviewNode(), _state([_ask_call()]))

    assert len(seen) == 1
    assert seen[0]["type"] == GATE_ASK_USER
    assert seen[0]["question"] == "改哪个？"
    assert seen[0]["options"] == ["a.py 里的", "c.py 里的"]
    assert seen[0]["why"] == "两个同名函数"
    assert seen[0]["tool_call_id"] == "call_ask"
    # 提问面板不需要审批那套遗留字段
    assert "current_step" not in seen[0] and "tool_name" not in seen[0]
    # 闸门自己**不产生**任何消息：回答由下游工具体渲染成回执
    assert out.get("messages") is None


def test_ask_call_is_routed_to_orchestrate_queue(captured_interrupts):
    """回答拿到后调用照常进**编排队列**（由 OrchestrateNode 执行工具体）——路由零特例。"""
    captured_interrupts({"option_index": 0})

    out = _run(ReviewNode(), _state([_ask_call()]))

    assert out["pending_tool_calls"] == []
    assert [c["name"] for c in out["approved_orchestrate_calls"]] == ["ask_user"]
    assert out["approved_tool_calls"] == []


def test_ask_answer_lands_in_state_slice(captured_interrupts):
    """回答写进 state 的 ask_answers（**闸门的产出是 state，执行器消费 state**）。"""
    captured_interrupts({"option_index": 1, "supplement": "  别动 b.py  "})

    out = _run(ReviewNode(), _state([_ask_call()]))

    assert out["ask_answers"] == {
        "call_ask": {"option_index": 1, "supplement": "别动 b.py"}  # 补充已去首尾空白
    }


def test_ask_without_any_answer_is_recorded_as_unanswered(captured_interrupts):
    """两段皆空（EOF / 直接回车）也照常放行——回执由工具体写成"未作答"，模型据此继续。"""
    captured_interrupts({"option_index": None, "supplement": None})

    out = _run(ReviewNode(), _state([_ask_call()]))

    assert out["ask_answers"] == {"call_ask": {"option_index": None, "supplement": None}}


def test_option_numbering_is_stable_across_gate_panel_and_tool(captured_interrupts):
    """序号在三处各自算出来（闸门校验 / 面板编号 / 工具渲染），**必须同口径**。

    口径一旦分叉，用户选的号会在其中一边越界、被静默当成"没选"——这条把它钉住：乱糟糟的原始
    选项清单（空白项、首尾空格）经过往返，编号仍然对得上。
    """
    seen = captured_interrupts({"option_index": 1, "supplement": None})
    raw_options = ["  甲  ", "   ", "乙"]

    out = _run(
        ReviewNode(),
        _state([{"name": "ask_user", "args": {"why": "w", "question": "q", "options": raw_options}, "id": "call_ask"}]),
    )

    # 面板编号依据的是闸门归一后的清单
    assert seen[0]["options"] == ["甲", "乙"]
    assert out["ask_answers"]["call_ask"]["option_index"] == 1
    # 工具体用自己的 args 渲染，也必须指着同一项
    content = ask_user.func(
        why="w", question="q", options=raw_options, state=out, tool_call_id="call_ask"
    )["messages"][0].content
    assert "2. 乙" in content


def test_ask_out_of_range_option_is_recorded_as_not_chosen(captured_interrupts):
    """序号对上不 options 就当没选（fail-closed）——不能把错位的文本当成选项原文喂给模型。"""
    captured_interrupts({"option_index": 9, "supplement": "还是改 a"})

    out = _run(ReviewNode(), _state([_ask_call()]))

    assert out["ask_answers"]["call_ask"] == {
        "option_index": None,
        "supplement": "还是改 a",
    }


# ------------------------- 闸门前的参数校验 -------------------------


@pytest.mark.parametrize(
    ("args", "hint"),
    [
        ({"why": "", "question": "改哪个？"}, "why"),
        ({"why": "为什么要问", "question": "   "}, "question"),
        (
            {"why": "为什么要问", "question": "改哪个？", "options": [str(i) for i in range(6)]},
            "上限",
        ),
    ],
)
def test_invalid_ask_never_reaches_the_human(monkeypatch, args, hint):
    """参数不合法时**不弹面板**：回一条可行动回执让模型自己改（同 unknown 工具的手感）。"""
    seen: list = []
    monkeypatch.setattr(nodes_module, "interrupt", lambda payload: seen.append(payload))

    out = _run(ReviewNode(), _state([{"name": "ask_user", "args": args, "id": "call_bad"}]))

    assert seen == [], "参数非法不该打扰人"
    assert out["approved_orchestrate_calls"] == [] and out["approved_tool_calls"] == []
    assert out["pending_tool_calls"] == []  # 该调用已被消费，不会卡住
    msg = out["messages"][0]
    assert isinstance(msg, ToolMessage)
    assert msg.tool_call_id == "call_bad"  # 兑现悬空 tool_call
    assert hint in msg.content
    assert msg.additional_kwargs["error_type"] == "invalid_ask"


def test_ask_request_normalizes_and_caps():
    """归一：去空白、丢掉空选项；5 项刚好放行、6 项拦下。"""
    ok, problem = _ask_request(
        {"why": " w ", "question": " q ", "options": ["  甲  ", "", "乙"]}
    )
    assert problem is None
    assert ok == {"why": "w", "question": "q", "options": ["甲", "乙"]}

    _, problem = _ask_request(
        {"why": "w", "question": "q", "options": [str(i) for i in range(_MAX_ASK_OPTIONS + 1)]}
    )
    assert problem is not None


def test_normalize_ask_answer_rejects_non_integer_index():
    assert _normalize_ask_answer({"option_index": "1", "supplement": ""}, ["甲"]) == {
        "option_index": None,
        "supplement": None,
    }


# ------------------------- 工具体：四档回执 -------------------------

OPTS = ["pytest（项目已有基建）", "unittest（stdlib）"]


def _receipt(answer, options=OPTS):
    return ask_user.func(
        why="两条路差别很大",
        question="用哪个测试框架？",
        state={"ask_answers": {"call_ask": answer}},
        tool_call_id="call_ask",
        options=options,
    )["messages"][0]


def test_receipt_reports_both_segments():
    msg = _receipt({"option_index": 1, "supplement": "带上覆盖率"})

    assert msg.name == "ask_user" and msg.tool_call_id == "call_ask"
    assert "2. unittest（stdlib）" in msg.content  # 序号是**给人看的 1 起始**
    assert "带上覆盖率" in msg.content


def test_receipt_reports_option_only():
    content = _receipt({"option_index": 0, "supplement": None}).content
    assert "1. pytest（项目已有基建）" in content
    assert "补充" not in content


def test_receipt_reports_supplement_only_as_a_real_answer():
    """**只补充、没选项是有效回答**——回执必须写清"用户没选选项但给了方案"。"""
    content = _receipt({"option_index": None, "supplement": "我改 t.py 里那个"}).content
    assert "未选" in content and "我改 t.py 里那个" in content
    assert "未作答" not in content


def test_receipt_reports_unanswered():
    content = _receipt({"option_index": None, "supplement": None}).content
    assert "未作答" in content
    assert "不要重复提问" in content  # 可行动：别再把同一个问题问一遍


def test_receipt_without_stored_answer_falls_back_to_unanswered():
    """state 里查不到回答（异常路径）也不能崩：按未作答处理。"""
    msg = ask_user.func(
        why="w", question="q", state={}, tool_call_id="call_missing", options=OPTS
    )["messages"][0]
    assert "未作答" in msg.content


# ------------------------- 登记与结构性排除 -------------------------


def test_ask_source_is_registered_for_gate_and_executor():
    """tool.json 登记是**唯一**的分流依据：漏了它 ask_user 会走普通工具队列、永远不提问。"""
    import json
    from pathlib import Path

    cfg = json.loads((Path(nodes_module.__file__).with_name("tool.json")).read_text("utf-8"))

    assert cfg["ask_user"]["source"] == "ask"
    assert cfg["ask_user"]["need_review"] is False  # 提问没什么可"批准"的
    assert "ask" in ReviewNode.ORCHESTRATE_SOURCES  # 闸门据此把它送进编排队列


def test_worker_graph_refuses_a_hallucinated_ask(monkeypatch):
    """worker 的闸门（allow_ask=False）不能把**编出来的** ask_user 当提问挂起。

    worker 结构性拿不到 ask_user，但它没有 toolset、"未知工具"那道防线不生效——真按提问挂起的
    话主侧会看到一张语义错乱的面板。回执要让模型把"该问什么"写进结论交回。
    """
    seen: list = []
    monkeypatch.setattr(nodes_module, "interrupt", lambda payload: seen.append(payload))

    out = _run(ReviewNode(allow_ask=False), _state([_ask_call()]))

    assert seen == [], "worker 侧不该弹提问面板"
    assert out["approved_orchestrate_calls"] == []
    msg = out["messages"][0]
    assert msg.tool_call_id == "call_ask"
    assert "不能向用户提问" in msg.content
    assert msg.additional_kwargs["error_type"] == "ask_not_available"


def test_worker_cannot_ask():
    """worker 结构性拿不到 ask_user（`source=ask` 不在它的两条 source 规则里、也无 worker_allow）。"""

    class _Tool:
        def __init__(self, name):
            self.name = name

    picked = worker_tools([_Tool("ask_user"), _Tool("read_file"), _Tool("web_search")])

    assert [t.name for t in picked] == ["read_file", "web_search"]


# ------------------------- 端到端：挂起 → 回答 → 回模型 -------------------------


def _ask_ai_message() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "ask_user",
                "args": {
                    "why": "工作区里有两个同名函数，猜错要重做",
                    "question": "改哪个？",
                    "options": ["a.py 里的", "c.py 里的"],
                },
                "id": "call_ask",
            }
        ],
    )


async def _drive(workspace, session: str, model, resume: dict) -> tuple[dict, dict]:
    """建真图（真 MCP 运行体）→ 脚本模型跑到挂起 → resume → 返回 (payload, 终态)。"""
    from app.agent.graph import get_main_agent_graph
    from app.agent.mcp import _reset_pools_for_tests, close_session_pool
    from app.platform.turn import build_turn_state

    _reset_pools_for_tests()
    try:
        graph = await get_main_agent_graph(
            workspace_path=str(workspace), session_id=session, model=model
        )
        compiled = graph.compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": session}}
        parked = await compiled.ainvoke(build_turn_state(session, "帮我改一下那个函数"), config)
        payload = parked["__interrupt__"][0].value
        final = await compiled.ainvoke(Command(resume=resume), config)
        return payload, final
    finally:
        await close_session_pool(str(workspace), session)


def _run_e2e(tmp_path, session: str, resume: dict):
    """模型经 `get_main_agent_graph(model=…)` 注入——回放走的是同一条缝。"""
    script = ReplayChatModel(replies=[_ask_ai_message(), AIMessage(content="好，按你说的做")])
    return asyncio.run(_drive(tmp_path, session, script, resume))


def test_e2e_ask_parks_then_answer_reaches_the_model(tmp_path):
    """闭环：图真挂起（不再执行任何工具）→ 两段回答经工具体成 ToolMessage → 模型继续。"""
    payload, final = _run_e2e(
        tmp_path,
        "e2e-ask",
        {"option_index": 1, "supplement": "别动 b.py"},
    )

    # 挂起时交给人看到的正是问题本身
    assert payload["type"] == GATE_ASK_USER
    assert payload["question"] == "改哪个？"
    assert payload["options"] == ["a.py 里的", "c.py 里的"]

    receipts = [m for m in final["messages"] if isinstance(m, ToolMessage) and m.name == "ask_user"]
    assert len(receipts) == 1
    assert receipts[0].tool_call_id == "call_ask"
    assert "2. c.py 里的" in receipts[0].content and "别动 b.py" in receipts[0].content
    # 回答之后模型真的又跑了一轮（ToolMessage 回到了对话里）
    assert final["messages"][-1].content == "好，按你说的做"


def test_e2e_unanswered_ask_still_returns_to_the_model(tmp_path):
    """没人作答也必须收场：回执写明"未作答"，run 照常走完，不卡在等人上。"""
    _, final = _run_e2e(
        tmp_path, "e2e-ask-blank", {"option_index": None, "supplement": None}
    )

    receipts = [m for m in final["messages"] if isinstance(m, ToolMessage) and m.name == "ask_user"]
    assert len(receipts) == 1
    assert "未作答" in receipts[0].content
    assert final["messages"][-1].content == "好，按你说的做"
