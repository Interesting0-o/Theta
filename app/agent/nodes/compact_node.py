"""CompactNode：轮末压缩的 state 适配器。"""
from langchain_core.messages import RemoveMessage, SystemMessage

from app.agent.compression import policy as compression_policy
from app.agent.compression.compact import plan_compact
from app.agent.state import AgentState


#----------------------轮末压缩（compact_node）----------------------
# 轮内压缩的预算默认值。**轮末 CompactNode 不再使用它**——2026-10-20 起轮末压缩无条件
# 启用，没有字符预算门；保留本值是为了轮内节点仍有同一个"字符数代理"口径可用。
_CONTEXT_BUDGET_DEFAULT = 60000


def needs_compact(state, budget: int) -> bool:
    """预算门（**仅测试/兼容保留**）：历史 content 字符总数超过 budget。

    轮末 `CompactNode` 已改为无条件启用；轮内 `CompressNode` 已改用可压正文水位
    （`compressible_chars`，见 §8.6），不再调用本函数。
    """
    total = sum(len(str(getattr(m, "content", "") or "")) for m in (state.get("messages") or []))
    return total > budget


class CompactNode:
    """轮末压缩节点（END 前执行）：`compression.compact` 的 **state 适配器**。

    引擎产出一个 `FoldPlan`；本节点只做翻译：

    - 每个 `FoldGroup` 生成一条 `SystemMessage(摘要, id=该组最早被删消息的 id)` 原位替换；
    - `RemoveMessage` 删除该组其余被折块；
    - `notes` 合并（仅联网四工具的成功正文）；
    - 混合块里保留调用后的 `AIMessage` 副本由 `group.replaced` 原位写回。

    折叠区 = **最终答复之前**（含刚完成的当前轮工具块；最终答复原样保留）。没有最终
    答复（steering 提前收口）时退回“最后一条 Human 之前”，当前轮在途结果保持原样。
    折叠规则（完整/已消费、编排工具保护、失败/被拒保留、联网正文归档）见
    `app/agent/compression/compact.py`。纯确定性、不调 LLM；没有可折块时返回 `{}`。
    """

    def __init__(self) -> None:
        """编排工具集合从 tool.json 的 source 派生（策略单点：compression.policy）。"""
        self._orchestrate_tools = compression_policy.orchestrate_tools()

    async def __call__(self, state: AgentState) -> dict | AgentState:
        messages = list(state.get("messages") or [])
        if not messages:
            return {}

        notes = dict(state.get("notes") or {})
        plan = plan_compact(messages, notes, self._orchestrate_tools)
        if not plan.groups:
            return {}

        updates: list = []
        for group in plan.groups:
            if not group.anchor_id or not group.removed:
                continue
            updates.append(SystemMessage(content=group.summary, id=group.anchor_id))
            updates += group.replaced
            updates += [
                RemoveMessage(id=m.id)
                for m in group.removed[1:]
                if getattr(m, "id", None)
            ]
        if not updates:
            return {}

        result: dict = {"messages": updates}
        if plan.archived_notes:
            notes.update(plan.archived_notes)
            result["notes"] = notes
        return result


