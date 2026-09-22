"""终端前端：实现基座的 `UI` 协议——stdin 取输入、标准输出渲染事件、闸门面板收人的决定
（审批 = y/n；提问 = 选项序号（可多选，见 `select`）+ 一句补充）。

位置：`app/tui/ui.py`（2026-09-10 基座/前端分家后新增）。本类是**协议的唯一实现**（将来
web 前端是另一个实现），基座（`app/platform/loop.py`）只认协议、不认识本类。

输入通道的取消语义（协议要求"被取消不丢输入"）：stdin 由**独立 reader 线程**独占读、
经 pump 转进 asyncio 队列；`read_line` 只是 `queue.get()`——被基座 cancel 时队列内容不丢。
"""
from __future__ import annotations

import asyncio
import queue

from app.schema.agent_schema import ASK_SELECT_MANY
from app.schema.approval_schema import DECISION_ANSWER, DECISION_APPROVAL, GATE_ASK_USER, Decision
from app.schema.ui_schema import (
    Notice,
    ReadyForInput,
    SessionStarted,
    TurnFailed,
    TurnFinished,
)
from app.tui.input import _pump_stdin, _start_stdin_reader
from app.tui.panels import (
    AGENT_TITLE,
    COMMAND_TITLE,
    QUESTION_TITLE,
    USER_TITLE,
    format_ask,
    format_tool_approval,
    render_markdown,
)


# 提问面板的序号分隔符：半角/全角逗号与空白（中文输入法下最容易敲出的就是全角逗号）。
# **不含区间（`1-3`）**：多一个语法面就要多一条测试，且与"补充里写的话"边界更模糊。
_INDEX_SEPARATORS = str.maketrans({",": " ", "，": " "})


def parse_option_indexes(text: str) -> list[int] | None:
    """把面板输入解析成**1 起始**的序号列表；`None` = 这串不是序号（调用方当补充文本）。

    判据是**整串只由"数字 + 分隔符"组成**——宽进：`1 个就好` 这类含其它字符的一律当文本，
    同 `@图片` 那条"不像就放行"的取舍。空串 / 只有分隔符 → `[]`（= 没给序号）。

    ⚠️ 用 `isdecimal()` 而**不是 `isdigit()`**：后者对 `²`（Numeric_Type=Digit）也返回 True，
    而 `int("²")` 抛 ValueError——用户从文档里粘一个上标字符进来就会炸掉整个面板。
    `isdecimal()` 为真 ⟺ `int()` 一定解析得动（全角数字 `１` 两者都认，照收）。
    """
    tokens = text.translate(_INDEX_SEPARATORS).split()
    if not tokens:
        return []
    if not all(token.isdecimal() for token in tokens):
        return None
    return [int(token) for token in tokens]


