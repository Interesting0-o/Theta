"""@图片路径 的调用期附图通道：**用户消息里的本地图片文件 → 请求体的 data URI 块**。

资源层里的"输入资源"那一支（同包另有 paths / memory / skills / profile）。输入是
"HumanMessage 文本 + 工作区"，输出是"请求体副本 + 记账"——**完全不碰 state**，故从
`LLMNode` 剥出来（2026-09-21，判据见 docs/ARCHITECTURE.md §4 的 A/B/C：读 agent 之外的
东西、转成内部表示；节点只该做"这一拍拼什么、发给谁"）。

**base64 从不进 messages/checkpoint**：state 里的消息始终是带 @路径 的纯文本（本身就是
"看过哪张图"的日志），图只活在**请求体副本**那一瞬；同轮不重发的记账由调用方写回
state["attached_images"]（`ImageRef` 元数据，单调追加）。勘察与方案见 docs/TODO.md「图像输入」。

**一条明确的资源访问政策：不做沙箱拒绝**。`@` 是用户亲手输入的通道，沙箱约束的是模型的
工具调用、不约束用户自己（截图在桌面/下载目录是常态）——所以这里的相对路径以工作区为基准
解析、绝对路径原样放行，都不做 "在工作区内" 的校验。也别 import `mcp_service/file_io.py`
的 `_resolve_path`：那个模块在 import 期就校验 WORKSPACE_PATH env（主进程没设，import 即炸）。
"""
from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path

from langchain_core.messages import HumanMessage

from app.schema.agent_schema import ImageRef

# 扩展名 → data URI 的 mime。命中才算"长得像图片"（宽进策略：不像的 @token 是普通文本，
# 原样放行——@人名、@note.txt 不该被碰）
_IMAGE_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}
_IMAGE_EXT_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|bmp)", re.IGNORECASE)
# 单张体积上限：超限不读盘、给模型一行说明。这是**本地上限**，不是厂商限额（未实测核实）
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
# 一条消息最多附几张，多的只给说明（防 payload 失控）
_MAX_IMAGES_PER_MESSAGE = 4

# read_bytes 失败的哨兵（与"文件不存在"区分：前者给"读取失败"，后者给"未找到"）
_UNREADABLE = object()


def image_tokens(text: str) -> list[tuple[int, int, str, str]]:
    """扫出文本里的 @图片引用：返回 (起始, 结束, 原始路径, mime)，区间即 @token 的原位替换范围。

    mime 在**收词时**一并定下（用命中的那个扩展名反查 _IMAGE_MIME）。调用方别再拿
    `Path(raw).suffix` 反推：那是"最后一个点号之后"的整段，而收词口径是"任意处命中"，
    两者不等价——`@"shot.png "` 的 suffix 是 `.png `、`@"a.png（新版）"` 是 `.png（新版）`，
    反推出来的键根本不在表里。

    - 引号形式 `@"…"`：路径可含空格，整段须有图片扩展名才算引用，否则整个引号段原样放行；
    - 裸 token 到首个空白为止；整 token 不以图片扩展名收尾（中文习惯 `@a.png帮我看` 不打
      空格）时**截到首个图片扩展名处**，余下字符留在正文里；
    - 扩展名不在 _IMAGE_MIME 里的 @token 根本不是引用（@人名、@note.txt），原样放行
      （宽进策略，2026-09-14 与用户议定）。
    """
    tokens: list[tuple[int, int, str]] = []
    i = 0
    while (i := text.find("@", i)) != -1:
        j = i + 1
        if j >= len(text) or text[j].isspace():
            i += 1  # 裸 @ 或后面是空白：不是引用
            continue
        if text[j] == '"':
            end = text.find('"', j + 1)
            if end == -1:  # 引号没闭合：当普通文本
                i += 1
                continue
            raw = text[j + 1 : end]
            m = _IMAGE_EXT_RE.search(raw)
            if m:
                tokens.append((i, end + 1, raw, _IMAGE_MIME[m.group(0)[1:].lower()]))
            i = end + 1  # 无论是不是引用都跳过整个引号段
            continue
        k = j
        while k < len(text) and not text[k].isspace():
            k += 1
        token = text[j:k]
        m = _IMAGE_EXT_RE.search(token)
        if m:
            tokens.append(
                (i, j + m.end(), token[: m.end()], _IMAGE_MIME[m.group(0)[1:].lower()])
            )
            i = j + m.end()  # 从路径结束处继续扫（余下字符回正文，其中的 @ 还能命中）
        else:
            i = k
    return tokens


