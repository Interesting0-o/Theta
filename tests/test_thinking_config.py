"""思考模式：配置映射、**"思考不进 messages"的契约**、以及可选的真调用验证。

三层各守一件事（少一层就有一段没人管）：

1. `thinking_extra_body` 的三态映射（纯函数）与"配置值真的落到请求体上"；
2. **契约用例**：`langchain-openai` 的响应转换**不提取** `reasoning_content`——这是"思考不会进
   messages"这条不变量的**唯一依据**。哪天库改成会提取，这条会红——那正是我们要被叫醒的时刻，
   届时在 `LLMNode` 的写回点补剥离（那里有注释指向本文件）；
3. **可选的真调用用例**：默认 skip，只有显式设 `THETA_LIVE_THINKING=1` 才跑。它真打一次 API，
   验证"配置确实生效、厂端确实返回思考、正文没被思考挤空"。花钱且依赖网络，故不进默认回路。

需要 `.env` 存在（`app.agent.nodes` 在 import 期就调 `get_settings()`，CLAUDE.md 前提）。
"""
import asyncio
import os
import time
from types import SimpleNamespace

import openai
import pytest
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

import app.agent.nodes as nodes_module
from app.agent.nodes import LLMNode
from app.exception import ConfigError

# 带思考字段的假响应：形状照 OpenAI Chat Completions，只多一个厂商扩展字段
_RAW_WITH_REASONING = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "fake-model",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": "答案是 4。",
                "reasoning_content": "让我想想：2 + 2 …",
            },
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14},
}

_LIVE_FLAG = "THETA_LIVE_THINKING"


def _bare_model() -> ChatOpenAI:
    """一个不联网的 ChatOpenAI：构造期不发请求，`api_key` 是假的。"""
    return ChatOpenAI(model="fake-model", api_key="dummy", base_url="http://127.0.0.1:1/v1")


# ---------------------- ① 映射（纯函数，零成本） ----------------------


def test_thinking_extra_body_maps_the_three_states():
    assert LLMNode.thinking_extra_body("enabled") == {"thinking": {"type": "enabled"}}
    assert LLMNode.thinking_extra_body("DISABLED ") == {"thinking": {"type": "disabled"}}
    # 留空 → None（**不是**空 dict）：库的 `exclude_if_none` 会把 None 的键整个丢掉，
    # 于是"不下发该参数"才成立；空 dict 反而会以 `{}` 发出去。
    assert LLMNode.thinking_extra_body("") is None
    assert LLMNode.thinking_extra_body(None) is None


def test_thinking_extra_body_rejects_typos_loudly():
    """写错就当场抛：静默按"没配"处理会让用户以为开了思考、实际没开，而这种事行为上几乎不可观测。"""
    with pytest.raises(ConfigError) as excinfo:
        LLMNode.thinking_extra_body("ture")  # 故意的错拼

    assert "CHAT_THINKING" in str(excinfo.value)


# ---------------------- ② 契约：库不提取思考 ----------------------


def test_chatopenai_does_not_extract_reasoning_content():
    """`reasoning_content` 不进 `AIMessage` —— "思考不占 messages"这条不变量的唯一依据。

    ⚠️ 这条**测的是库的行为，不是我们的代码**（故意的）。库哪天开始提取，它就红，我们补剥离。
    """
    typed = openai.types.chat.ChatCompletion.model_validate(_RAW_WITH_REASONING)

    message = _bare_model()._create_chat_result(typed).generations[0].message

    assert message.content == "答案是 4。"  # 正文照常拿到
    assert "reasoning_content" not in message.additional_kwargs


def test_reasoning_content_survives_in_the_raw_sdk_object():
    """对照面：数据在 **SDK 层**是活的——上面那条契约才说得清"是谁把它弄丢的"。"""
    typed = openai.types.chat.ChatCompletion.model_validate(_RAW_WITH_REASONING)

    extra = typed.choices[0].message.model_extra or {}

    assert extra.get("reasoning_content") == "让我想想：2 + 2 …"


# ---------------------- ③ 配置 → 模型构造 ----------------------