class TerminalUI:
    """终端前端：stdin 线程 + 标准输出渲染 + 审批面板。

    结构上即 `app/platform/ui.py::UI` 协议的实现（Python 的类型检查是结构化的，无需继承）；
    基座只按协议调用 emit / read_line / decide 三个方法。
    """

    def __init__(self) -> None:
        self._reader_q: queue.Queue | None = None
        self._out_q: asyncio.Queue | None = None
        self._pump: asyncio.Task | None = None
        # 输入通道是否已 EOF（stdin 关了 / 管道耗尽）：EOF 之后队列再不会有新内容，
        # 谁再 await 它就是永久阻塞——见 _take_line。
        self._eof = False
        # **面板出现之前**就已排队的行（= 用户在模型思考时预打的下一条消息）。它们属于下一个
        # turn，不属于这张面板：decide 会把它们挪到这里，read_line 优先从这里取。不这么做的话
        # 预输入会被当成闸门的答案——不匹配就成了"拒绝"，而且那行字永久消失（2026-09-15 审计）。
        self._held: list[str] = []

    async def start(self) -> None:
        """起输入通道（单 stdin reader 线程 + pump 任务）；需在事件循环内调用。"""
        self._reader_q = _start_stdin_reader()
        self._out_q = asyncio.Queue()
        self._pump = asyncio.create_task(_pump_stdin(self._reader_q, self._out_q))

    async def aclose(self) -> None:
        """收尾输入通道（停 pump；reader 线程是 daemon，随进程结束）。

        cancel 之后要 **await 掉**那个任务：只 cancel 不 await，asyncio 会在循环关闭时
        报 "Task was destroyed but it is pending!"（噪声，且让退出看起来像出错）。
        """
        if self._pump is not None:
            self._pump.cancel()
            try:
                await self._pump
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 —— 取消是预期路径
                pass
            self._pump = None

    async def _take_line(self) -> str | None:
        """从输入队列取一行；**EOF 后立即返回 None**，不再 await（否则永久阻塞）。

        EOF 由 pump 放进队列的 None 哨兵标记——EOF 之后 pump 就返回了，队列再不会有东西，
        此时若还有一轮 turn 要审批（`decide`），干等就是死锁（只能 Ctrl+C）。
        """
        if self._eof or self._out_q is None:
            return None
        line = await self._out_q.get()
        if line is None:
            self._eof = True
        return line

    # ---------- UI 协议 ----------

    def emit(self, event) -> None:
        """把基座事件渲染成终端输出（只显示，不改状态）。"""
        if isinstance(event, SessionStarted):
            print(f"[会话] session_id={event.session_id}")
            print(f"[工作区] {event.workspace}（首次输入时才会建库）")
        elif isinstance(event, ReadyForInput):
            print(USER_TITLE)
        elif isinstance(event, TurnFinished):
            print(AGENT_TITLE)
            # 模型答复按 markdown 渲染（可选依赖 rich，见 panels.render_markdown）；
            # 渲染不了就原样打——答复绝不能因为显示层而丢失。
            if not render_markdown(event.text):
                print(event.text)
        elif isinstance(event, TurnFailed):
            print(f"\n[agent 运行出错] {event.message}")
        elif isinstance(event, Notice):
            print(f"\n[提示] {event.text}")

    async def read_line(self) -> str | None:
        """取一行用户输入；None = EOF。可被基座 cancel 而不丢输入（队列内容仍在）。

        先取 `_held`（审批期间被挡下的预输入），再取队列——保证那几行按原顺序走进下一个 turn。
        """
        if self._held:
            return self._held.pop(0)
        return await self._take_line()

    async def decide(self, value: dict) -> Decision:
        """按闸门种类问人：审批收 y/n，提问收"选项 + 补充"两段；返回他给的决定。

        面板出现**之前**就已排队的行（用户在模型思考时预打的下一条消息）先挪进 `_held`，留给
        下一个 turn——否则它们会被当成答案：不匹配就成了"拒绝"，而且那行字永久消失。
        这段挪动与下面的打印之间**没有 await**，pump 不可能插进来把答案混进这批旧的。

        EOF（stdin 关了 / 管道耗尽）时没人能回答，按各类闸门的 fail-closed 语义收场
        （见 `app/platform/ui.py::UI.decide` 的 EOF 契约）：否则这里会永久阻塞
        （pump 已结束，队列不会再有内容）。
        """
        ask = value.get("type") == GATE_ASK_USER
        stale = self._out_q.qsize() if self._out_q is not None else 0
        print(QUESTION_TITLE if ask else COMMAND_TITLE)
        print(format_ask(value) if ask else format_tool_approval([value]))
        print()
        self._hold_pretyped(stale)
        return await (self._ask(value) if ask else self._approve())

    def _hold_pretyped(self, stale: int) -> None:
        """把面板出现**之前**就已排队的行挪进 `_held`，留给下一个 turn（见 `decide` 的说明）。

        调用点必须紧跟在"面板已经打印完"之后、任何 await 之前：这样 pump 没机会把用户对面板的
        回答混进这批旧行里。
        """
        if not stale:
            return
        for _ in range(stale):
            if self._out_q is None:
                return
            item = self._out_q.get_nowait()
            if item is None:
                self._eof = True  # EOF 哨兵：别把它当成一行输入塞给下一个 turn
                break
            self._held.append(item)
        if self._held:
            print(f"（面板出现前你已输入 {len(self._held)} 行，已留给下一个回合）")

    async def _prompt_line(self, prompt: str) -> str | None:
        """打一行提示并取一行输入；返回 None = EOF（输入流结束，不会再有人回答）。"""
        print(prompt, end="", flush=True)
        return await self._take_line()

    async def _approve(self) -> Decision:
        """收 y/n；EOF 按**不批准**处理（fail-closed：没人批 = 不执行）。"""
        line = await self._prompt_line("是否批准该操作？(y/n): ")
        if line is None:
            print("（输入已结束，无人确认——按未批准处理）")
            return Decision(kind=DECISION_APPROVAL, approved=False)
        return Decision(kind=DECISION_APPROVAL, approved=line.strip().lower() in ("y", "yes", "是"))

    async def _ask(self, value: dict) -> Decision:
        """收回答的**两段**：先选选项（可跳过），再补一句（可留空）。

        第一问按题目的模态（`value["select"]`）收：

        - **不是序号但是文本** → 用户懒得先选、直接说了方案：整串当补充，**跳过第二问**；
        - 空回车 → 不选，继续问补充；
        - `select="many"`：逗号（半角/全角）或空格分隔多个序号，**越界的丢掉、保留合法的**；
        - `select="one"`：只认**恰好一个**合法序号；给了多个（`1,3`）或越界（`9`）都**提示并重问**
          ——单选容不下第二个有效态，静默丢掉一半比让人重输更糟。重问是**可逃逸**的：空回车跳过、
          任意文本当补充、EOF 落回"未回答"，所以不会把人困住；
        - 补充可留空。**两段皆空 = 未回答**（与 EOF 同语义），由模型自己带假设继续。

        这里的 `value["options"]` 是**闸门归一后**那份清单（`app/agent/gates.py::ask_request` 已去掉空白项），
        所以面板上显示的序号与闸门回写 `ask_answers` 时校验的序号是同一套——别在这边再过滤一遍，
        否则两边会错位。
        """
        options = [str(item) for item in (value.get("options") or [])]
        many = value.get("select") == ASK_SELECT_MANY   # 词表归 schema，别在这里写字面量

        indexes: list[int] = []
        while True:
            line = await self._prompt_line("选择序号（回车 = 不选）: ")
            if line is None:
                print("（输入已结束，无人作答——按未回答处理）")
                return Decision(kind=DECISION_ANSWER)

            text = line.strip()
            raw = parse_option_indexes(text)
            if raw is None:  # 不是序号列表 → 整串当补充，跳过第二问
                return Decision(kind=DECISION_ANSWER, supplement=text)
            if not raw:  # 空回车 / 只有分隔符 → 不选
                break

            valid = [number - 1 for number in raw if 1 <= number <= len(options)]
            if many:
                indexes = list(dict.fromkeys(valid))  # 越界项已丢；去重保序（`1,1` → 只留一个）
                # 宽容但**不静默**：照常收下合法项，只是告诉人丢掉了什么——不然用户敲了
                # `1,9` 会以为 9 也算数了（取向同单选那边的"提示"，只是不拦着重输）
                dropped = [number for number in raw if not 1 <= number <= len(options)]
                if dropped:
                    print(f"（已忽略超出范围的序号：{'、'.join(str(n) for n in dropped)}）")
                break
            if len(raw) > 1:
                print("（这是单选题，只能选一项；请重新输入一个序号）")
                continue
            if not valid:
                print(f"（序号超出范围；本次共 {len(options)} 项，请重新输入）")
                continue
            indexes = valid
            break

        supplement_line = await self._prompt_line("补充说明（可留空）: ")
        if supplement_line is None and not indexes:
            print("（输入已结束，无人作答——按未回答处理）")
            return Decision(kind="answer")
        # 第二问 EOF 但第一问已有选择：**保留那半份回答**（半份比没有好）
        return Decision(
            kind="answer",
            option_indexes=indexes,
            supplement=(supplement_line or "").strip() or None,
        )
