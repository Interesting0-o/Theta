from typing import TypedDict,Annotated,List,Any,Dict
from langgraph.graph.message import add_messages,BaseMessage
from app.schema.agent_schema import AskAnswer,ImageRef,PlanStep,NoteEntry

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
    # 已批准待执行的编排类调用（source 命中 ReviewNode.ORCHESTRATE_SOURCES），由 orchestrate_node 消费
    approved_orchestrate_calls: List[dict[str, Any]]

    #--------------人机闸门（agent 提问）-----------------
    # 提问的回答：{tool_call_id: AskAnswer}。**闸门的产出是 state，执行器消费 state**——
    # ReviewNode 拿到人的回答后写这里，ask_user 工具经 OrchestrateNode 执行时按 tool_call_id 取回
    # 并渲染成回执（同 approved_* 队列的路子）。无 reducer → 写入方读改写合并；
    # 每轮由 QueueNode 清空（回答是单轮内的东西，只有同回合的工具执行要读它）。
    ask_answers: Dict[str, AskAnswer]
    # 当前计划：每步 {id: str, task: str, status: PlanStatus}
    # 由编排节点写回；模型通过编排工具（create_plan/update_plan_step/clear_plan）驱动
    current_plan: List[PlanStep]

    #--------------上下文留存-----------------
    # 笔记区：折叠时"不可免费重取"的联网检索正文落点；外层 dict 的 key = notes#r<n>（稳定 ref，
    # 随 checkpoint 存亡（不做跨会话语义库）。无 reducer → 写入方读改写合并。
    notes: Dict[str, NoteEntry]

    #--------------图片（@路径 的调用期附图通道）-----------------
    # 已随请求发送过的图片元数据：@路径 由 LLMNode 构造请求体副本时解析附上，base64 从不进
    # messages/checkpoint（见 ImageRef 注释）。无 reducer → 只由 LLMNode 单调追加。
    attached_images: List[ImageRef]

    #--------------技能（skill）-----------------
    # 已加载技能的 name 清单。**只存名字、不存正文**：正文由 LLMNode 每轮从盘上读
    # （口径同记忆的"文件即真值"）。只存名字让 SKILL_DESIGN §3.5 的铁律**结构性成立**——
    # 正文只有一个家（系统提示），卸载就是删一个名字，不存在"两份拷贝"。无 reducer。
    loaded_skills: List[str]
