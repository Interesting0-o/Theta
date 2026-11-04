"""ReviewNode：人机闸门的唯一落点（审批 / 提问 / 分流）。"""
import asyncio
import json

from langchain_core.messages import ToolMessage
from langgraph.types import interrupt

from app.agent import gates
from app.agent.compression.parser import one_line
from app.agent.state import AgentState
from app.resource import event_log
from app.resource.skills import get_meta, read_tool_policies
from app.schema.approval_schema import GATE_ASK_USER, GATE_TOOL_APPROVAL
from ._shared import _tool_stamp


#----------------------工具调用日志（审核节点逐条输出）----------------------

def _tool_call_log(tool_call: dict) -> str:
    """把一条待审工具调用渲染成一行日志（工具名 + 紧凑参数）。"""
    name = tool_call.get("name") or "?"
    args = tool_call.get("args") or {}
    try:
        brief = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except TypeError:
        brief = str(args)
    return f"→ 工具调用: {name}  {one_line(brief)}"


#----------------------工具审核节点--------------------
class ReviewNode:
    """逐条过审 pending 工具调用：按 tool.json 定处置（放行 / 挂起问人 / 回执），再按 source 分流。

    本节点是**人机闸门的唯一落点**，两种载荷都在这里挂起（docs/MULTI_AGENT.md §6）：
    - `tool_approval`：审批（need_review 的调用）；
    - `ask_user`：agent 提问（source=ask 的调用，见 ASK_SOURCE）。

    ⚠️ **interrupt 只能待在"挂起之前没有副作用"的节点里**——这条是承重约束，不是风格偏好。
    LangGraph 的 resume 会**把整个节点从头重跑**（interrupt() 依次返回记录的 resume 值），
    实测钉死于 2026-09-16：一个含 `print` + `interrupt()` 的节点，resume 后 print 出现第二次。
    注意重跑丢弃的是**未提交的 state 写**，而节点里发生的**真实外部副作用不会回滚**
    （写文件、起进程都会真切两次）——所以 `ask_user` 的 interrupt **不能**放进
    `OrchestrateNode`（同批次里可能有 write_memory）。本节点满足该约束：interrupt 之前只有
    一行日志 print；工具的执行与副作用一律交给下游执行器。

    与 ToolNode/OrchestrateNode 共用同一份 tool.json（读取与缓存单点在 app/agent/gates.py）。
    """

    # 编排工具在 tool.json 的 source 取值集合：命中即分流进 approved_orchestrate_calls，
    # 交 OrchestrateNode（通用编排执行器）处理。plan=编排；notes=笔记读回；
    # ask=向用户提问（见下 ASK_SOURCE 的说明）；
    # memory=长期记忆读写（app/agent/tools.py 的 write_memory/read_memory，靠注入的 workspace
    # 定位记忆文件）；skill=技能加载/卸载（同文件 get_skill/drop_skill）；
    # events=确定性 event log 的只读查询（read_event_log，同样靠注入的 workspace 定位会话日志）。
    # 注意两处**不在**这里的：① 技能**带来**的工具——它们的 source 取 `skills/<name>`，不命中本
    # 集合 → 进普通工具队列（get_skill 是编排动作，它带来的工具不是）；② `dispatch_subtasks`
    # ——2026-09-21 起它是 `mcp_service/dispatch` 这个 server 的工具（派生 worker = 独立进程
    # 与独立 Agent runtime，四问的第四问），走 ToolNode，**不再进编排队列**。
    # 单一来源在 `gates.ORCHESTRATE_SOURCES`（2026-11-04 下移：压缩引擎也要用它，
    # 而 compression 不能反向 import 本模块）；这里保留同名类属性，便于读者按闸门查。
    ORCHESTRATE_SOURCES: frozenset[str] = gates.ORCHESTRATE_SOURCES

    # 人机闸门的第二种载荷（docs/MULTI_AGENT.md §6）：source=ask 的工具（当前只有 ask_user）
    # **除了走编排执行器，还要在本节点再挂一次 interrupt 向人提问**。分流进编排队列照旧——
    # 回答经 state 的 ask_answers 交给工具体渲染成回执，见下面 __call__ 的 ASK 分支。
    ASK_SOURCE = "ask"

    def __init__(self, log: bool = True, toolset=None, allow_ask: bool = True) -> None:
        """log=False 供 stdio MCP server 型 worker 使用（stdout 即 JSON-RPC 协议，不能 print）。

        toolset：给了才做"存在性 + 漏登记"那道防线（主图给；worker 与单测不给 → 行为与改造前一致）；
        allow_ask=False 供 worker 用：**本图不承载提问载荷**（见 __call__ 的 ASK 分支）。
        """
        self.tool_review = gates.tool_config()
        self.log = log
        self.toolset = toolset
        self.allow_ask = allow_ask

    def _known_names(self, state: AgentState) -> set[str] | None:
        """当前可用的工具名（含编排工具）；无 toolset（worker / 单测）→ None = 不判存在性。"""
        if self.toolset is None:
            return None
        return self.toolset.known_names(state)

    def _tool_cfg(self, tool_name: str | None, state: AgentState) -> dict:
        """一条工具调用的策略条目：**内置表（tool.json）优先，其次已加载技能的 skill.json 声明**。

        2026-09-19 起技能自带工具的 `need_review` 声明在技能目录自己的 skill.json（约束跟能力
        走，见 skills.read_tool_policies）；tool.json 只管内置 MCP 工具与编排工具。技能工具能
        走到本查询的前提是它已被加载——加载期 `_check_skill_tools` 已逐条校验过声明形状，这里
        信任那份声明。两级都没有 → {}（`_decide` 把"在工具表里却两头无政策"判成 review，
        fail-closed 第二道防线不变）。读盘不缓存：与加载期校验同一口径，改 skill.json 立即生效。
        """
        if not tool_name:
            return {}
        cfg = self.tool_review.get(tool_name)
        if cfg is not None:
            return cfg
        if self.toolset is None:
            return {}
        for skill_name in state.get("loaded_skills") or []:
            meta = get_meta(skill_name)
            if meta is None:
                continue
            conf = read_tool_policies(meta).get(tool_name)
            if conf is not None:
                return conf
        return {}

    def _decide(
        self,
        tool_name: str | None,
        tool_cfg: dict,
        state: AgentState,
        tool_args: dict | None = None,
    ) -> str:
        """给一条待审调用定处置：`unknown` / `review` / `free`。

        - **unknown**：名字不在当前工具表里（模型编的，或它所属技能刚被卸载）→ 回执、**不弹面板**。
          否则模型每编一个名字就打断人一次，而它无论如何都会被 ToolNode 拒掉；
        - **free（命令级）**：tool.json 标了 `free_when: readonly_shell` 的工具（当前只有
          run_command），**还要它的参数**判为只读终端命令才算数，见 `gates.free_shell_verdict`。
          判定不过就落回 review —— 这条路是加宽免审面的唯一入口，失败方向必须是收紧。
        - **review**：策略说 `need_review: true`（内置表或技能自己的 skill.json）；**或**它在
          工具表里却两头都没有政策条目 —— 这条是 §13.4 的第二道防线（fail-closed）：有人往
          MCP server 加了个写工具却忘了声明，旧行为是"未声明 = 免审"，等于静默放行一次写操作。
          限定在"在表里"是为了不误伤模型编出来的名字（那是 unknown，回执而不弹面板）。
        - **free**：有政策条目且 `need_review: false`。
        """
        if tool_name is None:
            return "free"
        known = self._known_names(state)
        if known is not None and tool_name not in known:
            return "unknown"
        if tool_cfg.get("free_when") == "readonly_shell" and gates.free_shell_verdict(tool_args or {}):
            return "free"
        if tool_cfg.get("need_review"):
            return "review"
        if known is not None and not tool_cfg:
            return "review"
        return "free"

    def _receipt_result(
        self, state: AgentState, current_tool: dict, content: str, error_type: str
    ) -> dict:
        """"回执而不执行"的通用返回切片：兑现悬空 tool_call + 给模型一条可行动的错误说明。

        未知工具与参数非法的提问共用它——两者都是"这条调用不会被送去执行，但模型必须知道
        为什么"。**以原 tool_call_id 兑现**是必须的：否则历史里会留下一条永远没有回应的
        assistant tool_calls 块。
        """
        return {
            "pending_tool_calls": list(state.get("pending_tool_calls", []))[1:],
            "approved_orchestrate_calls": list(state.get("approved_orchestrate_calls", [])),
            "approved_tool_calls": list(state.get("approved_tool_calls", [])),
            "messages": [
                ToolMessage(
                    name=current_tool.get("name"),
                    tool_call_id=current_tool.get("id"),
                    content=content,
                    additional_kwargs=_tool_stamp(error_type=error_type),
                )
            ],
        }

    def _unknown_result(self, state: AgentState, current_tool: dict) -> dict:
        """未知工具：兑现悬空 tool_call + 回一条可行动回执（既不打扰人、也不放行去执行）。"""
        tool_name = current_tool.get("name")
        return self._receipt_result(
            state,
            current_tool,
            (
                f"工具不存在或未注册: {tool_name}"
                f"（不在本会话当前可用的工具表里——可能所属技能刚刚被卸载、或它的运行体"
                f"已断开）。若仍需要它，请先用 get_skill 重新加载对应技能；不要原样重试。"
            ),
            "unknown_tool",
        )

    async def _log_event(
        self,
        state: AgentState,
        tool_call: dict,
        *,
        status: str,
        error_type: str | None,
        approval: str,
        content_chars: int = 0,
    ) -> None:
        """写一行确定性 event log（只记未被执行 / 被拒的闸门结果）。

        已放行的执行结果由 ToolNode / OrchestrateNode 写——同一 tool_call 只落一行，不重复。
        """
        workspace = getattr(self.toolset, "workspace", None)
        session = state.get("session_id")
        if not workspace or not session:
            return
        await asyncio.to_thread(
            event_log.append_event,
            workspace,
            session,
            tool=tool_call.get("name") or "",
            tool_call_id=tool_call.get("id") or "",
            args=tool_call.get("args") or {},
            status=status,
            error_type=error_type,
            approval=approval,
            content_chars=content_chars,
        )

    async def __call__(self, state: AgentState) -> dict | AgentState:
        pending = list(state.get("pending_tool_calls", []))
        if not pending:
            return {"pending_tool_calls": []}

        current_tool = pending[0]
        tool_name = current_tool.get("name")
        if self.log:
            # 工具调用日志：每条待审调用过审核节点时打一行（含需要人工审批与免审放行者）
            print(_tool_call_log(current_tool))
        tool_cfg = self._tool_cfg(tool_name, state)
        approved = True
        denied_messages: list[ToolMessage] = []

        action = self._decide(tool_name, tool_cfg, state, current_tool.get("args", {}))
        if action == "unknown":
            # 模型编出来的名字（本会话根本没绑定过它）：回执而不弹面板，见 _decide 的说明
            result = self._unknown_result(state, current_tool)
            content = result["messages"][0].content
            await self._log_event(
                state,
                current_tool,
                status="failed",
                error_type="unknown_tool",
                approval="not_required",
                content_chars=len(content),
            )
            return result

        ask_answers: dict | None = None
        if action == "review":
            payload = {
                "type": GATE_TOOL_APPROVAL,
                "current_step": "1/1",
                "tool_name": tool_name,
                "tool_args": current_tool.get("args", {}),
                "tool_call_id": current_tool.get("id"),
            }
            decision = interrupt(payload)
            approved = isinstance(decision, dict) and decision.get("approved", False)
            if not approved:
                # 被拒的调用必须回灌一条显式消息，而不是静默丢弃：
                # - 模型得知道"这次调用没执行、原因是被拒"，否则会原样重试同一命令；
                # - 这条 ToolMessage 以原 tool_call_id 兑现历史里悬空的 tool_calls 块，
                #   避免下一条用户消息紧跟未兑现的 assistant tool_call（协议隐患）。
                denied_messages.append(
                    ToolMessage(
                        name=tool_name,
                        tool_call_id=current_tool.get("id"),
                        content=(
                            "[approval_denied] 工具调用被人工拒绝，未执行。"
                            "不要原样重试相同参数；若仍认为该操作必要，"
                            "先向用户说明意图或提出替代方案。"
                        ),
                        # 结构化戳：压缩消费端读 decision=denied 即可判定，不必解析前缀
                        additional_kwargs=_tool_stamp(decision="denied"),
                    )
                )
                await self._log_event(
                    state,
                    current_tool,
                    status="denied",
                    error_type=None,
                    approval="denied",
                    content_chars=len(denied_messages[-1].content),
                )

        elif tool_cfg.get("source") == self.ASK_SOURCE and not self.allow_ask:
            # worker：提问载荷在这张图里不成立（worker 是只读资料收集器、不该问用户）。它**结构性**
            # 拿不到 ask_user（source=ask 不在 worker_tools 的规则里），但模型仍可能编出这个名字——
            # 而 worker 的 ReviewNode 没有 toolset，"未知工具"那道防线不生效。真按提问挂起的话，
            # 主侧会收到一张语义错乱的面板、人还得白答一次。回执让它把"该问什么"写进结论交回去。
            return self._receipt_result(
                state,
                current_tool,
                f"{tool_name} 在本图不可用：你不能向用户提问——把「需要用户定夺什么」写进"
                "结论交回主 agent，由它来问。不要原样重试。",
                "ask_not_available",
            )
        elif tool_cfg.get("source") == self.ASK_SOURCE:
            # 人机闸门的第二种载荷：把问题摆给人、等回答（docs/MULTI_AGENT.md §6）。
            # 参数先在弹面板**之前**校验——空问题 / 超限选项摆到人面前是最糟的形态。
            request, problem = gates.ask_request(current_tool.get("args") or {})
            if problem is not None:
                result = self._receipt_result(
                    state, current_tool, problem, "invalid_ask"
                )
                await self._log_event(
                    state,
                    current_tool,
                    status="failed",
                    error_type="invalid_ask",
                    approval="not_required",
                    content_chars=len(problem),
                )
                return result
            # 不带 current_step：那是审批面板的遗留样式（恒为 "1/1"），提问面板不需要。
            decision = interrupt(
                {
                    "type": GATE_ASK_USER,
                    "why": request["why"],
                    "question": request["question"],
                    "options": request["options"],
                    # 模态透传给面板：它据此提示"可多选"、并决定输入多个序号时收下还是重问
                    "select": request["select"],
                    "tool_call_id": current_tool.get("id"),
                }
            )
            # 回答**不回 messages**（不像被拒那样在此地造 ToolMessage）：写进 state 的 ask_answers，
            # 由 ask_user 工具（下游 OrchestrateNode 执行）按 tool_call_id 取回、渲染成回执。
            # 闸门的产出是 state，执行器消费 state——与 approved_* 队列同一条路子。
            ask_answers = dict(state.get("ask_answers") or {})
            ask_answers[current_tool.get("id")] = gates.normalize_ask_answer(
                decision, request["options"]
            )

        # 按 tool.json 的 source 分流：编排类进 orchestrate 队列，其余进普通工具队列
        is_orchestrate = tool_cfg.get("source") in self.ORCHESTRATE_SOURCES

        approved_orchestrate = list(state.get("approved_orchestrate_calls", []))
        approved_calls = list(state.get("approved_tool_calls", []))
        if approved:
            # 带审批结果下放：event log 用它区分 approved / not_required，不必在 ToolNode
            # 再猜一次策略。普通工具执行、编排工具执行都读这把字段。
            routed = dict(current_tool)
            routed["_approval"] = "approved" if action == "review" else "not_required"
            (approved_orchestrate if is_orchestrate else approved_calls).append(routed)

        remaining = pending[1:]

        result: dict = {
            "pending_tool_calls": remaining,
            "approved_orchestrate_calls": approved_orchestrate,
            "approved_tool_calls": approved_calls,
        }
        if ask_answers is not None:
            result["ask_answers"] = ask_answers
        if denied_messages:
            result["messages"] = denied_messages
        return result


