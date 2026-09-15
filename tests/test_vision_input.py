"""图像输入的最小链路：两种图块形状 → 请求体（契约）+ 可选的真实调用。

两层（照 `tests/test_thinking_config.py` 的结构）：

1. **契约用例**（无网络，进 CI）：LangChain 的规范图块与 OpenAI 风格的 `image_url` 块，经
   `ChatOpenAI` 之后都变成 `{"type": "image_url", "image_url": {"url": "data:…;base64,…"}}`
   ——钉住"我们构造的消息形状库接得住"。**测的是库的行为**，库变了它会红。
2. **可选的真实调用**（默认 skip）：用 stdlib 现造一张左红右蓝的 PNG 发出去，断言回复里**同时
   出现红与蓝**——"非空"不算过，那验不出它到底看没看图。

前提：**模型必须能看图**。更早（2026-09-14）配置的 `glm-4.7-flash` 会硬拒
（`400 / code 1210 / messages.content.type 参数非法，取值范围 ['text']`），当日的替代是
`glm-4.6v-flash`（base64 与 http URL 都行，且**能同时挂工具**——agent 每轮都 `bind_tools`，
这是它能当主模型的前提）。
**2026-09-15 复测：当前配置的 `glm-5.3-flash` 已支持视觉**，下面的 live 用例 3 条全过——
换模型后请重跑一次再下结论，别照抄这里的历史结论。

**URL 路径不写进用例**：它要求一个稳定的外部图片地址，会随外部世界腐坏。通路是同一条——
把 `image_url.url` 换成 http 地址即可（已实测：`glm-4v-flash` 认出了百度 logo）。

需要 `.env` 存在（`app.agent.model` 在 import 期就调 `get_settings()`，CLAUDE.md 前提）。
"""
import asyncio
import base64
import os
import struct
import time
import zlib

import openai
import pytest
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

import app.agent.model as model_module

_LIVE_FLAG = "THETA_LIVE_VISION"
_LIVE_MODEL_ENV = "THETA_LIVE_VISION_MODEL"
_DEFAULT_VISION_MODEL = "glm-4.6v-flash"


def _png(width: int, height: int, left: tuple, right: tuple) -> bytes:
    """纯 stdlib 造一张左半 left 色、右半 right 色的 PNG：不引 Pillow，也不依赖任何外部图片。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    rows = b"".join(
        b"\x00" + bytes(left) * (width // 2) + bytes(right) * (width // 2) for _ in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


# 左半纯红、右半纯蓝的 64×64 图——用来验"它到底看没看见"，而不是"回了个非空字符串"
_IMAGE_BASE64 = base64.b64encode(_png(64, 64, (255, 0, 0), (0, 0, 255))).decode()


def _sent_content(message) -> list:
    """把一条消息过一次 `ChatOpenAI` 的转换，取它**真正会发出去**的 content（不联网）。"""
    model = ChatOpenAI(model="fake", api_key="dummy", base_url="http://127.0.0.1:1/v1")
    return model._get_request_payload([message])["messages"][0]["content"]


# ---------------------- ① 契约：两种图块形状都到得了请求体 ----------------------


def test_langchain_image_block_becomes_a_data_uri():
    """LangChain 的规范图块会被**自动翻译**成 OpenAI 的 `image_url` + data URI（我们不必自己拼）。"""
    content = _sent_content(
        HumanMessage(
            content=[
                {"type": "text", "text": "这张图是什么？"},
                {"type": "image", "base64": _IMAGE_BASE64, "mime_type": "image/png"},
            ]
        )
    )

    assert content[0] == {"type": "text", "text": "这张图是什么？"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{_IMAGE_BASE64}"},
    }


def test_openai_style_image_url_passes_through():
    """`image_url` 形状原样透传——http 地址与 data URI 走的是同一个字段。"""
    content = _sent_content(
        HumanMessage(
            content=[{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]
        )
    )

    assert content[0]["image_url"]["url"] == "https://example.com/a.png"


# ---------------------- ② 可选的真实调用（默认 skip） ----------------------


def _invoke_with_retry(model, message):
    """真打一次；限流/网络不通 → **skip**（环境问题不是被测链路的缺陷）。"""
    for attempt in range(4):
        try:
            return asyncio.run(model.ainvoke([message]))
        except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError) as exc:
            if attempt == 3:
                pytest.skip(f"端点暂时不可用（限流/网络），本次没验成：{type(exc).__name__}: {exc}")
            time.sleep(8)
    raise AssertionError("不可达：上面的循环要么 return 要么 skip")


def test_live_vision_sees_the_image_through_our_model_layer(monkeypatch):
    """真打一次 API：通过 `get_main_chat_model()` 发一张图，回复里要看得出图的内容。

    默认 skip（花钱 + 依赖网络 + 要求模型是视觉模型）。显式打开：

        THETA_LIVE_VISION=1 THETA_LIVE_VISION_MODEL=glm-4.6v-flash \
            .venv/Scripts/python.exe -m pytest tests/test_vision_input.py

    模型名用 `monkeypatch` 覆盖 settings，**不动 `.env`**。
    """
    if not os.getenv(_LIVE_FLAG):
        pytest.skip(f"需要 {_LIVE_FLAG}=1 才跑（真打 API，会花钱）")

    model_name = os.getenv(_LIVE_MODEL_ENV) or _DEFAULT_VISION_MODEL
    monkeypatch.setattr(
        model_module,
        "settings",
        model_module.settings.model_copy(update={"CHAT_MODEL_NAME": model_name}),
    )
    reply = _invoke_with_retry(
        model_module.get_main_chat_model(),
        HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": "这张图左半边和右半边分别是什么颜色？只回答两个颜色词，用逗号分隔",
                },
                {"type": "image", "base64": _IMAGE_BASE64, "mime_type": "image/png"},
            ]
        ),
    )

    text = str(reply.content)
    assert "红" in text and "蓝" in text, (
        f"回复里看不出图的内容（模型 {model_name} 可能不是视觉模型）：{text!r}"
    )
