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