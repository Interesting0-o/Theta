from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

from pydantic import BaseModel


class ToolResult(BaseModel):
    success: bool
    content: str
    error_type: str | None = None


# 计划每步的状态枚举（与运行时 current_plan 的元素字段保持一致）
PlanStatus = Literal["pending", "in_progress", "done"]


class PlanStep(TypedDict):
    # 创建时分配的序号（"1","2",...）
    id: str
    task: str
    status: PlanStatus


class NoteEntry(TypedDict):
    """会话级笔记条目（外层 dict 的 key 即 ref，如 "notes#r3"；随 checkpoint 存亡）。

    定位：给"花钱、不可免费重取"的大结果（联网检索等）一个指针常驻、正文按需取回的落点，
    让折叠可以更激进。不做跨会话语义库——值钱结论由 agent 主动 promote 进 repo。
    kind 约定：research / web / crawl / extract / command_output …（系统按 kind + 体积阈值归档）。
    topic/tags：归档时留空，需整理时由模型调 update_note_tags / notes_by_topic 懒分类。
    created：unix 秒；size：content 的字符数；content：markdown payload（消费者是模型）。
    """
    kind: str
    title: str
    source_tool: str
    content: str
    created: int
    size: int
    topic: NotRequired[str]
    tags: NotRequired[list[str]]


@dataclass(frozen=True)
class MemoryEntry:
    """长期记忆的一条（`resource/<ws_key>/memory/memory.md` 里的一段，跨会话长存）。

    与 PlanStep / NoteEntry 的差别在**载体**：那两个是会话内的 state 切片、要经 checkpoint
    序列化，所以用 TypedDict；MemoryEntry 是解析记忆文件得到的值对象，从不进 state，
    因此用 frozen dataclass——不可变、按属性访问，四要素即文件里的一行标题 + 一段正文。

    - key：单调递增编号（m1, m2, …），供 read_memory 取单条 / write_memory 按 key 覆写；
    - type：user-preference / decision / convention / project-fact（见 §6）；
    - date：**首次记入日** YYYY-MM-DD——覆写保留原日期，不重排、不重新编号；
    - content：条目正文（不含标题行）。

    设计见 docs/LONG_TERM_MEMORY.md §3；读写与解析在 app/agent/memory.py。
    """

    key: str
    type: str
    date: str
    content: str