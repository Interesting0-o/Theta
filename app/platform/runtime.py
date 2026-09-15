"""按 (工作区, 会话) 装配运行时：db / checkpointer / 编译图 / 记忆播种（归基座）。

位置：`app/platform/runtime.py`（从 app/tui/driver.py::_build_session_runtime 平移；
2026-09-10 基座/前端分家后归基座——它只与资源落点和图有关，与终端显示无关）。

`app.agent.*`（graph/model，import 即需 .env）在本模块内**懒加载**：`import app.platform`
不触发 .env，保持 TUI/基座可被无 .env 的测试与工具顶层 import。
`ensure_memory_template`（要 stat/mkdir）走 asyncio.to_thread——langgraph dev 的 blockbuster
会在事件循环里拦截这类阻塞调用。注意 `session_db_path()` 的路径解析与 `mkdir` 是**直接**在
协程里调的（本路径不在 langgraph dev 上跑，故未加 to_thread）。
"""
from __future__ import annotations

import asyncio

from app.resource import session_db_path


async def build_session_runtime(workspace: str, session_id: str):
    """构建本会话运行时：db 落盘 + AsyncSqliteSaver + 编译图 + thread step。

    返回 (connection, step)：connection 供退出时关闭；step 是 compiled.ainvoke 的闭包
    （带本会话 thread_id）。首次为某工作区建会话时，顺带播种该工作区的长期记忆模板
    （app/agent/memory.py::ensure_memory_template，缺失才写）。app.agent.* 在此懒加载
    ——首次交互时才有 .env 与真实需要。
    """
    import aiosqlite  # noqa: PLC0415
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: PLC0415
    from app.agent.graph import get_main_agent_graph  # noqa: PLC0415
    from app.agent.memory import ensure_memory_template  # noqa: PLC0415

    db_path = session_db_path(workspace, session_id)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(ensure_memory_template, workspace)  # 新工作区播种记忆模板
    connection = await aiosqlite.connect(str(db_path))
    try:
        checkpointer = AsyncSqliteSaver(connection)
        await checkpointer.setup()

        # session_id 一并传入：MCP 运行体按 (工作区, 会话) 隔离（terminal 的受管进程表是会话
        # 语义状态，跨会话共享即泄漏）；切会话时由 AgentPlatform.switch_session 关掉旧会话的池。
        graph = await get_main_agent_graph(workspace, session_id)
    except BaseException:
        # 建图失败（.env 缺 / MCP 起不来等）时这个连接**还没交给调用方**——调用方的 finally
        # 只关它拿到的那个，所以这里不关就没人关（2026-09-15 审计发现的异常路径泄漏）。
        await connection.close()
        raise
    compiled = graph.compile(checkpointer=checkpointer)
    config: dict = {"configurable": {"thread_id": session_id}}
    step = lambda inputs: compiled.ainvoke(inputs, config)  # noqa: E731
    return connection, step
