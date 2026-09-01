from typing import TypedDict,Annotated,List,Any
from langgraph.graph.message import add_messages,BaseMessage


class AgentState(TypedDict):
    #--------------会话的唯一标识------------
    session_id:str

    #--------------历史消息-----------------
    messages:Annotated[List[BaseMessage],add_messages]

    #--------------工具调用-----------------
    # 待审队列与放行队列，审批状态通过独立队列持久化
    pending_tool_calls: List[dict[str, Any]]
    approved_tool_calls: List[dict[str, Any]]
