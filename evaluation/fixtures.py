"""录制产物的落点与读写：一份 fixture = "模型当时吐了什么"的存档。

落点 `evaluation/fixtures/<task>.json`（与 `tests/fixtures/` 平级、互不相干）。

**为什么连同 prompt / setup 一起存**：回放时拿它们与当前任务做**指纹比对**，不一致就拒绝回放
（`resolve` 返回"任务已改动，需重录"）。理由是陈旧的 fixture 会拿着**旧问题的答案**为新问题背书
——那种"静默通过"比直接失败糟得多。

**为什么 tool_calls 的 `id` 一个都不能丢**：ReviewNode 拿它兑现 `ToolMessage`、提问闸门按它索引
`ask_answers`，丢了 id 回放会走成另一条路（于是测的就不是生产行为）。`AIMessage` 的序列化在这里
是**只取 content + tool_calls**：思考内容本来就不进 messages（见 app/agent/model.py），
additional_kwargs 也不参与图的行为。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from langchain_core.messages import AIMessage

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class FixtureError(Exception):
    """fixture 文件存在但不可用（JSON 坏了 / 结构不对 / 一条回复都没有）。"""


@dataclass(frozen=True)
class Fixture:
    """一份录制：任务输入（用于比对）+ 模型的输出序列。"""

    task: str
    prompt: str = ""
    setup: dict[str, str] = field(default_factory=dict)
    replies: list[AIMessage] = field(default_factory=list)
    model: str = ""
    recorded_at: str = ""


def path_for(task_name: str) -> Path:
    return FIXTURES_DIR / f"{task_name}.json"


def _reply_to_dict(message: AIMessage) -> dict:
    return {"content": message.content, "tool_calls": list(message.tool_calls or [])}


def _reply_from_dict(raw: object) -> AIMessage:
    if not isinstance(raw, dict):
        raise FixtureError(f"replies 的元素必须是对象，收到 {type(raw).__name__}")
    content = raw.get("content") or ""
    tool_calls = raw.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        raise FixtureError("tool_calls 必须是数组")
    return AIMessage(content=content, tool_calls=tool_calls)


def save(fixture: Fixture) -> Path:
    """把一份录制写进 `evaluation/fixtures/<task>.json`（覆盖），返回落点路径。"""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    target = path_for(fixture.task)
    payload = {
        "task": fixture.task,
        "recorded_at": fixture.recorded_at or datetime.now().isoformat(timespec="seconds"),
        "model": fixture.model,
        "prompt": fixture.prompt,
        "setup": fixture.setup,
        "replies": [_reply_to_dict(m) for m in fixture.replies],
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def load(task_name: str) -> Fixture:
    """读一份 fixture；文件不存在 → `FixtureError`（调用方按"未录制"处理）。"""
    target = path_for(task_name)
    if not target.is_file():
        raise FixtureError(f"未录制：{target} 不存在")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"fixture 读不动（{type(exc).__name__}: {exc}）") from exc
    if not isinstance(payload, dict):
        raise FixtureError("fixture 顶层必须是对象")
    replies = [_reply_from_dict(item) for item in payload.get("replies") or []]
    if not replies:
        raise FixtureError("fixture 里一条回复都没有（录制那次跑失败了吗？）")
    setup = payload.get("setup") or {}
    if not isinstance(setup, dict):
        raise FixtureError("setup 必须是对象")
    return Fixture(
        task=str(payload.get("task") or task_name),
        prompt=str(payload.get("prompt") or ""),
        setup={str(k): str(v) for k, v in setup.items()},
        replies=replies,
        model=str(payload.get("model") or ""),
        recorded_at=str(payload.get("recorded_at") or ""),
    )


def stale_reason(fixture: Fixture, prompt: str, setup: dict[str, str]) -> str | None:
    """fixture 与当前任务是否匹配；不匹配就返回一句可行动的说明（None = 一致）。

    比的是**任务的输入**（prompt + setup）：输入变了，录制下来的输出就不再是对"这个问题"的回答。
    """
    if fixture.prompt != prompt:
        return "任务已改动（prompt 与录制时不一致），需 --record 重录"
    if fixture.setup != {str(k): str(v) for k, v in setup.items()}:
        return "任务已改动（setup 与录制时不一致），需 --record 重录"
    return None


def resolve(task_name: str, prompt: str, setup: dict[str, str]) -> tuple[Fixture | None, str | None]:
    """为回放找一个可用 fixture：命中返回 `(fixture, None)`，否则 `(None, 不能用的原因)`。

    三种"不能用"由调用方统一翻成 SKIP（不计入通过率）：未录制 / 文件损坏 / 任务已改动。
    """
    try:
        fixture = load(task_name)
    except FixtureError as exc:
        return None, str(exc)
    reason = stale_reason(fixture, prompt, setup)
    if reason is not None:
        return None, reason
    return fixture, None
