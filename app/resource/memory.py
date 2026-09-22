"""长期记忆：记忆 md 的读写单点（已落地；"Phase B" 是当时的设计阶段名）。

落点：`resource/<ws_key>/memory/memory.md`（路径经 `app/resource/paths.py::memory_path` 算，
文件名也归那里——模型只给 type/content/key，永远不碰路径，见 LONG_TERM_MEMORY.md §5）。
文件形态见 §3：

    # 长期记忆（工作区 <ws_key>）
    <说明行>
    <!-- 追加格式示例：
    ## [m1] decision · 2026-09-09
    -->
    ## [m1] user-preference · 2026-09-08
    用户要求：输出与注释用中文。

两条纪律：

- **解析必须跳过 HTML 注释**。模板刻意把"追加格式示例"放在注释里，而示例正文本身就含一行
  `## [m1] …`：朴素按标题正则扫描会把示例当真实条目——编号凭空 +1、按 key 覆盖还可能改到
  注释里那行。本模块用 `_visible_segments` 先切出"注释外区间"，只在其中认标题。
- **追加写原始文本**（注释原样保留），**覆盖写只替换注释外那一段 span**。

本模块只依赖 stdlib + `app.resource.paths` + `app.schema`（不碰 app.agent 内部），可被工具层
（app.agent.tools）、基座（app.platform.runtime 的播种）与测试直接 import。日期一律作参量传入
（工具侧传 date.today()），保证纯函数好测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.config import TEXT_BUDGET_CHARS
from app.resource.paths import memory_path, workspace_key
from app.schema.agent_schema import MemoryEntry

# 注入用整份 md 的字符上限（docs/LONG_TERM_MEMORY.md §3 暂定 6–8k）。超限只注入"最新的、
# 装得下的条目"+ 一行省略提示。**值归 app/config.py::TEXT_BUDGET_CHARS**（同族四个预算一处出处）。
MEMORY_INJECT_CAP = TEXT_BUDGET_CHARS

# 条目标题行：`## [m3] decision · 2026-09-08`
_HEADING_RE = re.compile(r"^## \[(?P<key>m\d+)\]\s*(?P<type>[^·]*?)\s*·\s*(?P<date>\d{4}-\d{2}-\d{2})\s*$")
# 连续空行收敛（剥注释后清理用）
_BLANK_RUN_RE = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class _Span:
    """条目在原始文本里的位置：标题行 [start, body_start)，正文 [body_start, end)。"""

    start: int
    body_start: int
    end: int
    entry: MemoryEntry


# ---------------------- 模板播种 ----------------------


def _template(key: str) -> str:
    return (
        f"# 长期记忆（工作区 {key}）\n"
        "\n"
        "记录跨会话仍稳定的 偏好 / 决策 / 约定 / 项目事实；可随时从工作区或 git 重取的内容不记。\n"
        "类型：user-preference / decision / convention / project-fact。\n"
        "<!-- 追加格式（write_memory 自动分配编号并带日期，追加在末尾）：\n"
        "## [m1] decision · 2026-09-09\n"
        "一句话结论，可附出处。\n"
        "-->\n"
    )


def ensure_memory_template(workspace_path: str) -> Path:
    """为工作区播种 memory.md 模板（缺失才写，幂等）；返回其路径。"""
    path = memory_path(workspace_path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_template(workspace_key(workspace_path)), encoding="utf-8")
    return path


# ---------------------- 解析（注释安全） ----------------------


def _visible_segments(text: str) -> list[tuple[int, int]]:
    """切出 text 中**不在 HTML 注释内**的连续区间（偏移对）。

    未闭合的 `<!--` 之后全部视作注释（模板正常闭合，这里只是不崩）。
    """
    segments: list[tuple[int, int]] = []
    pos = 0
    while True:
        start = text.find("<!--", pos)
        if start == -1:
            segments.append((pos, len(text)))
            return segments
        segments.append((pos, start))
        end = text.find("-->", start + 4)
        if end == -1:
            return segments
        pos = end + 3


def _iter_headings(text: str, seg_start: int, seg_end: int):
    """在注释外区间里逐行找条目标题，产出 (行首偏移, 行尾偏移, 解析结果)。

    区间可能从行中间开始（`<!-- c -->## [m1] …` 同一行）：不在行首的标题不算数，
    先跳到下一个换行，避免把紧跟注释闭合符的伪标题当条目。
    """
    if seg_start > 0 and text[seg_start - 1] != "\n":
        nl = text.find("\n", seg_start, seg_end)
        if nl == -1:
            return
        seg_start = nl + 1

    line_start = seg_start
    while line_start < seg_end:
        nl = text.find("\n", line_start, seg_end)
        line_end = seg_end if nl == -1 else nl
        match = _HEADING_RE.match(text[line_start:line_end])
        if match:
            yield line_start, line_end, match
        if nl == -1:
            return
        line_start = nl + 1


def _scan_spans(text: str) -> list[_Span]:
    """扫描原始文本，返回全部条目及其 raw 偏移（注释里的示例不算）。"""
    hits: list[tuple[int, int, re.Match[str]]] = []
    for seg_start, seg_end in _visible_segments(text):
        hits.extend(_iter_headings(text, seg_start, seg_end))

    spans: list[_Span] = []
    for i, (start, line_end, match) in enumerate(hits):
        body_start = line_end + 1 if line_end < len(text) else len(text)
        # 本条正文到"下一条目标题行首"为止；末条到文件尾
        end = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        spans.append(
            _Span(
                start=start,
                body_start=body_start,
                end=end,
                entry=MemoryEntry(
                    key=match.group("key"),
                    type=match.group("type").strip(),
                    date=match.group("date"),
                    content=text[body_start:end].strip(),
                ),
            )
        )
    return spans


def parse_entries(text: str) -> list[MemoryEntry]:
    """解析出全部真实条目（注释里的格式示例被跳过）。"""
    return [span.entry for span in _scan_spans(text)]


def strip_comments(text: str) -> str:
    """剥掉 HTML 注释，返回"给模型看"的可见文本（保留标题、说明行与条目正文）。

    注释整行被拿掉后会留下多余空行，这里把 3 个以上连续换行收敛成 2 个（一个空行），
    让注入/读取出去的样子干净些。
    """
    visible = "".join(text[start:end] for start, end in _visible_segments(text))
    return _BLANK_RUN_RE.sub("\n\n", visible)


def next_key(entries: list[MemoryEntry]) -> str:
    """下一个可用编号：现有最大号 +1（示例注释不影响编号，见模块 docstring）。"""
    max_n = 0
    for entry in entries:
        if entry.key.startswith("m") and entry.key[1:].isdigit():
            max_n = max(max_n, int(entry.key[1:]))
    return f"m{max_n + 1}"


# ---------------------- 读 / 写 ----------------------


def read_text(workspace_path: str) -> str:
    """读记忆原文；文件不存在返回空串（不隐式建文件——建由 ensure_memory_template 负责）。"""
    path = memory_path(workspace_path)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def read_entries(workspace_path: str) -> list[MemoryEntry]:
    """读并解析某工作区的全部条目。"""
    return parse_entries(read_text(workspace_path))


def entry_keys(workspace_path: str) -> list[str]:
    """现有条目编号清单（如 ["m1","m2"]）——工具在"key 不存在"的回执里列给模型看。"""
    return [entry.key for entry in read_entries(workspace_path)]


def render_entry(entry: MemoryEntry) -> str:
    """把一条记忆渲染成文件里的 markdown 段落。"""
    return f"## [{entry.key}] {entry.type} · {entry.date}\n{entry.content}\n"


def append_entry(workspace_path: str, type: str, content: str, today: str) -> MemoryEntry:
    """追加一条记忆（key 系统按现有最大号 +1 分配，日期取 today）；返回写入的条目。

    写的是**原始文本**：模板里的说明与 HTML 注释原样保留，新条目接在文件末尾。
    """
    path = ensure_memory_template(workspace_path)  # 工作区没播过种（如评估场景）也不至于丢写
    text = path.read_text(encoding="utf-8")
    entry = MemoryEntry(
        key=next_key(parse_entries(text)),
        type=type.strip(),
        date=today,
        content=content.strip(),
    )
    path.write_text(text.rstrip() + "\n\n" + render_entry(entry), encoding="utf-8")
    return entry


def memory_block(workspace_path: str, cap: int = MEMORY_INJECT_CAP) -> str:
    """把某工作区的长期记忆渲染成注入用的 SystemMessage 正文（没有条目就返回空串）。

    注入 §4 的"每轮读一次"入口：调用方（LLMNode）读盘即用、不做缓存——记忆文件就是真值。

    - 注入**剥掉 HTML 注释**的可见文本：模板里的"格式示例"是给人看的，不给模型；
    - 内容不超过 `cap` → 原样（trim 后）返回；
    - 超过 `cap` → **从最新条目往前装**，装不下的旧条目退化成一行"未注入 + 编号清单"，
      让模型知道还有更早的记忆、需要时用 read_memory 按编号取回（§3 的截断策略，
      不做压缩/归档——那是 Phase C）。
    """
    text = read_text(workspace_path)
    entries = parse_entries(text)
    if not entries:
        return ""

    visible = strip_comments(text).strip()
    if len(visible) <= cap:
        return visible

    header = visible.split("\n## [", 1)[0].rstrip()
    kept: list[str] = []
    omitted: list[str] = []
    budget = cap - len(header)
    for entry in reversed(entries):  # 从最新往前装：旧条目先被挤出去
        block = render_entry(entry).strip()
        if len(block) <= budget:
            kept.append(block)
            budget -= len(block)
        else:
            omitted.append(entry.key)

    parts = [header]
    if omitted:
        keys = "、".join(sorted(omitted, key=lambda k: int(k[1:])))
        parts.append(f"（更早的 {len(omitted)} 条因长度上限未注入：{keys}；需要时用 read_memory 按编号取回）")
    if kept:
        parts.append("\n\n".join(reversed(kept)))  # 还原成"旧→新"的阅读顺序
    return "\n\n".join(parts)


def overwrite_entry(
    workspace_path: str, key: str, type: str, content: str
) -> MemoryEntry | None:
    """按 key 覆写一条记忆的 type 与正文（保留原 key 与原日期）；key 不存在返回 None。

    只替换该条目在**注释外**的那一段 span——注释里的格式示例不会被误伤。
    """
    path = memory_path(workspace_path)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    wanted = (key or "").strip()
    span = next((s for s in _scan_spans(text) if s.entry.key == wanted), None)
    if span is None:
        return None

    entry = MemoryEntry(
        key=span.entry.key,
        type=type.strip(),
        date=span.entry.date,  # 日期 = 首次记入日，改写不重排
        content=content.strip(),
    )
    # span 尾部含"本条与下一条之间的空行"，被 render_entry 吃掉了要补回来，
    # 否则覆写会把两条记忆挤成相邻行（末条则不留多余空行）。
    tail = text[span.end :]
    updated = text[: span.start] + render_entry(entry) + ("\n" if tail else "") + tail
    path.write_text(updated, encoding="utf-8")
    return entry
