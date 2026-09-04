from typing import TypedDict,Annotated,List,Any
from langgraph.graph.message import add_messages,BaseMessage
from app.schema.agent_schema import PlanStep

class AgentState(TypedDict):
    #--------------会话的唯一标识------------
    session_id:str

    #--------------历史消息-----------------
    messages:Annotated[List[BaseMessage],add_messages]

    #--------------工具调用-----------------
    # 待审队列与放行队列，审批状态通过独立队列持久化
    pending_tool_calls: List[dict[str, Any]]
    approved_tool_calls: List[dict[str, Any]]

    #--------------编排模式-----------------
    # 已批准待执行的编排类调用（source 命中 nodes.py 的 ORCHESTRATE_SOURCES），由 orchestrate_node 消费
    approved_orchestrate_calls: List[dict[str, Any]]
    # 当前计划：每步 {id: str, task: str, status: PlanStatus}
    # 由编排节点写回；模型通过编排工具（create_plan/update_plan_step/clear_plan）驱动
    current_plan: List[PlanStep]
