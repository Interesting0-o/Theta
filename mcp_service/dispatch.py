"""派发子任务 MCP server（stdio）：`dispatch_subtasks` —— 主 agent 的"并发资料收集"入口。

**为什么在这里、而不在 `app/agent/tools.py`**（2026-09-21 拍板，理由见 docs/ARCHITECTURE.md §4
的四问与 docs/TODO.md 该条）：它派生 worker —— **独立进程 + 独立 Agent runtime**，命中四问的
**第四问**（multi-agent 正落在这里）。它与 `mcp_service/sub_agent.py` 是**一对**：一个拉起、
一个被拉起；worker 既然住在 MCP 侧，拉起它的入口就该在同一侧。留在 `app/agent/` 的写法有个
说不通的理由——"因为需要注入工作区"：那是**处境**（住主进程必然靠注入），不是结构
（`file_io` 同样需要工作区，却是普通工具，因为工作区就是它的启动配置）。

⚠️ **不走 `InjectedWorkspace` 了**：工作区改从**启动 env `WORKSPACE_PATH`** 取（与 file_io /
sub_agent 同一口径），由 `app/platform/mcp.py::_build_servers` 在建运行体时塞进 `env`。

⚠️ **`AGENT_INBOX_URL` 的转发链多了一跳**：worker 的审批要回主侧的收件箱，而 worker 子进程的
env 由本模块内的 `_worker_child_env` 组装（子进程 env 是**整体替换**、不继承）。完整链条是

    主进程 env →（stdio_connection 的 extra_env）→ 本 server 的 env →（_worker_child_env）→ worker

**漏掉中间那跳不会报错，只会静默发往默认端口**——所以 `_build_servers` 必须显式把这个键透传进来
（`app/platform/mcp.py`）。主侧启动收件箱后会把**实际**地址写进 `os.environ["AGENT_INBOX_URL"]`。

审批语义不变：**派发本身免审**（`tool.json` 里 `need_review: false`），worker 侧真正有副作用的
调用各自过闸门、经主侧统一 broker 人工批准（跨进程回传，见 sub_agent.py）。
本 server **不进 worker 可用工具集**（`source: mcp_service/dispatch` 不命中 `worker_tools` 的两条
source 规则、也没有 `worker_allow`）——那正是"递归派发"的防线，别顺手把它的 source 加进
`_WORKSPACE_SOURCES`。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from app.exception import ConfigError, InvalidArgumentError
from app.resource.paths import PROJECT_ROOT
from app.schema.agent_schema import ToolResult
from mcp_service.utils import guard

logger = logging.getLogger(__name__)

mcp = FastMCP("Dispatch")

# 项目根：spawn worker 子进程时 cwd 用它，使 worker 能读到项目 .env（worker 内懒加载模型）。
# **不能靠 cwd 反推**：stdio_connection 给 server 设的 cwd 是**工作区**（见 app/platform/mcp.py）。
# 真值归 app/resource/paths.py（只依赖 stdlib，子进程 import 得起）。


def _resolve_workspace() -> str:
    """取并校验本 server 的工作区（启动 env WORKSPACE_PATH）。缺失/非目录 → ConfigError。

    与 `mcp_service/sub_agent.py::_resolve_workspace` 同形——两侧各校验各自的启动配置，是
    "每个 server 只认自己 env"的既有口径（去重与否见 docs/TODO.md「减法审计遗留」里那条
    WORKSPACE_PATH 合并待议项）。
    """
    raw = os.environ.get("WORKSPACE_PATH", "")
    if not raw or not raw.strip():
        raise ConfigError("WORKSPACE_PATH 未设置（无法定位派发的工作区）")
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise ConfigError(f"派发工作区 {path} 不存在")
    return str(path)


def _worker_child_env(workspace: str) -> dict[str, str]:
    """worker 子进程的 env。

    子进程 env 是**整体替换**（不继承父进程），所以该带的都得显式带上：
    - `WORKSPACE_PATH`：worker 只读干活的工作区；
    - `PYTHONPATH`：cwd 已切到项目根，靠它保证 `python -m mcp_service.*` 能 import 到包；
    - `AGENT_INBOX_URL`：**主侧审批收件箱的实际地址**——由主侧启动收件箱时写进自己的 env、
      再经 `_build_servers` 透传进本 server 的 env（见模块 docstring 的转发链）。只在确实
      拿到地址时才带（空值不传：worker 那边把空串当"未设置"处理，但传个空变量本身就是噪声）。
    """
    env = {"WORKSPACE_PATH": str(workspace), "PYTHONPATH": str(PROJECT_ROOT)}
    inbox_url = os.environ.get("AGENT_INBOX_URL", "").strip()
    if inbox_url:
        env["AGENT_INBOX_URL"] = inbox_url
    return env


async def _spawn_subagent_worker(task: str, workspace: str) -> str:
    """默认 worker 执行器：现场 spawn 一个 sub_agent stdio 子进程跑 run_subtask。

    langchain_mcp_adapters 的 get_tools 工具是"每次调用开一个新会话"：每次 ainvoke 拉起
    一个 `python -m mcp_service.sub_agent` 子进程、调用 run_subtask、结束后回收，因此
    N 次并发 = N 个独立 worker 进程，无预启动、无常驻泄漏。cwd=项目根使 worker 进程能
    读到项目 .env 的 CHAT_*；WORKSPACE_PATH 指向本 server 的工作区（worker 在其上只读干活）。
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: PLC0415

    client = MultiServerMCPClient(
        {
            "worker": {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "mcp_service.sub_agent"],
                "cwd": str(PROJECT_ROOT),
                "env": _worker_child_env(workspace),
            }
        }
    )
    tools = await client.get_tools()
    run_subtask = next(t for t in tools if t.name == "run_subtask")
    result = await run_subtask.ainvoke({"task": task})
    # MCP adapters 返回 content-block 形态 → 归一成模型可见正文，空正文兜底
    from app.platform.tool_results import format_tool_result  # noqa: PLC0415

    return format_tool_result(result) or "（worker 未返回正文）"


