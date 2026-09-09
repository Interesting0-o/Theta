"""评估驱动器：把 TUI 的人工循环改成"审批策略 + 断言"的批量循环。

app/main.py 的循环是评估的原型——这里唯一的变化是把 `input()` 换成 policy 函数：

    while True:
        result = await graph.ainvoke(inputs, config)
        if "__interrupt__" in result:               # ReviewNode 挂起等审批
            approved = policy(payload)              # 模拟人按 y/n
            inputs = Command(resume={"approved": approved})
            continue
        break

与 TUI 的差异：
- 图用 InMemorySaver 编译（不落 resource/agent.db，评估不污染正式会话）；
- thread_id 按任务名区分，checkpoint 互不串扰；
- 每任务一个隔离临时工作区（setup 预置文件），作为 workspace_path 实参传给
  get_main_agent_graph，天然受 file_io 沙箱约束；
- 加 max_rounds 上限：拒绝路径若死循环，评估必须能报错收场而不是挂死。

已知代价：每任务建图会各自拉起一次 MCP 子进程组（mcp.py 有按工作区的进程级
缓存，同工作区重复评估只付一次握手成本）。TODO：按工作区分组复用编译图。

TODO（层 A）：无 LLM 成本的录制回放——把真实运行的 tool_calls 序列存下来，
用 tests/ 里"假模型 + scripted AIMessage"的模式回放（tests/test_review_routing.py），
断言路由/审批/终态，作为 prompt/工具 schema 改动的快速回归。
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agent.graph import get_main_agent_graph
from app.agent.state import AgentState

from .policy import Policy
from .tasks import Task


@dataclass(frozen=True)
class InterruptRecord:
    """一次审批中断的记录：给报告统计审批次数、审计"批了什么"。"""

    tool_name: str
    approved: bool
    payload: Dict[str, Any]


@dataclass
class EvalResult:
    """单任务单次运行的原始结果（检查项在 checks.py，不在此处评分）。

    TODO：从最后一条 AIMessage 的 response_metadata 聚合 token 用量与延迟细分。
    """

    task_name: str
    ok: bool
    final_state: Optional[Dict[str, Any]] = None
    interrupts: List[InterruptRecord] = field(default_factory=list)
    rounds: int = 0
    elapsed_seconds: float = 0.0
    error: Optional[str] = None


async def build_eval_graph(workspace: Path):
    """按评估工作区编译图（InMemorySaver，不碰正式 agent.db）。

    工作区作为 workspace_path 实参传给 get_main_agent_graph：工作区注入、MCP 子进程
    拉起（子进程 env 由父进程据此注入）、沙箱校验都走现有链路。
    """
    graph = await get_main_agent_graph(str(workspace))
    return graph.compile(checkpointer=InMemorySaver())


def apply_setup(workspace: Path, setup: Dict[str, str]) -> None:
    """把 task.setup 的预置文件写进工作区（相对路径建目录）。"""
    for rel_path, content in setup.items():
        target = workspace / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _extract_payload(interrupts) -> Tuple[Any, Dict[str, Any]]:
    """从 result["__interrupt__"] 取最后一个 Interrupt 的 value 字典。

    一次挂起只处理一条（与 TUI 一致：恢复后 review_node 会为下一条再次 interrupt）。
    """
    item = interrupts[-1]
    payload = getattr(item, "value", item)
    if not isinstance(payload, dict):
        payload = {"tool_name": "unknown", "raw": str(payload)}
    return item, payload


async def run_task(
    graph,
    task: Task,
    workspace: Path,
    policy: Optional[Policy] = None,
) -> EvalResult:
    """在已预置的工作区里跑一个任务，返回原始 EvalResult（不做评分）。

    graph 为编译后的图（build_eval_graph 产出）；policy 缺省取 task.policy。
    """
    policy = policy or task.policy
    result = EvalResult(task_name=task.name, ok=False)
    started = time.monotonic()

    state: dict | AgentState = {
        "session_id": task.name,
        "messages": [HumanMessage(content=task.prompt)],
        "pending_tool_calls": [],
        "approved_tool_calls": [],
        "current_plan": [],
    }
    config = {"configurable": {"thread_id": f"eval::{task.name}"}}

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
                _, payload = _extract_payload(interrupts)
                approved = bool(policy(payload))
                result.interrupts.append(
                    InterruptRecord(
                        tool_name=payload.get("tool_name", "unknown"),
                        approved=approved,
                        payload=payload,
                    )
                )
                inputs = Command(resume={"approved": approved})
                continue

            result.ok = True
            result.final_state = output
            return result
    except Exception as exc:  # noqa: BLE001 —— 评估任务失败要记下来，不能炸掉整批
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        result.elapsed_seconds = time.monotonic() - started


async def run_suite(
    tasks: List[Task],
    workspace_root: Optional[Path] = None,
    keep_workspace: bool = False,
) -> List[Tuple[Task, EvalResult, Path]]:
    """跑一批任务：每任务隔离临时工作区，返回 (task, result, workspace)。

    keep_workspace=True 时保留工作区（供人工复查失败现场）；否则任务结束即清理。
    """
    root = (workspace_root or Path(tempfile.gettempdir())).resolve()
    root.mkdir(parents=True, exist_ok=True)

    outcomes: List[Tuple[Task, EvalResult, Path]] = []
    for task in tasks:
        workspace = Path(tempfile.mkdtemp(prefix=f"eval-{task.name}-", dir=root))
        try:
            apply_setup(workspace, task.setup)
            graph = await build_eval_graph(workspace)
            result = await run_task(graph, task, workspace)
            outcomes.append((task, result, workspace))
        finally:
            if not keep_workspace:
                shutil.rmtree(workspace, ignore_errors=True)
    return outcomes