def _model_with_thinking(monkeypatch, mode: str) -> ChatOpenAI:
    """用假 settings 构造模型：只换 `CHAT_THINKING`，其余照真配置的字段形状。"""
    monkeypatch.setattr(
        nodes_module,
        "settings",
        SimpleNamespace(
            CHAT_MODEL_NAME="fake-model",
            CHAT_MODEL_URL="http://127.0.0.1:1/v1",
            CHAT_MODEL_API_KEY=SecretStr("dummy"),
            CHAT_THINKING=mode,
        ),
    )
    return LLMNode.main_chat_model()


def test_configured_thinking_reaches_the_request_payload(monkeypatch):
    """配置的值要真落到请求体上（`extra_body` 是 ChatOpenAI 的一等字段，不走 model_kwargs）。"""
    model = _model_with_thinking(monkeypatch, "disabled")

    assert model._default_params["extra_body"] == {"thinking": {"type": "disabled"}}


def test_empty_config_does_not_send_the_key_at_all(monkeypatch):
    """留空 = **不下发**该参数（不是下发一个空对象）——默认行为必须与"根本没有这个键"一致。"""
    model = _model_with_thinking(monkeypatch, "")

    assert "extra_body" not in model._default_params


# ---------------------- ④ 可选的真调用验证（默认 skip） ----------------------


def _create_with_retry(client, **payload):
    """真打一次；撞上限流/网络就重试几次，仍不行则 **skip**（不是 fail）。

    实测智谱免费层会返 429（"该模型当前访问量过大"）。限流是**环境**的问题，不是被验的配置
    有什么毛病——做成失败会误导（同 tests/test_file_io_sandbox.py 对"无符号链接权限"的处置）。
    """
    last: Exception | None = None
    for attempt in range(4):
        try:
            return client.chat.completions.create(**payload)
        except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError) as exc:
            last = exc
            if attempt < 3:
                time.sleep(8)
    pytest.skip(f"端点暂时不可用（限流/网络），本次没验成：{type(last).__name__}: {last}")


def test_live_endpoint_returns_reasoning_and_the_model_sheds_it(monkeypatch):
    """真打一次 API：配置生效、厂端真返回思考、正文没被挤空、且 langchain 侧拿不到思考。

    默认 skip（花钱 + 依赖网络 + 依赖端点支持 `thinking`）。显式打开：

        THETA_LIVE_THINKING=1 .venv/Scripts/python.exe -m pytest tests/test_thinking_config.py
    """
    if not os.getenv(_LIVE_FLAG):
        pytest.skip(f"需要 {_LIVE_FLAG}=1 才跑（真打 API，会花钱）")

    real = nodes_module.settings
    prompt = "一句话说明为什么 2+2=4"

    # ① 旁路直打 SDK：证明**厂端确实在返回思考**，且正文没被思考挤空。
    client = openai.OpenAI(
        api_key=real.CHAT_MODEL_API_KEY.get_secret_value(), base_url=real.CHAT_MODEL_URL
    )
    raw = _create_with_retry(
        client,
        model=real.CHAT_MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"thinking": {"type": "enabled"}},
    )
    message = raw.choices[0].message
    reasoning = (message.model_extra or {}).get("reasoning_content") or ""
    assert reasoning, "厂端没返回思考——该端点可能不支持 thinking 参数"
    assert (message.content or "").strip(), (
        "正文是空的：思考把 max_tokens 吃光了（见 .env.example 里那条提醒）"
    )

    # ② 走我们自己的模型：正文正常，且**思考没有被带进来**（契约用例守的那条不变量，端到端再验一次）
    monkeypatch.setattr(
        model_module, "settings", real.model_copy(update={"CHAT_THINKING": "enabled"})
    )
    try:
        result = asyncio.run(LLMNode.main_chat_model().ainvoke(prompt))
    except openai.RateLimitError as exc:  # 同①：环境问题不当失败
        pytest.skip(f"端点限流，第二段没验成：{exc}")

    assert isinstance(result, AIMessage)
    assert str(result.content).strip()
    assert "reasoning_content" not in result.additional_kwargs
