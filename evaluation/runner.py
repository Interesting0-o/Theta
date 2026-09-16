"""评估驱动器：把 TUI 的人工循环改成"审批策略 + 断言"的批量循环。

app/platform/turn.py::drive_turn 的 park/resume 是评估的原型——这里唯一的变化是把人工决定的
那一步换成 policy 函数：

    while True:
        result = await graph.ainvoke(inputs, config)
        if "__interrupt__" in result:               # ReviewNode 挂起等闸门
            decision = policy(payload)              # 模拟人给决定（审批 y/n 或提问的回答）
            inputs = Command(resume=decision_to_resume(decision))
            continue
        break

**三种模式共用上面这一个循环**，区别只在"喂给图的模型是谁"（`app/agent/models.py` 的说明）：

| mode | 模型 | 成本 | 用途 |
| --- | --- | --- | --- |
| `replay`（默认） | `ReplayChatModel`（脚本） | **零 token** | 层 A：拿**真实录制的模型输出**当固定输入样本，断言"除模型之外的一切"没变 |
| `live` | 生产模型 | 真 token | 层 B：看模型这次表现如何（prompt / docstring 改动的**唯一**验证手段） |
| `record` | 生产模型 + `RecordingCallback` | 真 token | 跑一次并把模型输出存成 fixture，供日后 replay |

⚠️ 回放的模型**不读输入**，所以**改了 prompt 必须跑 `live`**——层 A 对这类改动是瞎的。

与 TUI 的其它差异：
- 图用 InMemorySaver 编译（不落 resource/agent.db，评估不污染正式会话）；
- thread_id 按任务名区分，checkpoint 互不串扰；
- 每任务一个隔离临时工作区（setup 预置文件），作为 workspace_path 实参传给
  get_main_agent_graph，天然受 file_io 沙箱约束；
- 加 max_rounds 上限：拒绝路径若死循环，评估必须能报错收场而不是挂死。

已知代价：每任务建图会各自拉起一次 MCP 子进程组（mcp.py 按工作区缓存工具 schema，
实际运行体按 (工作区, 任务名) 懒起、任务结束即关，见 app/agent/mcp.py）。
TODO：按工作区分组复用编译图。
"""
from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agent.graph import get_main_agent_graph
from app.agent.mcp import close_session_pool
from app.agent.state import AgentState
from app.config import get_settings
from app.platform.turn import decision_to_resume

from . import fixtures
from .models import RecordingCallback, ReplayChatModel
from .tasks import Task

MODE_REPLAY = "replay"
MODE_LIVE = "live"
MODE_RECORD = "record"

MODES = (MODE_REPLAY, MODE_LIVE, MODE_RECORD)


@dataclass
class EvalResult:
    """单任务单次运行的原始结果（检查项在 checks.py，不在此处评分）。

    TODO：从最后一条 AIMessage 的 response_metadata 聚合 token 用量与延迟细分。
    """

    task_name: str
    ok: bool
    final_state: Optional[Dict[str, Any]] = None
    # 只留计数：消费者（checks.approvals_between / report 的表格列）要的都只是次数。
    # 曾记过 InterruptRecord(tool_name/approved/payload) 三个字段，全仓无人读——要做
    # "批了什么"的审计时再按当时的需要加回来。
    interrupt_count: int = 0
    rounds: int = 0
    elapsed_seconds: float = 0.0
    error: Optional[str] = None
    # 这一轮**压根没跑**（回放模式下 fixture 缺失/损坏/与任务不一致）。不计入通过率——它不是
    # 模型的锅；但也不能藏：报告里单列一行写出原因与"怎么补"。
    skipped: bool = False
    skip_reason: Optional[str] = None


def skipped_result(task: Task, reason: str) -> EvalResult:
    return EvalResult(task_name=task.name, ok=False, skipped=True, skip_reason=reason)


async def build_eval_graph(workspace: Path, session: str, model: BaseChatModel | None = None):
    """按评估工作区编译图（InMemorySaver，不碰正式 agent.db）。

    工作区作为 workspace_path 实参传给 get_main_agent_graph：工作区注入、MCP 子进程
    拉起（子进程 env 由父进程据此注入）、沙箱校验都走现有链路。

    模型经 `model=` 构造参量注入：回放传脚本模型，其余模式传 None（用生产模型）。

    session 决定 MCP 运行体的作用域（每任务一组，key=(工作区, 会话)）；调用方必须在任务结束时
    `close_session_pool`，否则常驻子进程会跨任务累积。
    """
    graph = await get_main_agent_graph(str(workspace), session_id=session, model=model)
    return graph.compile(checkpointer=InMemorySaver())


def apply_setup(workspace: Path, setup: Dict[str, str]) -> None:
    """把 task.setup 的预置文件写进工作区（相对路径建目录）。"""
    for rel_path, content in setup.items():
        target = workspace / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _extract_payload(interrupts) -> Dict[str, Any]:
    """从 result["__interrupt__"] 取最后一个 Interrupt 的 value 字典。

    一次挂起只处理一条（与 TUI 一致：恢复后 review_node 会为下一条再次 interrupt）。
    """
    payload = getattr(interrupts[-1], "value", interrupts[-1])
    if not isinstance(payload, dict):
        payload = {"tool_name": "unknown", "raw": str(payload)}
    return payload


