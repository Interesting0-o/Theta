"""评估的模型侧：**回放用的替身** + **录制用的旁观者**（层 A 的两半）。

层 A 之所以能零成本，靠的就是这里：**除了模型换成脚本，其余全是真的**——真图、真 MCP 运行体、
真 ReviewNode 路由、真工具执行。所以它守的是"**除模型之外的一切**"（图接线 / tool.json 策略 /
工具参数处理 / 编排分流 / 审批拒绝 / 提问闸门 / compact 折叠），与 tests/ 里手写的 tool_calls
互补：那边覆盖边界情况，这边用**真实录制**的形态（一轮调几个、参数怎么写、正文写什么）。

⚠️ **回放的模型不读输入**（脚本按序吐 AIMessage），所以**改了 prompt / 工具 docstring 必须跑层 B
真跑**——层 A 对这类改动是瞎的，"回放全绿"不等于"评估过了"。

两条路刻意用了**不同的接缝**，各取所长：
- **回放**必须换掉模型 → 走 `get_main_agent_graph(model=…)`（构图期参量，与
  `get_sub_agent_graph` 的同名参量一路）；
- **录制**不必动模型 → 挂一个 run 级 callback 旁观（`BaseCallbackHandler.on_llm_end`）。
  于是录制跑的就是**生产路径本身**，零偏离。已实测：图把它 `ainvoke(inputs, config)` 收到的
  `callbacks` 传播到了节点内部 `model.ainvoke(messages)`（节点自己并没有传 config）——正是靠这条，
  录制才不必包装模型。
"""
from __future__ import annotations

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ReplayChatModel(BaseChatModel):
    """按序吐 `AIMessage` 的脚本模型——把录制下来的模型输出原样放回去。

    最后一条**粘住**（不再 pop）：图在 tool_node 之后会回到 `llm_node`，收尾轮数不由脚本决定，
    粘住尾条让"给完答复就 END"自然发生，回放不必猜轮数（`replies` 为空会在 fixtures 加载期被
    拦下，见 `evaluation/fixtures.py`）。

    `bind_tools` 返回自身：`LLMNode` 按工具集版本调它，缺这个方法会 NotImplementedError。
    """

    replies: list[AIMessage]

    @property
    def _llm_type(self) -> str:
        return "replay"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # noqa: ARG002
        reply = self.replies[0] if len(self.replies) == 1 else self.replies.pop(0)
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools, **kwargs):  # noqa: ARG002 —— 只认脚本，不必真绑定
        return self


class RecordingCallback(BaseCallbackHandler):
    """被动的录制者：把模型每次的响应收进 `replies`，不干预任何东西。

    挂法（不碰模型、不碰图）::

        recorded: list[AIMessage] = []
        config = {..., "callbacks": [RecordingCallback(recorded)]}
        await graph.ainvoke(inputs, config)

    只挂 `on_llm_end` 一个钩子：它是 `BaseChatModel._generate_with_cache` **直接**触发的那个
    （探针实测）。**没有**同时挂 `on_chat_model_end`——那是更高一层的 runnable 事件，它与本钩子
    会不会同时触发取决于具体模型实现（本仓库只验证过"只挂 on_llm_end 时录到的条数与模型实际调用
    次数一致、没有重复"，没逐一对齐过两个都挂的情形）。要加之前先实测会不会录两遍。
    """

    def __init__(self, replies: list[AIMessage]) -> None:
        super().__init__()
        self.replies = replies

    def on_llm_end(self, response, **kwargs) -> None:  # noqa: ARG002 —— 只要响应本体
        """`response` 是 `LLMResult`，`generations` 为 `list[list[ChatGeneration]]`。"""
        for batch in getattr(response, "generations", None) or []:
            for generation in batch if isinstance(batch, list) else [batch]:
                message = getattr(generation, "message", None)
                if isinstance(message, AIMessage):
                    self.replies.append(message)
