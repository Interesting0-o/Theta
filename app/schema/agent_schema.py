from typing import Literal, TypedDict
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
