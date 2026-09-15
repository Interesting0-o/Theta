"""MCP 运行时：连接配置 + 工具加载 + **运行体常驻**（docs/SKILL_DESIGN.md §12）。

工作区由 get_main_agent_graph 解析后以 workspace_path 实参传入；这里在拉起子进程前先做前置校验
（未提供 / 目录不存在 → ConfigError），避免把坏配置传进子进程再等它 import 时失败。

**本模块承载三件事**（合一即"MCP 运行时"这一职责）：

1. **连接配置**：`_build_servers` 按工作区构造 file_io / terminal / web_search 的 stdio 连接；
2. **工具加载**：`load_mcp_tool` 返回模型可见的工具（**不绑会话的 shim**，见下）；
3. **运行体常驻**：每个 (工作区, 会话, server) 一个 owner task，持有一条长活 MCP 会话。

## 为什么要有 owner task（**承重约束，改这里前必读**）

anyio 的 cancel scope 与 asyncio Task 绑定（`anyio/_backends/_asyncio.py`："Attempted to exit
cancel scope in a different task than it was entered in"），而 MCP 会话内部就是
`anyio.create_task_group()`（stdio 子进程挂在里面）。偏偏 langgraph **每个节点都在新 Task 里执行**
（`pregel/_executor.py` → `loop.create_task`）——于是"在 A 节点的 Task 里建会话、日后在 B 里关"
必然抛 RuntimeError，而且抛在退出路径上（评估那处还会打断整批任务）。

结论：**会话的建与关必须发生在同一个 Task 里**。`_ServerWorker._run` 就是那个 Task——它建会话、
逐条执行调用、并在收到关闭哨兵时关闭。其它任何 Task 只能经 `call()` 投递请求（队列 + future）。
**复用**会话是安全的，只有建/关受这条约束。

## 为什么工具是 shim，而不是直接绑会话

`load_mcp_tools(session)` 返回的工具把会话**闭包**在 coroutine 里，会话一关它们就报
`ClosedResourceError`。所以本模块返回自造的 `StructuredTool`：只带 schema，执行时才向 owner 要会话
（`_make_shim`）。session 与 worker 都在**调用时**解析、不在构型期闭包——否则
`close_session_pool` 之后，缓存里的旧 shim 会作用在已关闭的池上。

shim 的返回值**与改造前逐字节同形**：内层是绑在该会话上的 adapter 原生工具，shim 把它的返回值包成
`(content, None)` 返回；`BaseTool.arun` 按 `content_and_artifact` 解包，而 `_format_output` 在
`tool_call_id is None`（本项目的 ToolNode 不传）时原样返回 content——工具结果归一化那条契约
（`app/agent/utils.py::format_tool_result`）因此不受影响。

## 常驻的语义（用户可见）

- **懒起**：首次**真调用**才建运行体；构图期只为拿 schema 拉一次临时进程（按工作区缓存）。
- **常驻到宿主关闭**：不做空闲回收。\"何时关\"由宿主驱动——TUI 在切会话/退出时、evaluation 在每任务
  结束时调 `close_session_pool` / `close_all_pools`（机制在本模块，生命周期归宿主：worker 子进程没有
  平台，机制不能寄生在平台里）。
- **按 (工作区, 会话) 隔离**：terminal 的受管进程表是**会话语义状态**，跨会话共享即泄漏。
- **配置在启动时刻固化**：env / cwd / WORKSPACE_PATH 都是建运行体时定的（`mcp_service.file_io`
  在 import 时校验），所以改 .env 需重开会话。
- **自愈**：传输类异常 → 关掉重建 → 重试一次。改造前"每次调用重开进程"天然有这个能力，
  常驻后必须显式补回，否则一个坏会话会让该会话**永久残废**。
"""
import asyncio
import logging
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import anyio
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import Connection, StdioConnection
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.shared.exceptions import McpError

from app.config import get_settings
from app.exception import ConfigError
from app.schema.agent_schema import MCPToolSpec

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent

# "运行体坏了"的异常类型：命中即由 owner 关掉当前会话、重建、重试一次（见 _ServerWorker._invoke）。
# 业务错误**不在**此列——mcp_service 的 guard 把工具失败收成普通返回值（ToolResult），
# 根本不会以异常形态冒到这里，也就绝不触发重建。
_TRANSPORT_ERRORS = (
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
    McpError,
)


