"""QueueNode：轮次开始时把 tool_calls 搬进 pending，并清空单轮队列。"""
from app.agent.state import AgentState


#----------------------审核队列处理节点-------------------
class QueueNode:
    """轮次开始时的队列复位：把模型这一轮要调的工具搬进 pending，并清空上一轮的放行/回答。

    三个 approved_* 队列与 ask_answers 都是**单轮内**的东西（审批状态本身则持久在队列里，
    见 docs/ARCHITECTURE.md）：新的一轮开始时它们必须归零，否则上一轮没执行完的调用会漏进
    这一轮，而上一轮的回答会一直躺在 checkpoint 里。
    """

    async def __call__(self,state: AgentState) -> dict | AgentState:
        tool_calls = getattr(state["messages"][-1], "tool_calls", None) or []
        return {
            "pending_tool_calls": tool_calls,
            "approved_tool_calls": [],
            "approved_orchestrate_calls": [],
            "ask_answers": {},
        }