# 模块级可替换的 worker 执行器；None = 用默认 _spawn_subagent_worker（测试注入假执行器）
worker_runner = None


async def run_subtask_batch(
    sub_tasks: list[str],
    workspace: str,
    runner=None,
) -> list[dict]:
    """并发跑一批资料收集子任务；每个子任务一个独立 worker（默认 spawn sub_agent 进程）。

    返回按输入顺序排列的 [{task, ok, content}]；单 worker 失败不拖垮整批
    （记 ok=False，content 带原因），由调用方决定是否重试/换法。
    """
    run = runner or worker_runner or _spawn_subagent_worker

    async def _one(task: str) -> dict:
        try:
            content = await run(task, workspace)
            return {"task": task, "ok": True, "content": str(content)}
        except Exception as exc:  # noqa: BLE001 —— worker 进程失败等，收口为条目
            return {"task": task, "ok": False, "content": f"worker 执行失败：{exc}"}

    return list(await asyncio.gather(*(_one(t) for t in sub_tasks)))


@mcp.tool()
@guard
async def dispatch_subtasks(sub_tasks: list[str]) -> ToolResult:
    """把多个**相互独立**的查证/调研问题，并行派给一批一次性 worker 子 agent 快速收集资料，
    拿回每个的结论正文。

    何时用：当前任务需要"先并行搜集一堆互不相关的资料/事实"时——例如分别调研工作区里几个
    模块各自怎么实现、分别查几份 API 文档/报错资料、分别读几块代码的职责。把每个独立问题写成
    一条 sub_task 一次派出，由系统对每条起一个独立 worker 进程**并发**执行，比你自己逐条串行
    读/搜快得多。派发本身免审批。

    worker 的能力边界（重要）：
    - worker 能：只读当前工作区（文件检索）+ **终端**（run_command 等）+ **联网检索**。
      其中只读 git 子命令（status/log/diff/show/branch/fetch）与文件检索**免审批**；
      其余命令与联网调用会以"子任务审批"形式出现在人工审批、可能等待。
    - worker 不能：**改动工作区文件**、做最终决策——它返回"结论正文 + 出处"，**只是给
      你做判断的素材**；它上下文独立，看不到其它 worker 的结果。
    - ⚠️ 也正因如此，**并行派多个 worker 时审批会从多路并来**：每个 worker 的写命令/联网都
      各占一次人工审批（面板上会标 `worker:<id>` 并附上那条子任务原文，便于你判断来由）。
      一次别派太多，别把审批面板变成队列。
    - 因此真正"干活"（改动、验证、给用户答复）仍是你自己：收到结论先汇总/交叉核对，需要落地
      改动时由你用写/命令工具执行，不要指望 worker 替你改。

    何时别用：子问题之间有依赖（下游要吃上游产物、需按先后做）——那不该并发，留给你自己按
    计划推进；或单问单答、拆无可拆——直接自己做即可，不要为派发而派发。

    用法提示：每条 sub_task 写成一条**自包含**的调研问题（含想要拿到的结论要点与出处要求），
    让 worker 查完即可回报、不必依赖别人；一次别派太多（建议 ≤5 条），太长就分批，避免并行
    结果过长、审批轰炸。

    Args:
        sub_tasks: 相互独立的调研/资料收集问题列表（每条约一个调查目标，含期望结论要点）。
            每条各由一个独立 worker 执行；互不共享上下文。空列表会被拒绝。

    Returns:
        汇总正文：逐个子任务的结果（✓/✗ + 结论或失败原因）。
    """
    if not isinstance(sub_tasks, list) or not [t for t in sub_tasks if str(t).strip()]:
        raise InvalidArgumentError(
            "sub_tasks 不能为空——至少要有一条自包含的调研问题；若只有一条、也不需要并发，直接自己做即可"
        )

    results = await run_subtask_batch(sub_tasks, _resolve_workspace())
    # 汇总正文很短，直接内联（不再抽独立渲染函数）
    lines = [f"已并发派出 {len(results)} 个资料收集 worker，结果："]
    for i, r in enumerate(results, 1):
        mark = "✓" if r["ok"] else "✗"
        lines.append(f"[{i}] ({mark}) 子任务「{r['task']}」")
        lines.append(str(r["content"]))
    return ToolResult(success=True, content="\n".join(lines))


if __name__ == "__main__":
    mcp.run(transport="stdio")