def _normalize_workspace(workspace_path) -> str:
    """工作区路径的归一化算法（**单点**）：调用方与注册表键都用它，避免同一目录两种写法。"""
    return str(Path(workspace_path).expanduser().resolve())


def _validate_workspace(workspace_path) -> str:
    """校验并归一化工作区路径；未设置或目录不存在抛 ConfigError。"""
    if not workspace_path or not isinstance(workspace_path, str) or not workspace_path.strip():
        raise ConfigError("WORKSPACE_PATH 未设置")
    path = Path(workspace_path).expanduser().resolve()
    if not path.is_dir():
        raise ConfigError(f"agent工作区路径{path} 不存在")
    return str(path)


def stdio_connection(
    module: str, workspace: str, extra_env: dict[str, str] | None = None
) -> StdioConnection:
    """按项目约定造一个 stdio 连接：cwd=工作区、PYTHONPATH=项目根、可追加额外 env。

    核心 server 与**技能自带的 server** 共用这一份约定（模块名不同而已），所以启动器是通用的：
    `python -m <module>`，不为每个 server 记启动方式。

    - 各 server 都以**工作区**为启动目录（而非项目根）：file_io 本身按 WORKSPACE_PATH 解析
      路径，cwd 与解析无关；terminal 的 run_command 不显式传 cwd 时就在该进程 cwd 里执行，切到
      工作区可避免命令默认落在 Theta 仓库根、误伤自身源码。
    - 子进程 cwd 已切走，靠 PYTHONPATH 补回项目根，保证 `python -m mcp_service.*` /
      `python -m skills.<name>.server` 仍能 import 到包。
    """
    env = {
        "WORKSPACE_PATH": workspace,
        "PYTHONPATH": str(PROJECT_ROOT),
        **(extra_env or {}),
    }
    return StdioConnection(
        transport="stdio",
        command=sys.executable,
        args=["-m", module],
        cwd=workspace,
        env=env,
    )


def _build_servers(workspace_path: str) -> dict[str, Connection]:
    """按当前配置构造要拉起的 MCP server 连接集合（不实际拉起子进程，便于单测）。

    - file_io / terminal：必选，cwd 设为工作区根；
    - web_search：TAVILY_API_KEY 为空则跳过并告警（可选能力不阻塞主流程）。
    """
    servers: dict[str, Connection] = {
        "file_io": stdio_connection("mcp_service.file_io", workspace_path),
        "terminal": stdio_connection("mcp_service.terminal", workspace_path),
    }

    tavily_key = get_settings().TAVILY_API_KEY.get_secret_value()
    if not tavily_key:
        logger.warning("TAVILY_API_KEY 为空，跳过 web_search MCP server（联网检索不可用）")
        return servers

    servers["web_search"] = stdio_connection(
        "mcp_service.web_search", workspace_path, {"TAVILY_API_KEY": tavily_key}
    )
    return servers


# ---------------------- 工具的 schema ----------------------

# schema 缓存：键 = 工作区（**只放核心 server**）。schema 与会话无关，且每条 (工作区, 会话) 的 shim 都从它派生。
_TOOL_SPECS: dict[str, list[MCPToolSpec]] = {}
# 单 server 的 schema 缓存：键 = (工作区, server)，给**运行中动态加入的 server**（技能）用。
#
# ⚠️ 与 `_TOOL_SPECS` 是两条缓存，**绝不能互相写**：`_TOOL_SPECS` 的内容等于"调用方传进来的那批
# connections"，若技能列 schema 复用它、只传技能自己的 connection，就会把该工作区的核心 schema
# 覆盖成"只剩技能工具"——之后每个新会话的 file_io/terminal 都会静默消失。
_SERVER_SPECS: dict[tuple[str, str], list[MCPToolSpec]] = {}
# shim 缓存：键 = (工作区, 会话)。shim 是**无状态**的（调用时才解析 worker），
# 所以关闭运行体后无需失效——同一个 shim 会透明地连到重建后的运行体。
_MCP_TOOLS_CACHE: dict[tuple[str, str | None], list[BaseTool]] = {}
# 动态加入的 server 带来的 shim：键 = (工作区, 会话) → {server: [shim]}（按加载顺序）。
_EXTRA_TOOLS: dict[tuple[str, str | None], dict[str, list[BaseTool]]] = {}
# 工具集版本：键 = (工作区, 会话)。加载/卸载技能就 +1，LLMNode 据此决定要不要重新 bind_tools。
_VERSIONS: dict[tuple[str, str | None], int] = {}
# 单飞锁：同一键的并发冷加载只真正做一次。
_MCP_LOADING_LOCKS: dict[tuple[str, str | None], asyncio.Lock] = {}
_SPEC_LOCKS: dict[str, asyncio.Lock] = {}
_SERVER_SPEC_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def _spec_from_tool(server: str, tool) -> MCPToolSpec:
    """适配器工具 → 静态 schema（两条 schema 缓存共用的转换）。"""
    return MCPToolSpec(
        server=server,
        name=tool.name,
        description=tool.description or "",
        args_schema=tool.args_schema,
        metadata=tool.metadata,
    )


