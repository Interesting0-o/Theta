import asyncio
import json
from pathlib import Path

import aiosqlite
from langchain_core.runnables import RunnableConfig
from langchain.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app.agent.graph import get_graph
from app.agent.state import AgentState


def get_agent_db_path() -> Path:
    """agent checkpoint 的持久化 SQLite 路径（项目根/resource/agent.db，自动建目录）。"""
    db_dir = Path(__file__).resolve().parent.parent / "resource"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "agent.db"


def truncate(text: str, limit: int = 200) -> str:
    """超长文本截断显示，避免整屏刷出大段文件内容。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f" ...(共 {len(text)} 字符，已截断)"


def format_tool_approval(interrupts) -> str:
    """
    把 langgraph 的 Interrupt 列表格式化成易读的审批信息。

    原始数据形如 [Interrupt(value={'type': 'tool_approval', 'tool_name': ...,
    'tool_args': {...}}, id='...')]，直接 print 会是一大坨难看的 repr。
    这里提取出工具名、步骤、参数、调用ID，并对长内容做截断。
    """
    lines: list[str] = []
    for item in interrupts:
        value = getattr(item, "value", item)
        if not isinstance(value, dict):
            lines.append(str(value))
            continue

        tool_name = value.get("tool_name", "?")
        step = value.get("current_step", "")
        tool_call_id = value.get("tool_call_id", "")

        header = f"[{tool_name}]"
        if step:
            header += f"   步骤 {step}"
        lines.append(header)

        args = value.get("tool_args", {})
        # 终端等工具带必填 description（模型对命令的人话解释）：单独一行突出展示，
        # 让人先看到"意图"，再核对下方命令本身是否一致。
        if isinstance(args, dict) and args.get("description"):
            lines.append(f"  解释: {truncate(str(args['description']))}")
            args = {k: v for k, v in args.items() if k != "description"}
        if args:
            lines.append("  参数:")
            if isinstance(args, dict):
                for k, v in args.items():
                    if isinstance(v, str):
                        lines.append(f"    - {k}: {truncate(v)}")
                    else:
                        lines.append(f"    - {k}: {json.dumps(v, ensure_ascii=False)}")
            else:
                lines.append(f"    - {json.dumps(args, ensure_ascii=False)}")

        if tool_call_id:
            lines.append(f"  调用ID: {tool_call_id}")

    return "\n".join(lines)


async def tui_test():
    """
    TUI测试
    """
    db_path = get_agent_db_path()
    # ainvoke 走异步循环，必须用 AsyncSqliteSaver（同步 SqliteSaver 不支持 async 方法）
    connection = await aiosqlite.connect(str(db_path))
    checkpointer = AsyncSqliteSaver(connection)
    # 创建表
    await checkpointer.setup()

    graph = await get_graph()
    compile_graph = graph.compile(checkpointer=checkpointer)
    user_title = """
+------+
| User |
+------+
"""
    ai_title = """
+-------+
| Agent |
+-------+
"""
    command_title = """
+---------+
| Command |
+---------+
"""

    config: RunnableConfig = {
            "configurable": {
            "thread_id": "conversation_456" # 会话ID，用于持久化/记忆功能
        }
    }
    
    try:
        while True:
            print(user_title)
            try:
                user_input = input()
            except EOFError:
                print("\n输入流已关闭，已退出。")
                break
            print()

            if user_input in ["quit","q","exit"]:
                break

            # 每轮只带"新用户消息 + 本轮瞬态审批队列"；刻意不放 current_plan。
            # 计划由编排工具（create_plan 等）写回、存于 checkpoint 中跨轮存活，
            # 这里若注入 [] 会把上一轮建好的计划覆盖清空（曾导致计划无法跨用户轮延续）。
            state: dict | AgentState = {
                "session_id": "conversation_456",
                "messages": [HumanMessage(content=user_input)],
                "pending_tool_calls": [],
                "approved_tool_calls": [],
            }

            # res = compile_graph.invoke(state, config=config)

            inputs = state  # 首次进入传完整 state；恢复断点时传 Command
            while True:
                result = await compile_graph.ainvoke(inputs, config)#type:ignore

                if "__interrupt__" in result:
                    interrupt_data = result["__interrupt__"]
                    print(command_title)
                    print(format_tool_approval(interrupt_data))
                    print()
                    decision = input("是否批准该操作？(y/n): ").strip().lower()
                    is_approved = decision in ("y", "yes", "是")
                    inputs = Command(resume={"approved": is_approved})
                    continue

                if "messages" in result:
                    print(ai_title)
                    print(result["messages"][-1].content)
                break
    finally:
        # 关闭 aiosqlite 连接：不关会让后台 sqlite 线程残留、python 进程无法退出
        await connection.close()

if __name__ == "__main__":
    
    asyncio.run(tui_test())