async def run_task(graph, task: Task, workspace: Path, config_extra: dict | None = None) -> EvalResult:
    """在已预置的工作区里跑一个任务，返回原始 EvalResult（不做评分）。

    graph 为编译后的图（build_eval_graph 产出）；审批策略取 task.policy（每任务自带）。
    config_extra 并入 ainvoke 的 config——录制模式就是靠它把 `RecordingCallback` 挂上去。
    """
    policy = task.policy
    result = EvalResult(task_name=task.name, ok=False)
    started = time.monotonic()

    state: dict | AgentState = {
        "session_id": task.name,
        "messages": [HumanMessage(content=task.prompt)],
        "pending_tool_calls": [],
        "approved_tool_calls": [],
        "current_plan": [],
    }
    config = {"configurable": {"thread_id": f"eval::{task.name}"}, **(config_extra or {})}

    inputs = state
    try:
        while True:
            result.rounds += 1
            if result.rounds > task.max_rounds:
                result.error = f"超过最大轮数 {task.max_rounds}（疑似拒绝路径死循环）"
                return result

            output = await graph.ainvoke(inputs, config)

            interrupts = output.get("__interrupt__") or []
            if interrupts:
                payload = _extract_payload(interrupts)
                decision = policy(payload)
                result.interrupt_count += 1
                # resume 值经**与基座同一个映射**造（app/platform/turn.py::decision_to_resume）：
                # 评估绕过 UI 协议，但不该绕过图侧的契约——否则审批/提问的形状会各写一份。
                inputs = Command(resume=decision_to_resume(decision))
                continue

            result.ok = True
            result.final_state = output
            return result
    except Exception as exc:  # noqa: BLE001 —— 评估任务失败要记下来，不能炸掉整批
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        result.elapsed_seconds = time.monotonic() - started


def _record_fixture(task: Task, replies: list) -> str | None:
    """把录制结果写盘；返回落点路径（失败返回 None 并打一行提示）。

    **只在跑成功时写**：半途报错的 run 录下来的序列是残的，存下来只会让人以为"这个任务录过了"。
    """
    if not replies:
        print(f"  ⚠️ {task.name}: 一条模型输出都没录到，未生成 fixture")
        return None
    path = fixtures.save(
        fixtures.Fixture(
            task=task.name,
            prompt=task.prompt,
            setup=dict(task.setup),
            replies=list(replies),
            model=get_settings().CHAT_MODEL_NAME,
        )
    )
    return str(path)


async def run_suite(
    tasks: List[Task],
    workspace_root: Optional[Path] = None,
    mode: str = MODE_REPLAY,
) -> List[Tuple[Task, EvalResult, Optional[Path]]]:
    """跑一批任务：每任务隔离临时工作区，返回 (task, result, workspace)。

    mode 见模块 docstring（replay / live / record）。回放模式下 fixture 不可用的任务**不跑**，
    直接产出 skipped 结果（workspace 为 None）——报告里单列，不计入通过率。

    ⚠️ **本函数不删工作区**：检查项（`checks.py`）要读终态文件，而它们在报告阶段才跑，删早了
    文件类断言会**恒失败**（2026-09-16 实跑发现：`--keep-workspace` 才 8/8、默认 6/8）。
    清理归 `cleanup_workspaces()`，由调用方在报告**之后**调。
    """
    if mode not in MODES:
        raise ValueError(f"未知的评估模式 {mode!r}（可用：{' / '.join(MODES)}）")

    root = (workspace_root or Path(tempfile.gettempdir())).resolve()
    root.mkdir(parents=True, exist_ok=True)

    outcomes: List[Tuple[Task, EvalResult, Optional[Path]]] = []
    for task in tasks:
        replay_model: BaseChatModel | None = None
        if mode == MODE_REPLAY:
            fixture, reason = fixtures.resolve(task.name, task.prompt, task.setup)
            if fixture is None:
                outcomes.append((task, skipped_result(task, reason or "无可用 fixture"), None))
                continue
            # 拷一份再交给模型：ReplayChatModel 会 pop 自己的列表
            replay_model = ReplayChatModel(replies=list(fixture.replies))

        workspace = Path(tempfile.mkdtemp(prefix=f"eval-{task.name}-", dir=root))
        try:
            apply_setup(workspace, task.setup)
            graph = await build_eval_graph(workspace, task.name, model=replay_model)
            if mode == MODE_RECORD:
                recorded: list = []
                result = await run_task(
                    graph,
                    task,
                    workspace,
                    config_extra={"callbacks": [RecordingCallback(recorded)]},
                )
                if result.ok:
                    path = _record_fixture(task, recorded)
                    if path is None:
                        result.error = "跑成功但没录到模型输出（回调没挂上？）"
                        result.ok = False
            else:
                result = await run_task(graph, task, workspace)
            outcomes.append((task, result, workspace))
        finally:
            # 只关本任务的常驻 MCP 运行体（其 cwd 就在工作区里）：不关会跨任务累积子进程，
            # 且进程活着时（Windows 上）谁也删不掉这个目录。**删目录不在这里**——检查项还要读它。
            await close_session_pool(str(workspace), task.name)
    return outcomes


def cleanup_workspaces(runs: List[Tuple[Task, EvalResult, Optional[Path]]]) -> None:
    """跑完（含报告里的检查项）之后删掉临时工作区。

    顺序是硬的，两道都不能少：**检查项要读工作区里的终态文件**（所以清理晚于 `print_report`）、
    而常驻 MCP 运行体的 cwd 就在工作区里（所以 `close_session_pool` 早于清理，那个在
    `run_suite` 的 finally 里）。要人工复查失败现场就别调它（CLI 的 `--keep-workspace`）。
    """
    for _task, _result, workspace in runs:
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)