async def _tool_specs(workspace_path: str, connections: dict[str, Connection]) -> list[MCPToolSpec]:
    """取某工作区**核心 server** 的工具 schema（按工作区缓存）。

    **逐 server** 调 `client.get_tools(server_name=...)` 是有意的——归属必须显式（见 `schema.agent_schema.MCPToolSpec`）；
    用 `asyncio.gather` 保持并发（别写成串行，那会把加载延迟从 max(t) 变成 sum(t)）。

    这里拿到的工具绑在**临时会话**上、用完即关，我们**只读 schema、从不调用它们**——真正执行走
    owner 持有的常驻会话。代价是每次冷加载仍要 spawn 一遍（与改造前相同），但被本缓存兜住。

    ⚠️ 本函数**只接受完整的那批核心 connections**；动态加入的 server 走 `_server_specs`（见上面的警告）。
    """
    cached = _TOOL_SPECS.get(workspace_path)
    if cached is not None:
        return cached

    lock = _SPEC_LOCKS.setdefault(workspace_path, asyncio.Lock())
    async with lock:
        cached = _TOOL_SPECS.get(workspace_path)
        if cached is not None:
            return cached

        client = MultiServerMCPClient(connections)
        by_server = await asyncio.gather(
            *(client.get_tools(server_name=server) for server in connections)
        )
        # 完整列完之后**一次写入**（与 _server_specs 同一条纪律：不留半截缓存）
        specs = [
            _spec_from_tool(server, tool)
            for server, tools in zip(connections, by_server)
            for tool in tools
        ]
        _TOOL_SPECS[workspace_path] = specs
        return specs


async def _server_specs(
    workspace_path: str, server: str, connection: Connection
) -> list[MCPToolSpec]:
    """列**单个** server 的工具 schema（按 (工作区, server) 缓存）——给运行中动态加入的 server 用。

    与 `_tool_specs` 是两条缓存，理由见 `_SERVER_SPECS` 上的警告。临时会话随用随关、
    在**调用方自己的 Task 里**开关（与 `_tool_specs` 同形，不违反 anyio 那条承重约束）——
    别"优化"成常驻会话。
    """
    key = (workspace_path, server)
    cached = _SERVER_SPECS.get(key)
    if cached is not None:
        return cached

    lock = _SERVER_SPEC_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _SERVER_SPECS.get(key)
        if cached is not None:
            return cached

        client = MultiServerMCPClient({server: connection})
        tools = await client.get_tools(server_name=server)
        specs = [_spec_from_tool(server, tool) for tool in tools]
        _SERVER_SPECS[key] = specs  # 完整列完后一次写入
        return specs


