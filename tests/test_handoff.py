"""project-handoff 技能的 host 侧两件事：交接材料指针注入 + 技能可发现。

- `handoff_pointer_block`（prompt.py）：工作区根有 HANDOFF.md → 指针；没有 → ""。
  挂在 LLMNode 的 `inject_session_context` 分支下——worker 与画像/记忆同一道闸，结构性吃不到。
- LLMNode 注入：有 HANDOFF.md 时系统消息里出现指针；worker 模式没有。
- 技能可发现：`scan_skills` 找得到 project-handoff（知识型、description 非空）——
  目录里缺了它或没 description，"交接建议"就无从谈起。

不花钱：假模型直接驱动 LLMNode 实例，不走图、不拉 MCP、不需 .env 之外的环境。
"""
import asyncio

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agent.nodes import LLMNode
from app.agent.prompt import HANDOFF_MD_FILENAME, handoff_pointer_block
from app.resource.skills import scan_skills

SKILL_NAME = "project-handoff"


class _FixedModel(BaseChatModel):
    """恒返一条 AIMessage 的假模型；**录下收到的 messages**——LLMNode 的系统消息是临时列表、
    只进模型请求不进 state 返回值，断言只能对着模型实际收到的那份。"""

    seen: list = []

    @property
    def _llm_type(self) -> str:
        return "fixed"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # noqa: ARG002
        self.seen.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    def bind_tools(self, tools, **kwargs):  # noqa: ARG002
        return self


def test_pointer_absent_without_file(tmp_path):
    assert handoff_pointer_block(str(tmp_path)) == ""


def test_pointer_present_with_file(tmp_path):
    (tmp_path / HANDOFF_MD_FILENAME).write_text("# 交接\n\n内容", encoding="utf-8")
    pointer = handoff_pointer_block(str(tmp_path))
    assert HANDOFF_MD_FILENAME in pointer
    assert "read_file" in pointer  # 指针只指路，正文让模型自己取
    assert "内容" not in pointer  # 不读正文——文件即真值，注入的只是"它存在"


def test_llmnode_injects_pointer_for_main_not_for_worker(tmp_path):
    (tmp_path / HANDOFF_MD_FILENAME).write_text("材料", encoding="utf-8")

    async def call(inject_session_context: bool):
        model = _FixedModel()
        node = LLMNode(
            model=model,
            workspace_path=str(tmp_path),
            attach_images=False,
            inject_session_context=inject_session_context,
        )
        await node({"messages": [HumanMessage(content="hi")]})
        return model.seen[-1]  # 模型实际收到的那一拍消息

    received = asyncio.run(call(True))
    system_texts = [m.content for m in received if isinstance(m, SystemMessage)]
    assert any("交接材料" in t and HANDOFF_MD_FILENAME in t for t in system_texts)

    # worker（inject_session_context=False）不注入：交接是主会话的任务态
    received_worker = asyncio.run(call(False))
    worker_texts = [m.content for m in received_worker if isinstance(m, SystemMessage)]
    assert not any("交接材料" in t for t in worker_texts)


def test_skill_is_discoverable():
    """scan_skills 找得到 project-handoff：知识型、description 非空（目录对模型有信息量的底线）。"""
    found = scan_skills()
    meta = found.get(SKILL_NAME)
    assert meta is not None, "skills/project-handoff 必须能被扫盘发现"
    assert meta.capability is False  # 知识型：纪律包，不带工具
    assert meta.description.strip()
    assert meta.name == SKILL_NAME and meta.dir_name == SKILL_NAME