def resolve_image_path(raw: str, workspace_path: str) -> Path:
    """解析 @路径：相对路径以工作区为基准、绝对路径原样（含 ~ 展开），resolve() 消掉
    `../` 与符号链接。

    只做解析、**不做沙箱拒绝**——`@` 是用户亲手输入的通道（理由见模块 docstring）。
    """
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path(workspace_path) / p
    return p.resolve()


async def attach_images_to_payload(
    messages: list,
    already_attached: list[ImageRef],
    workspace_path: str,
) -> tuple[list, list[ImageRef]]:
    """把最后一条 HumanMessage 里的 @图片引用临时附进**请求体副本**（不动 state）。

    返回 (新消息列表, 本次新附上的 ImageRef)。每个 @token 的处置都写成正文里的状态说明，
    模型自然会把失败转告用户（LLMNode 没有 Notice 通道，让模型当信使）：
    `[图片已附加: …]` / `[图片已在上文发送过…: …]` / `[图片未找到: …]` /
    `[图片过大…: …]` / `[图片读取失败: …]`。base64 只活在本函数的调用栈里；记账由
    调用方写回 state["attached_images"]，下一轮据此不再重发（首次-only 语义；进程重启
    也不怕——记账里的图重读盘即得）。

    只解析 **HumanMessage** 且只是最后一条：AIMessage/ToolMessage 里的 @ 一律不认——
    否则模型输出 `@C:\\…` 就等于模型能指挥宿主读任意本地文件送进 API。
    """
    sent = {ref["path"] for ref in already_attached}
    last_human = max(
        (i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=None
    )
    if last_human is None or not isinstance(messages[last_human].content, str):
        return messages, []
    text = messages[last_human].content
    tokens = image_tokens(text)
    if not tokens:
        return messages, []

    def _load(p: Path):
        if not p.is_file():
            return None
        try:
            return p.read_bytes()
        except OSError:
            return _UNREADABLE

    parts: list[str] = []
    blocks: list[dict] = []
    newly: list[ImageRef] = []
    pos = 0
    for start, end, raw, mime in tokens:
        resolved = resolve_image_path(raw, workspace_path)
        key = str(resolved)
        if key in sent or any(ref["path"] == key for ref in newly):
            marker = f"[图片已在上文发送过，不重复附图: {raw}]"
        elif len(newly) >= _MAX_IMAGES_PER_MESSAGE:
            marker = f"[图片数量超上限（{_MAX_IMAGES_PER_MESSAGE} 张），未附加: {raw}]"
        else:
            data = await asyncio.to_thread(_load, resolved)
            if data is None:
                marker = f"[图片未找到: {raw}]"
            elif data is _UNREADABLE:
                marker = f"[图片读取失败: {raw}]"
            elif len(data) > _MAX_IMAGE_BYTES:
                marker = (
                    f"[图片过大（上限 {_MAX_IMAGE_BYTES // (1024 * 1024)}MB），未附加: {raw}]"
                )
            else:
                b64 = base64.b64encode(data).decode()
                blocks.append(
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
                )
                newly.append(ImageRef(path=key, mime=mime))
                marker = f"[图片已附加: {raw}]"
        parts.append(text[pos:start])
        parts.append(marker)
        pos = end
    parts.append(text[pos:])

    out = list(messages)
    out[last_human] = HumanMessage(
        content=[{"type": "text", "text": "".join(parts)}, *blocks]
    )
    return out, newly