def _make_shim(
    *, workspace: str, session: str | None, spec: MCPToolSpec, connection: Connection
) -> BaseTool:
    """把一条 schema 造成**不绑会话**的 shim：执行时向 owner 要会话。

    ⚠️ 三条不能改的约束（改了就与改造前的模型可见文本不一致）：

    - **不传 `tool_call_id` / `config` 给内层**：传了之后 `_format_output` 会返回
      `ToolMessage(status="error")`，`format_tool_result` 的 content-block 分支失效；
    - **不设 `handle_tool_error`**：内层（adapter 原生工具）已经吞掉 `ToolException`，这一层
      再也见不到它；设 True 只会把漏出来的裸 `ToolException` 降级成信息更少的字符串；
    - **`args_schema` 原样搬运** MCP 的 `inputSchema`：不规范化成 pydantic、不做 snake_case、
      不重排（server 顺序 = connections 顺序、server 内 = list_tools 顺序）——schema 一变就
      污染 prompt cache 与评估复现。
    """

    async def _call(**kwargs):
        # worker 在**调用时**解析：关闭运行体后再调用会透明地重建（见文件头"shim"一节）。
        worker = get_worker(workspace, session, spec.server, connection)
        result = await worker.call(spec.name, kwargs)
        # 包成 content_and_artifact 的二元组，交给外层 BaseTool.arun 解包——与内层同形。
        return (result, None)

    return StructuredTool(
        name=spec.name,
        description=spec.description,
        args_schema=spec.args_schema,
        coroutine=_call,
        response_format="content_and_artifact",
        metadata=spec.metadata,
    )


# ---------------------- 运行体（每 server 一个 owner task） ----------------------

# owner 队列的关闭哨兵：**不取消 task**——取消会截断 finally 里的 aclose（清理被腰斩）。
_SHUTDOWN = object()


class _ServerWorker:
    """某个 (工作区, 会话, server) 的专属 owner task：唯一有权建/关该运行体会话的地方。

    任何 Task 都能 `call()`，但真正的工作在 owner 自己的 Task 里跑——因为 anyio 的 cancel scope
    与 Task 绑定，而 langgraph 每节点开新 Task（见文件头"承重约束"）。

    常驻意味着"建完就一直活着"：owner 阻塞在队列上直到收到关闭哨兵，没有空闲计时器。
    """

    def __init__(
        self,
        *,
        workspace: str,
        session: str | None,
        server: str,
        connection: Connection,
        session_factory=None,
        tools_loader=None,
    ) -> None:
        self.workspace = workspace
        self.session = session
        self.server = server
        # 建在哪个事件循环上：stream 与 asyncio 原语都 loop-bound，换 loop 必须重建（见 get_worker）。
        self.loop = asyncio.get_running_loop()
        self._client = MultiServerMCPClient({server: connection})
        # 测试缝：默认走真 MCP；注入后可完全在进程内跑（不需要真 server，也才能断言"跨 Task 关闭"）。
        self._session_factory = session_factory  # (server) -> AsyncContextManager[ClientSession]
        self._tools_loader = tools_loader or load_mcp_tools  # (session) -> list[BaseTool]
        self._queue: asyncio.Queue = asyncio.Queue()
        self._start_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stack: AsyncExitStack | None = None
        self._tools: dict[str, BaseTool] | None = None

    async def call(self, tool_name: str, args: dict):
        """任意 Task 可调：把请求投给 owner，等它在自己那边执行完。"""
        await self._ensure_started()
        fut = asyncio.get_running_loop().create_future()
        await self._queue.put((tool_name, args, fut))
        return await fut

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run(), name=f"mcp-{self.server}")

    async def _run(self) -> None:
        """owner task：建会话 → 逐条执行 → 收到哨兵后**在自己的 Task 里**关闭。"""
        self._stack = AsyncExitStack()
        try:
            while True:
                command = await self._queue.get()
                if command is _SHUTDOWN:
                    break
                tool_name, args, fut = command
                try:
                    result = await self._invoke(tool_name, args)
                except asyncio.CancelledError:
                    raise  # owner 被取消（进程退出等）：向上走，走 finally 清理
                except BaseException as exc:  # noqa: BLE001 —— 交给调用方，不让 owner 自己死掉
                    if not fut.done():
                        fut.set_exception(exc)
                else:
                    if not fut.done():  # 调用方可能已被取消（turn 取消），别往已结束的 future 上写
                        fut.set_result(result)
        finally:
            self._tools = None
            await self._stack.aclose()  # ← 与创建同一个 Task，这正是本类存在的理由
            logger.info(
                "MCP 运行体已关闭：server=%s workspace=%s session=%s",
                self.server,
                self.workspace,
                self.session,
            )

    async def _invoke(self, tool_name: str, args: dict):
        """执行一次工具调用；会话失效则重建一次再重试。"""
        tools = await self._ensure_tools()
        try:
            return await tools[tool_name].ainvoke(args)
        except _TRANSPORT_ERRORS as exc:
            logger.warning(
                "MCP 运行体传输失败，重建后重试一次：server=%s workspace=%s session=%s（%s）",
                self.server,
                self.workspace,
                self.session,
                exc,
            )
            await self._discard_stack()
            tools = await self._ensure_tools()
            return await tools[tool_name].ainvoke(args)

    async def _ensure_tools(self) -> dict[str, BaseTool]:
        """懒建会话（首次真调用时）并取回绑在它上面的 adapter 原生工具。"""
        if self._tools is not None:
            return self._tools

        session_cm = (
            self._session_factory(self.server)
            if self._session_factory is not None
            else self._client.session(self.server)
        )
        session = await self._stack.enter_async_context(session_cm)
        try:
            tools = await self._tools_loader(session)
        except BaseException:
            # 会话已入栈但工具没拿到：立刻拆掉，免得下一个调用再叠一条会话上去
            await self._discard_stack()
            raise
        self._tools = {tool.name: tool for tool in tools}
        logger.info(
            "MCP 运行体已建立：server=%s workspace=%s session=%s（%d 个工具）",
            self.server,
            self.workspace,
            self.session,
            len(self._tools),
        )
        return self._tools

    async def _discard_stack(self) -> None:
        """关掉当前会话与子进程，为重建让路（**只在 owner 的 Task 里调**）。"""
        self._tools = None
        await self._stack.aclose()
        self._stack = AsyncExitStack()

    async def aclose(self) -> None:
        """宿主驱动的关闭：投哨兵 + 等 owner 收尾。

        不用 `task.cancel()`：取消会打断 finally 里的 `aclose`（清理被腰斩，子进程可能残留）；
        哨兵让 owner 走正常退出路径。在途调用会被先处理完（队列有序）——这正是想要的。
        """
        task, self._task = self._task, None
        if task is None:
            return  # 从未起过：没有运行体可关
        if task.done():
            if not task.cancelled():
                task.exception()  # 取回异常，避免 "Task exception was never retrieved"
            return
        await self._queue.put(_SHUTDOWN)
        await task


