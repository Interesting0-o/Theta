import asyncio
import sqlite3

from langchain_core.runnables import RunnableConfig
from langchain.messages import HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app.agent.graph import get_graph
from app.agent.state import AgentState
from app.agent.utils import format_tool_approval, get_agent_db_path


async def tui_test():
    """
    TUI测试
    """
    db_path = get_agent_db_path()
    connection = sqlite3.connect(str(db_path), check_same_thread=False)
    checkpointer = SqliteSaver(connection)
    # 创建表
    checkpointer.setup()

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

        state: AgentState = {
            "session_id": "conversation_456",
            "messages": [HumanMessage(content=user_input)],
            "pending_tool_calls": [],
            "approved_tool_calls": [],
            "current_plan": [],
        }

        # res = compile_graph.invoke(state, config=config)

        inputs = state  # 首次进入传完整 state；恢复断点时传 Command
        while True:
            result = await compile_graph.ainvoke(inputs, config)

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

if __name__ == "__main__":
    
    asyncio.run(tui_test())