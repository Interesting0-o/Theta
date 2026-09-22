"""@图片路径 附图通道（app/resource/images.py::attach_images_to_payload）的机制测试——无网络。

真调用的最小链路见 test_vision_input.py（模型层）；本文件钉的是**通道本身**：
@token 怎么解析、base64 怎么只进请求体副本、state 怎么保持纯文本、记账怎么防止重发、
模型侧的 @ 为什么一律不认。
"""
import asyncio
import os
import time
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import app.agent.nodes as nodes_module
from app.agent.nodes import LLMNode
from app.resource import images
from app.schema.agent_schema import ImageRef


def _make_ws(tmp_path: Path) -> str:
    """造一个小工作区：两张真扩展名的假图（通道不解析图像内容，只认扩展名与字节）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    (ws / "b.jpg").write_bytes(b"\xff\xd8fakejpeg")
    (ws / "with space.png").write_bytes(b"\x89PNGfake")
    (ws / "note.txt").write_text("hello", encoding="utf-8")
    return str(ws)


class _CaptureModel:
    """记录每次收到的 messages，回复固定（同 test_memory_injection 的手法）。"""

    def __init__(self):
        self.seen = []
        self.reply = AIMessage(content="好的")

    async def ainvoke(self, messages, **kwargs):
        self.seen.append(list(messages))
        return self.reply


# ---------------------- 解析：宽进策略与截断 ----------------------


def test_relative_and_absolute_path_both_attach(tmp_path):
    ws = _make_ws(tmp_path)
    abs_ref = Path(ws) / "b.jpg"
    msgs = [HumanMessage(content=f"@a.png 相对的 @{abs_ref} 绝对的")]

    out, newly = asyncio.run(images.attach_images_to_payload(msgs, [], ws))

    assert [ref["mime"] for ref in newly] == ["image/png", "image/jpeg"]
    assert all(Path(ref["path"]).is_absolute() for ref in newly)
    # 副本成了 [文本, 图, 图] 三块
    assert out[0].content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert out[0].content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # 传入的列表没被改动（副本语义）
    assert msgs[0].content == f"@a.png 相对的 @{abs_ref} 绝对的"


def test_only_last_human_message_is_parsed(tmp_path):
    """作用域 = 最后一条 HumanMessage：更早消息里的 @ 不解析（那一轮早已发过）。"""
    ws = _make_ws(tmp_path)
    msgs = [
        HumanMessage(content="@a.png 上一轮"),
        AIMessage(content="好"),
        HumanMessage(content="这轮没图"),
    ]

    out, newly = asyncio.run(images.attach_images_to_payload(msgs, [], ws))

    assert newly == []
    assert out[0].content == "@a.png 上一轮"  # 旧消息原样


def test_cjk_tail_is_cut_and_remainder_kept(tmp_path):
    """`@a.png帮我看` 不打空格：路径截到扩展名处，"帮我看"留在正文。"""
    ws = _make_ws(tmp_path)
    msgs = [HumanMessage(content="帮我看@a.png和@b.jpg这两张")]

    out, newly = asyncio.run(images.attach_images_to_payload(msgs, [], ws))

    assert len(newly) == 2
    text = out[0].content[0]["text"]
    assert text == "帮我看[图片已附加: a.png]和[图片已附加: b.jpg]这两张"
    assert len(out[0].content) == 3  # 文本 + 两张图


def test_quoted_path_with_spaces(tmp_path):
    ws = _make_ws(tmp_path)
    msgs = [HumanMessage(content='@"with space.png" 看看这张')]

    out, newly = asyncio.run(images.attach_images_to_payload(msgs, [], ws))

    assert len(newly) == 1 and newly[0]["mime"] == "image/png"
    assert "[图片已附加: with space.png]" in out[0].content[0]["text"]


def test_image_token_mime_follows_the_admission_rule():
    """mime 必须由**命中的那个扩展名**决定，而不是 Path(raw).suffix。

    两者不等价：收词口径是"任意处命中扩展名"，suffix 是"最后一个点号之后的整段"。
    回归：`@"shot.png "` 的 suffix 是 `.png `、`@"a.png（新版）"` 是 `.png（新版）`，
    都不在 _IMAGE_MIME 里 → KeyError → 整轮 turn 直接失败（不是软回执）。
    """
    for text, want in [
        ('@"with space.png "', "image/png"),   # suffix 会是 '.png '
        ('@"a.png（新版）"', "image/png"),      # suffix 会是 '.png（新版）'
        ("@a.png", "image/png"),
        ("@b.JPG", "image/jpeg"),              # 大小写不敏感（_IMAGE_EXT_RE 是 IGNORECASE）
        ("@c.jpeg", "image/jpeg"),
    ]:
        tokens = images.image_tokens(text)
        assert len(tokens) == 1, text
        assert tokens[0][3] == want, text


def test_quoted_token_that_is_not_a_real_file_gets_note_not_crash(tmp_path):
    """引号里"含扩展名但不止于扩展名"→ 软回执（路径就是那段原文，指不到文件）。"""
    ws = _make_ws(tmp_path)
    content = '@"with space.png（新版）" 看看'

    out, newly = asyncio.run(
        images.attach_images_to_payload([HumanMessage(content=content)], [], ws)
    )

    assert newly == []
    assert "[图片未找到: with space.png（新版）]" in out[0].content[0]["text"]


def test_non_image_at_tokens_left_alone(tmp_path):
    """宽进策略：不像图片的 @token 是普通文本，整条消息保持 str 原样。"""
    ws = _make_ws(tmp_path)
    raw = "@note.txt 和 @someone 还有裸@ 与 @\"没闭合 不算"

    msgs = [HumanMessage(content=raw)]
    out, newly = asyncio.run(images.attach_images_to_payload(msgs, [], ws))

    assert newly == []
    assert out[0].content == raw  # 没有任何图块，消息原样


# ---------------------- 处置：失败当说明，不当异常 ----------------------


def test_missing_file_becomes_note(tmp_path):
    ws = _make_ws(tmp_path)
    msgs = [HumanMessage(content="@nope.png 看看")]

    out, newly = asyncio.run(images.attach_images_to_payload(msgs, [], ws))

    assert newly == []
    assert out[0].content[0]["text"] == "[图片未找到: nope.png] 看看"
    assert len(out[0].content) == 1  # 只有文本块，无图


def test_oversize_becomes_note(tmp_path, monkeypatch):
    ws = _make_ws(tmp_path)
    monkeypatch.setattr(images, "_MAX_IMAGE_BYTES", 16)
    msgs = [HumanMessage(content="@a.png 看看")]

    out, newly = asyncio.run(images.attach_images_to_payload(msgs, [], ws))

    assert newly == []
    assert "[图片过大" in out[0].content[0]["text"]


def test_over_count_becomes_note(tmp_path, monkeypatch):
    ws = _make_ws(tmp_path)
    monkeypatch.setattr(images, "_MAX_IMAGES_PER_MESSAGE", 1)
    msgs = [HumanMessage(content="@a.png 和 @b.jpg")]

    out, newly = asyncio.run(images.attach_images_to_payload(msgs, [], ws))

    assert len(newly) == 1  # 第一张照常附
    text = out[0].content[0]["text"]
    assert "[图片已附加: a.png]" in text and "图片数量超上限" in text


# ---------------------- 记账与作用域 ----------------------


def test_already_attached_not_resent(tmp_path):
    """同一轮工具循环的第二次调用：已发过的图换成"已发送过"说明，不重发 base64。"""
    ws = _make_ws(tmp_path)
    ref = ImageRef(path=str((Path(ws) / "a.png").resolve()), mime="image/png")
    msgs = [HumanMessage(content="@a.png 再看一眼"), AIMessage(content="图我看到了")]

    out, newly = asyncio.run(images.attach_images_to_payload(msgs, [ref], ws))

    assert newly == []
    assert out[0].content[0]["text"].startswith("[图片已在上文发送过")
    assert len(out[0].content) == 1


def test_at_in_aimessage_is_never_parsed(tmp_path):
    """模型侧安全边界：AIMessage 里的 @ 一律不认（否则模型可指挥宿主读本地文件送 API）。"""
    ws = _make_ws(tmp_path)
    aim = AIMessage(content="请发 @a.png 给我")
    msgs = [HumanMessage(content="你好"), aim]

    out, newly = asyncio.run(images.attach_images_to_payload(msgs, [], ws))

    assert newly == []
    assert out[1] is aim  # 模型消息原样


# ---------------------- LLMNode 端到端（假模型） ----------------------


def test_llm_node_end_to_end(tmp_path):
    """过一遍 LLMNode：state 消息保持纯文本、模型收到副本图块、记账写回、下轮不重发。"""
    ws = _make_ws(tmp_path)
    model = _CaptureModel()
    node = LLMNode(model=model, workspace_path=ws)
    state = {"messages": [HumanMessage(content="@a.png 这是什么")]}

    out = asyncio.run(node(state))

    # state 侧：原消息没被污染（调用方传入的列表原样）
    assert state["messages"][0].content == "@a.png 这是什么"
    # 模型侧：副本带图（系统消息在头部，用户消息在最后）
    sent = model.seen[0][-1].content
    assert sent[1]["type"] == "image_url" and sent[1]["image_url"]["url"].startswith("data:")
    assert "[图片已附加: a.png]" in sent[0]["text"]
    # 记账写回
    assert out["attached_images"] == [
        {"path": str((Path(ws) / "a.png").resolve()), "mime": "image/png"}
    ]

    # 第二次调用（同 turn 工具循环）：记账生效，不再重发
    state2 = {
        "messages": [*state["messages"], AIMessage(content="x")],
        "attached_images": out["attached_images"],
    }
    asyncio.run(node(state2))
    sent2 = model.seen[1][2].content  # [系统, 系统, Human, AIMessage]
    assert sent2[0]["text"].startswith("[图片已在上文发送过")
    assert len(sent2) == 1  # 无图块


def test_llm_node_worker_gate(tmp_path):
    """attach_images=False（worker）：@token 原样透传，不读盘、不记账。"""
    ws = _make_ws(tmp_path)
    model = _CaptureModel()
    node = LLMNode(model=model, workspace_path=ws, attach_images=False)

    out = asyncio.run(node({"messages": [HumanMessage(content="@a.png 看看")]}))

    assert model.seen[0][-1].content == "@a.png 看看"  # str 原样
    assert "attached_images" not in out


def test_llm_node_no_attach_key_when_nothing_attached(tmp_path):
    """没有 @引用时不写 attached_images 键（少一次无意义的 state 覆写）。"""
    ws = _make_ws(tmp_path)
    node = LLMNode(model=_CaptureModel(), workspace_path=ws)

    out = asyncio.run(node({"messages": [HumanMessage(content="普通消息")]}))

    assert "attached_images" not in out
    assert [m.content for m in out["messages"]] == ["好的"]


# ---------------------- 可选的真调用（默认 skip） ----------------------


def test_live_apple_image_through_attach_channel(monkeypatch):
    """tmp/ 下放一张苹果图片，走 @附图通道真打一次 API，模型要认出是苹果。

    相对路径解析（workspace=tmp）也顺带在这条里验掉。默认 skip（花钱 + 网络 + 需视觉模型），
    显式打开：

        THETA_LIVE_VISION=1 .venv/Scripts/python.exe -m pytest tests/test_image_attach.py -k apple

    模型必须能看图（当前主模型 glm-5.3-flash 已支持；前提与实测记录见 test_vision_input.py）。
    """
    if not os.getenv("THETA_LIVE_VISION"):
        pytest.skip("需要 THETA_LIVE_VISION=1 才跑（真打 API，会花钱）")

    import app.agent.nodes as nodes_module

    tmp = Path(__file__).resolve().parents[1] / "tmp"
    images = (
        sorted(
            p
            for p in tmp.iterdir()
            if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
        )
        if tmp.is_dir()
        else []
    )
    if not images:
        pytest.skip("仓库 tmp/ 下没有图片")

    monkeypatch.setattr(
        model_module,
        "settings",
        nodes_module.settings.model_copy(update={"CHAT_MODEL_NAME": "glm-4.6v-flash"}),
    )

    payload, newly = asyncio.run(
        images.attach_images_to_payload(
            [HumanMessage(content=f"@{images[0].name} 这张图里是什么？只回答一个词")], [], str(tmp)
        )
    )
    assert newly, "附图失败：tmp/ 里的文件没能进请求体"

    # 限流/网络抖动 → 重试几次后 skip（环境问题不是被测链路的缺陷，同 test_vision_input）
    import openai

    reply = None
    for attempt in range(4):
        try:
            reply = asyncio.run(LLMNode.main_chat_model().ainvoke(payload))
            break
        except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError) as exc:
            if attempt == 3:
                pytest.skip(f"端点暂时不可用（限流/网络），本次没验成：{type(exc).__name__}: {exc}")
            time.sleep(8)
    text = str(reply.content)
    assert "苹果" in text or "apple" in text.lower(), f"回复里看不出是苹果：{text!r}"
