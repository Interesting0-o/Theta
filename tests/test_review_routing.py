"""review 分流与图路由单测（不拉起真实 MCP、不触发真实 interrupt）。

分流类用例用 tool.json 里 need_review:false 的工具（create_plan / read_file），
所以 ReviewNode 不会 interrupt，可安全地 asyncio.run 驱动；
审批闸门类用例 monkeypatch interrupt，验证需审批工具（如 web_search）的挂起与决策。
run_command 走**命令级**判定（tool.json 的 free_when: readonly_shell）：只读终端命令免审、
其余照常挂起，判定本体是 `ReviewNode._free_shell_verdict`（纯函数，直接单测）。
"""
import asyncio

import pytest
from langchain_core.messages import ToolMessage

from app.agent.nodes import ReviewNode
from app.agent.graph import should_review_or_execute, should_continue_after_orchestrate
import app.agent.nodes as nodes_module


def _call(name, call_id="call_x"):
    return {"name": name, "args": {}, "id": call_id}


def _cmd(command, call_id="call_c"):
    return {
        "name": "run_command",
        "args": {"command": command, "description": "看一眼仓库状态"},
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


def test_orchestration_call_goes_to_orchestrate_queue():
    """source=='plan' 的编排调用应落入 approved_orchestrate_calls。"""
    out = _run(ReviewNode(), _state([_call("create_plan")]))
    assert out["pending_tool_calls"] == []
    assert [c["name"] for c in out["approved_orchestrate_calls"]] == ["create_plan"]
    assert out["approved_tool_calls"] == []


def test_ordinary_call_goes_to_tool_queue():
    """免审的普通调用应落入 approved_tool_calls。"""
    out = _run(ReviewNode(), _state([_call("read_file")]))
    assert out["pending_tool_calls"] == []
    assert out["approved_orchestrate_calls"] == []
    assert [c["name"] for c in out["approved_tool_calls"]] == ["read_file"]


def test_mixed_turn_splits_into_separate_queues():
    """编排+普通并存时，review 逐条 drain 后分别落入两个队列。"""
    node = ReviewNode()
    out1 = _run(node, _state([_call("create_plan", "c1"), _call("read_file", "c2")]))
    # 第一次只消费 create_plan，read_file 仍在 pending
    assert [c["id"] for c in out1["pending_tool_calls"]] == ["c2"]
    assert [c["name"] for c in out1["approved_orchestrate_calls"]] == ["create_plan"]
    assert out1["approved_tool_calls"] == []

    out2 = _run(node, out1)
    assert out2["pending_tool_calls"] == []
    assert [c["name"] for c in out2["approved_orchestrate_calls"]] == ["create_plan"]
    assert [c["name"] for c in out2["approved_tool_calls"]] == ["read_file"]


def test_need_review_tool_interrupts_and_approval_lets_it_pass(monkeypatch):
    """web_search 等需审批工具必须挂起；批准后才进入放行队列。"""
    interrupts = []
    monkeypatch.setattr(
        "app.agent.nodes.interrupt",
        lambda payload: interrupts.append(payload) or {"approved": True},
    )
    out = _run(ReviewNode(), _state([_call("web_search")]))
    assert [p["tool_name"] for p in interrupts] == ["web_search"]
    assert out["pending_tool_calls"] == []
    assert [c["name"] for c in out["approved_tool_calls"]] == ["web_search"]


def test_need_review_tool_rejection_injects_denial_message(monkeypatch):
    """被拒的需审批调用不放行，但须回灌一条 ToolMessage 告知模型（兑现悬空 tool_call）。"""
    monkeypatch.setattr(
        "app.agent.nodes.interrupt",
        lambda payload: {"approved": False},
    )
    out = _run(ReviewNode(), _state([_call("web_search", call_id="call_w1")]))
    assert out["pending_tool_calls"] == []
    assert out["approved_tool_calls"] == []
    assert out["approved_orchestrate_calls"] == []
    # 拒绝必须回模型一条显式信号：以原 tool_call_id 兑现，内容标明被拒
    msgs = out.get("messages", [])
    assert len(msgs) == 1
    msg = msgs[0]
    assert isinstance(msg, ToolMessage)
    assert msg.tool_call_id == "call_w1"
    assert msg.name == "web_search"
    assert "approval_denied" in msg.content


def test_need_review_tool_approval_injects_no_message(monkeypatch):
    """批准路径不应注入多余 ToolMessage（只有拒绝才需要反馈）。"""
    monkeypatch.setattr(
        "app.agent.nodes.interrupt",
        lambda payload: {"approved": True},
    )
    out = _run(ReviewNode(), _state([_call("web_search")]))
    assert out.get("messages") is None


def test_denied_call_only_gets_denial_message_in_mixed_turn(monkeypatch):
    """同一 AIMessage 的多个调用：被拒的那个收到拒绝消息，免审放行的那个不注入。"""
    monkeypatch.setattr(
        "app.agent.nodes.interrupt",
        lambda payload: {"approved": False},  # web_search 被拒；read_file 免审不会走到这里
    )
    node = ReviewNode()
    out1 = _run(node, _state([_call("web_search", call_id="c1"), _call("read_file", call_id="c2")]))
    # 第一次：c1 被拒 → 注入 c1 的拒绝消息，c2 留在 pending
    assert [m.tool_call_id for m in out1.get("messages", [])] == ["c1"]
    assert [c["id"] for c in out1["pending_tool_calls"]] == ["c2"]

    out2 = _run(node, out1)
    # 第二次：c2 免审放行入 approved_tool_calls，不再注入消息
    assert out2.get("messages") is None
    assert [c["id"] for c in out2["approved_tool_calls"]] == ["c2"]


# ---------- run_command 的命令级免审判定（_free_shell_verdict）----------
# 失败方向是重点：这些都是**必须落回审批**的形态，漏一个就是静默放行一次没人看过的命令。

FREE_SHELL_COMMANDS = [
    "git status",
    "git status --porcelain --branch",
    "git log --oneline -20",
    "git log --pretty=format:%h %s",
    "git diff HEAD~1",
    "git diff --staged -- src/app.py",
    "git fetch origin",
    "git branch",
    "git branch -a -v",
    "git -C sub/repo status",  # 多仓库工作区里模型的常见写法
    "git --no-pager log -5",
]

REVIEW_SHELL_COMMANDS = [
    "",
    "ls -la",  # 尚未纳入免审面（首版只收 git）
    "git",  # 没有子命令
    "git commit -m x",
    "git push origin main",
    "git branch -d feat",  # 删分支：branch 只认列分支用法
    "git branch newbranch",  # 建分支
    "git log --output=out.txt",  # 只读子命令也能写文件
    "git diff -o out.txt",
    "git -c http.sslVerify=false status",  # TODO 点名要挡的"关证书校验"开关
    "git --git-dir=/tmp/x status",
    "GIT_SSL_NO_VERIFY=1 git status",  # 首词不是 git
    "git status && rm -rf x",  # 元字符以下：作用域可被改写
    "git status | cat",
    "git status > out.txt",
    "git status; ls",
    "git^ status",  # cmd 的转义符
    "git status\r\n",
]


@pytest.mark.parametrize("command", FREE_SHELL_COMMANDS)
def test_free_shell_verdict_accepts_readonly_commands(command):
    assert ReviewNode._free_shell_verdict({"command": command}) is True


@pytest.mark.parametrize("command", REVIEW_SHELL_COMMANDS)
def test_free_shell_verdict_rejects_everything_else(command):
    assert ReviewNode._free_shell_verdict({"command": command}) is False


def test_free_shell_verdict_is_fail_closed_without_a_command():
    """参数缺失/类型不对（模型乱给）不得放行。"""
    assert ReviewNode._free_shell_verdict({}) is False
    assert ReviewNode._free_shell_verdict({"command": ["git", "status"]}) is False


def test_command_policy_file_is_loadable_and_shaped():
    """策略表来自 app/agent/command_policy.json；读取时归一了类型。"""
    policy = nodes_module._command_policy()
    assert set(policy) == {"free_commands", "write_flags", "global_skip", "branch_list_flags"}
    assert "git" in policy["free_commands"]
    # write_flags 必须是 tuple：str.startswith 只认 str 或 tuple，裸 list 会 TypeError
    assert isinstance(policy["write_flags"], tuple)


def test_verdict_reads_the_policy_table_not_hardcoded_rules(monkeypatch):
    """解耦的证明：**只改表、不动代码**，判定立刻跟着变。"""
    base = dict(nodes_module._command_policy())

    widened = {
        **base,
        "free_commands": {**base["free_commands"], "git": base["free_commands"]["git"] | {"show"}},
    }
    monkeypatch.setattr(nodes_module, "_command_policy", lambda: widened)
    assert ReviewNode._free_shell_verdict({"command": "git show HEAD~1"}) is True  # 表里新加的子命令

    narrowed = {**base, "free_commands": {**base["free_commands"], "git": frozenset()}}
    monkeypatch.setattr(nodes_module, "_command_policy", lambda: narrowed)
    assert ReviewNode._free_shell_verdict({"command": "git status"}) is False  # 原本免审的，抽掉即收回


def test_free_terminal_command_skips_the_approval_panel(monkeypatch):
    """只读 git 命令不再打断人：直接放行进执行队列（这是下沉 git 不增摩擦的前提）。"""

    def _unexpected(payload):
        raise AssertionError("只读命令不该弹审批面板")

    monkeypatch.setattr("app.agent.nodes.interrupt", _unexpected)
    out = _run(ReviewNode(), _state([_cmd("git status --porcelain")]))
    assert out["pending_tool_calls"] == []
    assert [c["name"] for c in out["approved_tool_calls"]] == ["run_command"]


def test_write_terminal_command_still_asks_for_approval(monkeypatch):
    """同一个工具、写形态照常挂起——决策只由命令文本决定。"""
    interrupts = []
    monkeypatch.setattr(
        "app.agent.nodes.interrupt",
        lambda payload: interrupts.append(payload) or {"approved": True},
    )
    out = _run(ReviewNode(), _state([_cmd("git push origin main")]))
    assert [p["tool_name"] for p in interrupts] == ["run_command"]
    assert [c["name"] for c in out["approved_tool_calls"]] == ["run_command"]


def test_should_review_or_execute_prefers_orchestrate():
    state = {
        "pending_tool_calls": [],
        "approved_orchestrate_calls": [_call("create_plan")],
        "approved_tool_calls": [_call("read_file")],
    }
    assert should_review_or_execute(state) == "orchestrate_node"


def test_should_review_or_execute_drains_pending_first():
    state = {
        "pending_tool_calls": [_call("create_plan")],
        "approved_orchestrate_calls": [],
        "approved_tool_calls": [_call("read_file")],
    }
    assert should_review_or_execute(state) == "review_node"


def test_after_orchestrate_falls_back_to_tool_or_llm():
    with_tool = {
        "pending_tool_calls": [],
        "approved_orchestrate_calls": [_call("create_plan")],
        "approved_tool_calls": [_call("read_file")],
    }
    assert should_continue_after_orchestrate(with_tool) == "tool_node"

    only_orchestrate = {
        "pending_tool_calls": [],
        "approved_orchestrate_calls": [_call("create_plan")],
        "approved_tool_calls": [],
    }
    assert should_continue_after_orchestrate(only_orchestrate) == "llm_node"