# 运行体注册表：键 = (工作区, 会话, server)。会话为 None = "无会话宿主"（langgraph dev 零参入口）。
_WORKERS: dict[tuple[str, str | None, str], _ServerWorker] = {}


def get_worker(
    workspace: str, session: str | None, server: str, connection: Connection
) -> _ServerWorker:
    """取（必要时建）某 (工作区, 会话, server) 的运行体；返回即用，实际子进程仍懒起。"""
    loop = asyncio.get_running_loop()
    key = (workspace, session, server)
    worker = _WORKERS.get(key)
    if worker is not None and worker.loop is loop:
        return worker
    if worker is not None:
        # 换了事件循环（测试里每个用例 asyncio.run、或新进程）：旧会话的 stream 与原语都绑在旧
        # loop 上，复用必炸。**丢弃而不关闭**——关闭同样跨 loop，且资源随旧 loop 一并消失。
        logger.info("MCP 运行体所在事件循环已变，丢弃重建：server=%s", server)
    worker = _ServerWorker(
        workspace=workspace, session=session, server=server, connection=connection
    )
    _WORKERS[key] = worker
    return worker


async def _shutdown(key: tuple[str, str | None, str]) -> None:
    worker = _WORKERS.pop(key, None)
    if worker is not None:
        await worker.aclose()


# ---------------------- 运行中动态加入/移除 server（技能二期用） ----------------------


