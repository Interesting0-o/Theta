"""evaluation/policy.py 的单测：闸门策略的规则式 / 脚本式 / 全拒三档。

这些是"评估的模拟人"——策略写错会让评估**误报**（把模型的行为问题算成通过，或反之），
所以简写、粘住、对不上号报错这些语义都要钉住。

前提：`import evaluation` 会经 __init__ → runner → app.agent.graph，故需要 .env 存在
（与仓库里大多数测试相同）。
"""
import pytest

from app.schema.approval_schema import GATE_ASK_USER, Decision
from evaluation.policy import allow_except, deny_all, scripted

APPROVAL = {"tool_name": "write_file"}
APPROVAL_TERMINAL = {"tool_name": "run_command"}
ASK = {"type": GATE_ASK_USER, "question": "改哪个？", "options": ["甲", "乙"]}


# ------------------------- allow_except -------------------------


def test_allow_except_approves_by_deny_list():
    policy = allow_except()
    assert policy(APPROVAL) == Decision(kind="approval", approved=True)
    assert policy(APPROVAL_TERMINAL) == Decision(kind="approval", approved=False)


def test_allow_except_can_be_narrowed():
    """黑名单可换：评估某个任务真需要跑命令时，任务自带一份更窄（或没有）的黑名单。"""
    policy = allow_except(deny=())
    assert policy(APPROVAL_TERMINAL).approved is True


def test_allow_except_answers_questions_in_order():
    """`answers` 是提问的回答源——**没有它，ask_user 在评估里恒为"未作答"，根本测不了**。"""
    policy = allow_except(answers=["用 pytest", "按方案 B"])

    assert policy(ASK) == Decision(kind="answer", supplement="用 pytest")
    assert policy(ASK) == Decision(kind="answer", supplement="按方案 B")
    # 用尽后回落到"未作答"（本设计里"没人答"的那条现成语义），不是报错
    assert policy(ASK) == Decision(kind="answer")
    # 提问与审批互不干扰：答了两个问题不影响审批判定
    assert policy(APPROVAL).approved is True


def test_allow_except_without_answers_never_invents_one():
    assert allow_except()(ASK) == Decision(kind="answer")


# ------------------------- deny_all -------------------------


def test_deny_all_rejects_everything_but_does_not_answer():
    """全拒是专测拒绝路径的：审批一律不放行，提问仍是"未作答"而非"否决"。"""
    policy = deny_all()

    assert policy(APPROVAL) == Decision(kind="approval", approved=False)
    assert policy(APPROVAL_TERMINAL) == Decision(kind="approval", approved=False)
    assert policy(ASK) == Decision(kind="answer")


# ------------------------- scripted -------------------------


def test_scripted_accepts_shorthands():
    """简写表：bool → 审批批准/拒绝；str → 提问的回答（"用户自己说了方案"）。"""
    policy = scripted(True, False, "我改 t.py 里那个")

    assert policy(APPROVAL) == Decision(kind="approval", approved=True)
    assert policy(APPROVAL) == Decision(kind="approval", approved=False)
    assert policy(ASK) == Decision(kind="answer", supplement="我改 t.py 里那个")


def test_scripted_accepts_full_decision_objects():
    explicit = Decision(kind="answer", option_index=1, option_text="乙", supplement="带上覆盖率")
    assert scripted(explicit)(ASK) == explicit


def test_scripted_sticks_on_the_last_decision():
    """粘住 = 天然的"前 N 次这样、之后都那样"：scripted(True, False) = 第一次批、之后全拒。"""
    policy = scripted(True, False)

    assert policy(APPROVAL).approved is True
    assert policy(APPROVAL).approved is False
    assert policy(APPROVAL).approved is False


def test_scripted_single_decision_sticks_forever():
    policy = scripted(False)
    assert all(policy(APPROVAL).approved is False for _ in range(5))


def test_scripted_requires_at_least_one_decision():
    with pytest.raises(ValueError, match="至少要给一条"):
        scripted()


def test_scripted_rejects_unknown_shorthand():
    with pytest.raises(TypeError, match="看不懂"):
        scripted(1.5)(APPROVAL)


def test_scripted_fails_loudly_when_script_and_gate_dont_line_up():
    """顺序对不上必须**当场报错**：静默降级成"未作答"会让人以为模型没提问。"""
    with pytest.raises(ValueError, match="对不上"):
        scripted(True)(ASK)  # 拿审批决定去答一个提问

    with pytest.raises(ValueError, match="对不上"):
        scripted("一段话")(APPROVAL)  # 拿回答去顶一个审批


def test_scripted_error_names_the_position():
    """报错要指出是第几条——脚本长了才发现对不上时，位置就是一切。"""
    policy = scripted(True, True, "回答")
    policy(APPROVAL)
    policy(APPROVAL)
    with pytest.raises(ValueError, match="第 3 条"):
        policy(APPROVAL)