async def register_server(
    workspace: str, session: str | None, server: str, connection: Connection
) -> list[str]:
    """把一个 server 的工具**动态加入某会话**的工具集；返回它带来的工具名（按 server 内顺序）。

    用途 = 能力型技能：`get_skill` 起这个 server（懒起——这里只列 schema 造 shim，真正的运行体在
    首次调用时才建）。mcp.py 本身**不认识"技能"**：它只知道"某会话多了一个 server"。

    校验（工具名是否已登记、是否重名）由调用方做——本函数只负责加入，失败回滚由调用方调
    `unregister_server`。
    """
    workspace = await asyncio.to_thread(_normalize_workspace, workspace)
    specs = await _server_specs(workspace, server, connection)
    tools = [
        _make_shim(workspace=workspace, session=session, spec=spec, connection=connection)
        for spec in specs
    ]
    key = (workspace, session)
    _EXTRA_TOOLS.setdefault(key, {})[server] = tools
    _VERSIONS[key] = _VERSIONS.get(key, 0) + 1
    logger.info(
        "已加入运行的 server：server=%s workspace=%s session=%s（%d 个工具）",
        server,
        workspace,
        session,
        len(tools),
    )
    return [tool.name for tool in tools]


async def unregister_server(workspace: str, session: str | None, server: str) -> None:
    """把一个 server 从某会话移除：丢掉它的 shim **并关掉它的运行体**（§3.3 第二坑：拆 server
    必须同时注销工具，否则模型会去调已经不存在的工具）。

    顺序：先摘 shim（纯内存）→ 再关运行体（投哨兵 + 等 owner，跨 Task 调用是安全的）→ 最后 +1 版本。
    没登记过（也没运行体）就什么都不做、**不**动版本——免得白让 LLMNode 重绑一次。

    也丢掉该 (工作区, server) 的 schema 缓存：它是**列一次用一辈子**的，留着会让"改技能 →
    drop_skill 再 get_skill"拿到**新代码 + 旧工具表**（新增/改名的工具看不见）。代价是下次
    加载重新列一次（本来就要起一次临时会话），而热改技能是低频事件。
    """
    workspace = await asyncio.to_thread(_normalize_workspace, workspace)
    key = (workspace, session)
    extras = _EXTRA_TOOLS.get(key)
    had_tools = extras is not None and extras.pop(server, None) is not None
    if extras is not None and not extras:
        _EXTRA_TOOLS.pop(key, None)

    had_worker = (workspace, session, server) in _WORKERS
    await _shutdown((workspace, session, server))
    _SERVER_SPECS.pop((workspace, server), None)

    if had_tools or had_worker:
        _VERSIONS[key] = _VERSIONS.get(key, 0) + 1
        logger.info(
            "已移除运行的 server：server=%s workspace=%s session=%s", server, workspace, session
        )


def session_tools(workspace: str, session: str | None) -> list[BaseTool]:
    """某会话当前可见的 MCP 工具：**核心 shim 在前（稳定前缀，利于 prompt cache）+ 技能带来的在后**。

    纯内存（不 spawn、不读盘、不抛），会被 LLMNode / ToolNode 每轮调用，所以必须廉价。

    ⚠️ **键的约定**：本函数与 `session_tool_names` / `tools_version` 都是**同步、按原样查**——
    它们不做 `_normalize_workspace`（那要 `resolve()`，是阻塞调用，会被 langgraph dev 的
    blockbuster 拦，而这里每轮都被调）。所以**调用方必须传归一化后的工作区**：生产路径上三个
    调用点（构图期的 `get_main_agent_graph`、`SessionToolset`、`get_skill` 的注入 workspace）
    拿到的都是 `_resolve_workspace` 的结果，天然一致。
    """
    key = (workspace, session)
    core = _MCP_TOOLS_CACHE.get(key)
    if core is None:
        # 本会话还没 load 过核心工具，或会话键与构图期不一致（理论上不该发生：两个宿主都用同一个
        # 会话 id）。**绝不能因为键没对上就让模型看不见核心工具**——退到该工作区已加载的那一份，
        # 并留一条日志（真出现就是键约定出了问题的信号）。
        core = next((v for k, v in _MCP_TOOLS_CACHE.items() if k[0] == workspace), ())
        if core:
            logger.warning(
                "会话 %s 没有核心工具缓存，退回该工作区已加载的那一份（键不一致？）", session
            )

    tools = list(core)
    for server_tools in _EXTRA_TOOLS.get(key, {}).values():
        tools.extend(server_tools)
    return tools


def session_tool_names(workspace: str, session: str | None) -> set[str]:
    """当前工具表里的工具名集合（同步、纯内存）——ReviewNode 判"这个名字在不在表里"用。"""
    return {tool.name for tool in session_tools(workspace, session)}


def registered_servers(workspace: str, session: str | None) -> set[str]:
    """某会话里**动态加入**的 server 名集合（同步、纯内存）——技能对账据此判断谁该在、谁该走。"""
    return set(_EXTRA_TOOLS.get((workspace, session), {}))


def tools_version(workspace: str, session: str | None) -> int:
    """工具集版本：加载/卸载技能会 +1。LLMNode 只在它变化时才重新 bind_tools。"""
    return _VERSIONS.get((workspace, session), 0)


async def close_session_pool(workspace: str, session: str | None) -> None:
    """关闭某 (工作区, 会话) 的全部运行体——**宿主驱动**（切会话 / 评估任务结束）。

    ⚠️ 调用方若随后要删工作区目录（evaluation 的 rmtree），必须**先关池、再删目录**：
    server 的 cwd 就在工作区，进程活着时 Windows 上删不掉。`aclose` 返回即子进程已被收
    （`create_session` 的 teardown 会关 stdin、等退出、超时后 SIGKILL），故无需额外等待。

    工作区按 `_normalize_workspace` 归一化后再匹配——键是归一化过的路径，调用方给的可能是原样路径。
    """
    if workspace:
        workspace = await asyncio.to_thread(_normalize_workspace, workspace)
    keys = [key for key in list(_WORKERS) if key[0] == workspace and key[1] == session]
    await asyncio.gather(*(_shutdown(key) for key in keys), return_exceptions=True)
    # 动态加入的工具与版本是**会话态**，随池一起清——否则切会话后旧键会永久留在表里
    # （工具再也用不到，却让 session_tools 每次多拼一份）。
    _EXTRA_TOOLS.pop((workspace, session), None)
    _VERSIONS.pop((workspace, session), None)


async def close_all_pools() -> None:
    """关闭本进程全部运行体（TUI 退出、进程收尾）。"""
    await asyncio.gather(*(_shutdown(key) for key in list(_WORKERS)), return_exceptions=True)
    _EXTRA_TOOLS.clear()
    _VERSIONS.clear()


def _reset_pools_for_tests() -> None:
    """清空全部注册表与缓存（测试用）。

    只清不关：跨 loop 关闭会炸（见 get_worker），且测试里的假会话没有真子进程要回收。
    """
    _WORKERS.clear()
    _TOOL_SPECS.clear()
    _SERVER_SPECS.clear()
    _MCP_TOOLS_CACHE.clear()
    _EXTRA_TOOLS.clear()
    _VERSIONS.clear()
    _MCP_LOADING_LOCKS.clear()
    _SPEC_LOCKS.clear()
    _SERVER_SPEC_LOCKS.clear()


# ---------------------- 对外入口 ----------------------


async def load_mcp_tool(workspace_path: str, session_id: str | None = None) -> list[BaseTool]:
    """加载某工作区的 MCP 工具（shim 形态），按 (工作区, 会话) 缓存。

    `session_id` 决定**运行体的作用域**：TUI 传真实会话 id、evaluation 传任务名、worker 传它自己
    的会话 id；langgraph dev 的零参入口传 None（该路径没有会话，所有会话共享一组运行体——
    已知偏差，见 docs/SKILL_DESIGN.md §12）。

    返回的工具**不绑会话**，所以本函数不会拉起常驻子进程；真正的运行体在首次工具调用时懒起。
    """
    # 文件系统 stat 属阻塞调用，langgraph dev 的 blockbuster 会在事件循环里拦截，故放线程里校验。
    workspace_path = await asyncio.to_thread(_validate_workspace, workspace_path)
    cache_key = (workspace_path, session_id)
    cached = _MCP_TOOLS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    lock = _MCP_LOADING_LOCKS.setdefault(cache_key, asyncio.Lock())
    async with lock:
        # 双检：等待锁的并发调用者直接复用已加载的结果
        cached = _MCP_TOOLS_CACHE.get(cache_key)
        if cached is not None:
            return cached

        connections = _build_servers(workspace_path)
        specs = await _tool_specs(workspace_path, connections)
        tools = [
            _make_shim(
                workspace=workspace_path,
                session=session_id,
                spec=spec,
                connection=connections[spec.server],
            )
            for spec in specs
        ]
        _MCP_TOOLS_CACHE[cache_key] = tools
        return tools
