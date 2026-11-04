"""轮内压缩引擎：read_file 去重 + 批级移动窗口 + 状态回执变薄。

区域边界（2026-10-20 定，与轮末 `CompactNode` 互不相交）：
    CompactNode  : messages[:last_human_index]        旧轮，轮末无条件执行
    CompressNode : messages[last_human_index + 1:]    当前轮，工具循环中执行

轮内**不删消息**：图路由中间删掉结果会破坏 tool_call / ToolMessage 配对与调用方状态，
因此只改 `ToolMessage.content` 与旧写操作的 `tool_call.args`（Phase 2；id / 配对不动）。
本模块只产出替换后的消息，state 合并由 `nodes.py::CompressNode` 适配器负责。
"""
from __future__ import annotations

import posixpath
import re

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from . import parser, planner, policy
from .models import ToolBlock, ToolCallInfo
from .planner import CycleBatchPlanner

# read_file 去重的最小正文长度：被替换的旧正文太短时，指针和正文差不多，不值得换。
READ_DEDUP_MIN_CHARS = 1000
# 轮内移动窗口的默认水位（当前轮**可压 ToolMessage 正文**字符数）。高水位触发、
# 低水位停止；滞回避免每拍都改前缀、打碎 prompt cache。阈值不含稳定前缀、AI 参数与
# 受保护/失败/写回执——那些不是可压池，压不动也不该压。
TURN_HIGH_WATERMARK_CHARS = 50000
TURN_LOW_WATERMARK_CHARS = 25000
# 默认保留最近 K 个 block；再旧的才压。按 block 而不是按消息，避免“一个块里多条结果”
# 让保留量随工具数漂移。
TURN_KEEP_BLOCKS = 8
# 单个批的上下界：低于 min 指针不划算；高于 max 一次改写的影响面太大。
MAX_BATCH_CHARS = 8000
MIN_BATCH_CHARS = 300
# 被压正文的最小长度：太短的正文压成指针不省，反而多一行噪声。
MASK_MIN_CHARS = 300


class TurnCompressor:
    """当前轮压缩引擎。纯确定性、不调 LLM、不碰 state。"""

    # read_file 去重：只处理这一种工具；指针里用最后一次的 tool_call_id 定位正文。
    READ_DEDUP_TOOL = "read_file"
    DEDUP_MARKER_PREFIX = "…[重复读取]"
    # 移动窗口的指针标记；幂等判定同时认这两个前缀。
    MASK_MARKER_PREFIX = "…[已压缩]"

    def __init__(
        self,
        read_dedup_min_chars: int = READ_DEDUP_MIN_CHARS,
        high_watermark_chars: int = TURN_HIGH_WATERMARK_CHARS,
        low_watermark_chars: int = TURN_LOW_WATERMARK_CHARS,
        keep_blocks: int = TURN_KEEP_BLOCKS,
        mask_min_chars: int = MASK_MIN_CHARS,
        max_batch_chars: int = MAX_BATCH_CHARS,
        min_batch_chars: int = MIN_BATCH_CHARS,
    ) -> None:
        """`read_dedup_min_chars`：被替换的旧 read_file 正文不短于它才指针化。

        `high/low_watermark_chars`：可压 ToolMessage 正文的高/低水位（字符数）；
        `keep_blocks`：保留最近 K 个 block；`mask_min_chars`：单条被压正文的最小长度；
        `max_batch_chars` / `min_batch_chars`：单个批的上下界（语义切点优先，预算是兜底）。
        """
        if read_dedup_min_chars < 1:
            raise ValueError("read_dedup_min_chars 至少为 1")
        if not 0 < low_watermark_chars < high_watermark_chars:
            raise ValueError("low_watermark_chars 必须大于 0 且小于 high_watermark_chars")
        if keep_blocks < 0:
            raise ValueError("keep_blocks 不能为负")
        if mask_min_chars < 1:
            raise ValueError("mask_min_chars 至少为 1")
        if min_batch_chars < 1:
            raise ValueError("min_batch_chars 至少为 1")
        if max_batch_chars < min_batch_chars:
            raise ValueError("max_batch_chars 不能小于 min_batch_chars")
        self.read_dedup_min_chars = read_dedup_min_chars
        self.high_watermark_chars = high_watermark_chars
        self.low_watermark_chars = low_watermark_chars
        self.keep_blocks = keep_blocks
        self.mask_min_chars = mask_min_chars
        self.max_batch_chars = max_batch_chars
        self.min_batch_chars = min_batch_chars
        self._planner = CycleBatchPlanner(max_batch_chars, min_batch_chars)
        # 编排/state 工具与轮末共享同一份策略源：read_note / read_event_log 等回执
        # 在轮内也不能被批级 mask（§2.2 的两套策略漂移，2026-11-04 统一）。
        self._orchestrate_tools = policy.orchestrate_tools()

    def compress(self, messages: list, current_plan=None) -> list:
        """返回当前轮需要替换的消息（ToolMessage / AIMessage，按原顺序）；无需压缩时 []。

        AIMessage 替换只用于 Phase 2 的写操作 args 清理——tool_call 本身与 tool_call_id
        不变，只把 content / old_string / new_string 换成带长度的指针。
        """
        if not messages:
            return []

        anchor = self._last_human_index(messages)
        if anchor < 0:
            return []

        region_start = anchor + 1
        args_by_call = self._read_args_by_call(messages, region_start)

        # id → 替换后的 ToolMessage；后面的规则按 id 覆盖，最终按原消息顺序写出。
        by_id: dict[str, ToolMessage] = {}
        for message in self._dedupe_reads(messages, region_start, args_by_call):
            by_id[message.id] = message

        # 状态回执变薄仍按“当前轮总 content”触发：它不计入可压池，但旧回执会一直重放。
        turn_chars = self._turn_chars(messages, region_start)
        if turn_chars > self.high_watermark_chars:
            projected = turn_chars - self._saved(messages, by_id)
            if projected > self.low_watermark_chars:
                for message in self._slim_state_receipts(
                    messages, region_start, args_by_call, by_id, projected
                ):
                    by_id[message.id] = message

        # 批级压缩只看“可压 ToolMessage 正文”；先把去重/回执替换应用到工作副本。
        working = [by_id.get(getattr(message, "id", None), message) for message in messages]
        protected_ids = self._dedup_target_ids(working, region_start)
        blocks, boundaries = parser.iter_tool_blocks(working, current_plan or [])
        region_blocks = [block for block in blocks if block.start >= region_start]
        self._mark_eligibility(region_blocks, protected_ids)

        compressible = sum(block.eligible_chars for block in region_blocks)
        if compressible > self.high_watermark_chars:
            plan_blocks = region_blocks[: max(0, len(region_blocks) - self.keep_blocks)]
            remaining = compressible
            for batch in self._planner.plan_batches(plan_blocks, boundaries):
                for message in self._mask_batch(batch):
                    by_id[message.id] = message
                # 滞回：逐批压，一旦回到低水位就停手；后面的批保持原文。
                remaining -= planner.CycleBatchPlanner.batch_chars(batch)
                if remaining <= self.low_watermark_chars:
                    break

        return [
            by_id[message.id]
            for message in messages[region_start:]
            if getattr(message, "id", None) in by_id
        ]

    # ---------- 区域 / 预算 ----------

    @staticmethod
    def _last_human_index(messages: list) -> int:
        """当前轮起点：最后一条 HumanMessage 的下标；没有则 -1。"""
        for index in range(len(messages) - 1, -1, -1):
            if isinstance(messages[index], HumanMessage):
                return index
        return -1

    @staticmethod
    def _turn_chars(messages: list, region_start: int) -> int:
        """当前轮消息 content 的字符总数（压缩预算的计量口径）。"""
        return sum(
            len(str(getattr(message, "content", "") or ""))
            for message in messages[region_start:]
        )

    @staticmethod
    def _saved(messages: list, by_id: dict[str, ToolMessage]) -> int:
        """已生成的替换相对原文节省的字符数（负节省按 0 计）。"""
        original = {m.id: m for m in messages if getattr(m, "id", None)}
        total = 0
        for mid, new in by_id.items():
            old = original.get(mid)
            if old is None:
                continue
            before = len(str(getattr(old, "content", "") or ""))
            after = len(str(getattr(new, "content", "") or ""))
            total += max(0, before - after)
        return total

    def _mark_eligibility(
        self,
        blocks: list[ToolBlock],
        protected_ids: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        """逐条标记可压性：ToolMessage 正文 + 旧写操作的大 args。

        block 只提供结构/消费/配对约束。失败/被拒、受保护工具（web/ask/dispatch/state）、
        写回执、未消费块、不完整块、已带压缩标记的消息，以及**被去重指针引用的最新读取**
        都不参与正文 mask；同一 block 里的其它成功结果仍可压。写回执虽然正文受保护，但
        其中的大 args（content / old_string / new_string）可压，单独计入同一可压池。
        """
        for block in blocks:
            block.eligible_chars = 0
            block_ok = block.complete and block.ids_ok and block.consumed
            for call in block.calls:
                call.eligible = False
                tool_message = call.tool_message
                if not block_ok or tool_message is None or call.status != "ok":
                    continue
                content = tool_message.content
                if not isinstance(content, str):
                    continue
                if (
                    call.name not in policy.NO_MASK_TOOLS
                    and call.name not in self._orchestrate_tools
                    and call.call_id not in protected_ids
                    and getattr(tool_message, "id", None)
                    and len(content) >= self.mask_min_chars
                    and not self._is_marked(content)
                ):
                    call.eligible = True
                    block.eligible_chars += len(content)

                # Phase 2：写操作的大 args 也可压（即使回执本身受保护）。与 content
                # 可压性独立，但同样只处理已消费块里的成功调用。
                call.arg_chars = self._clearable_arg_chars(call)
                if call.arg_chars >= self.mask_min_chars:
                    call.arg_eligible = True
                    block.eligible_chars += call.arg_chars

    @staticmethod
    def _clearable_arg_chars(call: ToolCallInfo) -> int:
        """该调用可清的 args 字符数（已清过的字段返回 0，保证幂等）。"""
        fields = policy.ARG_CLEAR_FIELDS.get(call.name)
        if not fields:
            return 0
        total = 0
        for key in fields:
            value = call.args.get(key)
            if (
                isinstance(value, str)
                and value
                and policy.ARG_CLEAR_MARKER not in value
            ):
                total += len(value)
        return total

    @staticmethod
    def _clear_args(ai_message: AIMessage, calls: list[ToolCallInfo]) -> AIMessage:
        """把指定调用的写操作大字段换成带长度的指针；只改 args，不动 id / name。"""
        target_ids = {call.call_id for call in calls}
        new_calls = []
        for tool_call in ai_message.tool_calls:
            if tool_call.get("id") not in target_ids:
                new_calls.append(tool_call)
                continue
            name = (tool_call.get("name") or "").strip()
            fields = policy.ARG_CLEAR_FIELDS.get(name, ())
            args = dict(tool_call.get("args") or {})
            for key in fields:
                value = args.get(key)
                if (
                    isinstance(value, str)
                    and value
                    and policy.ARG_CLEAR_MARKER not in value
                ):
                    args[key] = (
                        f"{policy.ARG_CLEAR_MARKER} 原 {len(value)} 字符；文件为准]"
                    )
            new_calls.append({**tool_call, "args": args})
        return ai_message.model_copy(update={"tool_calls": new_calls})

    @staticmethod
    def _has_later_ai(region: list, index: int) -> bool:
        """这条消息之后、下一条 Human 之前是否已出现 AIMessage（是否已被模型消费）。"""
        for message in region[index + 1:]:
            if isinstance(message, AIMessage):
                return True
            if isinstance(message, HumanMessage):
                return False
        return False

    def _is_marked(self, content: str) -> bool:
        return (
            self.DEDUP_MARKER_PREFIX in content
            or self.MASK_MARKER_PREFIX in content
        )

    def _dedup_target_ids(self, messages: list, region_start: int) -> set[str]:
        """当前轮里被去重指针引用的 tool_call_id。

        这些消息是“某文件最后一次成功读取”，是旧指针的解析目标；批级 mask 必须
        放过它们，否则指针会指向一条已被压成短戳的消息（Stage 1 修复）。
        """
        ids: set[str] = set()
        for message in messages[region_start:]:
            if not isinstance(message, ToolMessage):
                continue
            content = message.content
            if not isinstance(content, str) or self.DEDUP_MARKER_PREFIX not in content:
                continue
            match = re.search(r"tool_call_id=([^\s，,）)]+)", content)
            if match:
                ids.add(match.group(1))
        return ids

    # ---------- read_file 重复读取：最后一次留正文，其余变指针 ----------

    @staticmethod
    def _read_args_by_call(messages: list, region_start: int) -> dict[str, dict]:
        """当前轮 tool_call_id → 调用参数（read_file 的 path 用来确认"同一个文件"）。"""
        args_by_call: dict[str, dict] = {}
        for message in messages[region_start:]:
            if not isinstance(message, AIMessage):
                continue
            for call in getattr(message, "tool_calls", None) or []:
                if isinstance(call, dict) and call.get("id"):
                    args = call.get("args")
                    args_by_call[call["id"]] = args if isinstance(args, dict) else {}
        return args_by_call

    def _dedupe_reads(
        self,
        messages: list,
        region_start: int,
        args_by_call: dict[str, dict],
    ) -> list[ToolMessage]:
        """同一轮内同一文件的多次 read_file：最后一次成功读取留正文，其余换成指针。

        与旧实现的区别：**不再比较正文**。文件是随时可能变的资源，内容相同只是巧合；
        真正可信的是时间上最后一次读取。失败/被拒的读取不参与替换（既不当目标，也不被替换），
        避免把一个错误回执当成"最新内容"。

        带行区间时加一道覆盖检查：最后一次必须覆盖旧读取的行范围，否则保留旧正文——
        例如"全文读 → 编辑 → 只复查第 10 行"时，不能把全文读换成第 10 行的指针。
        """
        entries: list[dict] = []
        for message in messages[region_start:]:
            if not isinstance(message, ToolMessage):
                continue
            name = (getattr(message, "name", "") or "").strip()
            if name != self.READ_DEDUP_TOOL:
                continue
            content = message.content
            if not isinstance(content, str):
                continue

            call_id = message.tool_call_id
            args = args_by_call.get(call_id) if call_id else None
            if not isinstance(args, dict):
                continue
            path = args.get("path")
            if not isinstance(path, str) or not path.strip():
                continue

            extra = message.additional_kwargs or {}
            ok = not extra.get("error_type") and extra.get("decision") != "denied"
            entries.append(
                {
                    "message": message,
                    "call_id": call_id,
                    "path": self._norm_path(path),
                    "display_path": path.strip(),
                    "content": content,
                    "range": parser.read_range(args),
                    "ok": ok,
                }
            )

        # 每个路径取时间上最后一次成功读取（倒序扫描；已指针化/已 mask 的不能当目标）
        latest_by_path: dict[str, dict] = {}
        for entry in reversed(entries):
            if not entry["ok"] or self._is_marked(entry["content"]):
                continue
            latest_by_path.setdefault(entry["path"], entry)

        replacements: list[ToolMessage] = []
        for entry in entries:
            latest = latest_by_path.get(entry["path"])
            if latest is None or latest is entry:
                continue
            if not entry["ok"]:
                continue
            if self._is_marked(entry["content"]):  # 幂等：已指针化 / 已压缩
                continue
            if len(entry["content"]) < self.read_dedup_min_chars:
                continue
            if not entry["call_id"] or not latest["call_id"]:
                continue
            if not self._covers(latest["range"], entry["range"]):
                continue

            replacements.append(
                entry["message"].model_copy(
                    update={
                        "content": self._dedup_pointer(
                            latest["call_id"],
                            latest["display_path"],
                            len(latest["content"]),
                        )
                    }
                )
            )
        return replacements

    @staticmethod
    def _covers(
        latest: tuple[int, int | None] | None,
        earlier: tuple[int, int | None] | None,
    ) -> bool:
        """最后一次读取是否覆盖旧读取请求的范围。

        - 全文请求只与全文请求互替：大文件的"全文"实际是前 200 行预览，不能声称覆盖区间；
        - 区间读取要求 latest 起点不晚于 earlier，且终点不早于 earlier；
        - `end=None`（到文件尾/单次 200 行上限）只与同为 `end=None` 的请求互认。
        """
        if latest is None:
            return earlier is None
        if earlier is None:
            return False

        latest_start, latest_end = latest
        earlier_start, earlier_end = earlier
        if latest_start > earlier_start:
            return False
        if latest_end is None:
            return earlier_end is None
        if earlier_end is None:
            return False
        return latest_end >= earlier_end

    @staticmethod
    def _norm_path(path: str) -> str:
        """路径归一（只用于判等）：统一分隔符、消掉 `./` 与冗余 `..`。"""
        return posixpath.normpath(path.strip().replace("\\", "/"))

    def _dedup_pointer(self, latest_call_id: str, path: str, length: int) -> str:
        """指针正文：指向最后一次成功读取，不复制原文。"""
        return (
            f"{self.DEDUP_MARKER_PREFIX} 同一文件以 tool_call_id={latest_call_id} 的"
            f"最后一次成功读取为准（{path}，{length} 字符）；此处旧结果不再展开。"
        )

    # ---------- 状态回执：每个工具只留最近一条 ----------

    def _slim_state_receipts(
        self,
        messages: list,
        region_start: int,
        args_by_call: dict[str, dict],
        by_id: dict[str, ToolMessage],
        projected: int,
    ) -> list[ToolMessage]:
        """把旧的状态回执压成一行；每个工具保留最近一条。"""
        region = messages[region_start:]
        entries: list[tuple[int, ToolMessage, str]] = []
        latest_by_tool: dict[str, int] = {}
        for index, message in enumerate(region):
            if not isinstance(message, ToolMessage):
                continue
            name = (getattr(message, "name", "") or "").strip()
            if name not in policy.STATE_RECEIPT_TOOLS:
                continue
            if not getattr(message, "id", None):
                continue
            latest_by_tool[name] = index
            entries.append((index, message, name))

        replacements: list[ToolMessage] = []
        for index, message, name in entries:
            if latest_by_tool.get(name) == index:
                continue
            if message.id in by_id:
                continue
            content = message.content
            if not isinstance(content, str) or self._is_marked(content):
                continue
            status, _ = parser.tm_status(message)
            if status != "ok":
                continue
            if not self._has_later_ai(region, index):
                continue
            pointer = self._state_pointer(name, args_by_call.get(message.tool_call_id) or {})
            saving = len(content) - len(pointer)
            if saving <= 20:
                continue
            replacements.append(message.model_copy(update={"content": pointer}))
            projected -= saving
            if projected <= self.low_watermark_chars:
                break
        return replacements

    def _state_pointer(self, name: str, args: dict) -> str:
        """状态回执的一行指针：保留"哪个工具、哪个目标、什么状态"。"""
        prefix = self.MASK_MARKER_PREFIX
        if name == "update_plan_step":
            return f"{prefix} update_plan_step · 步骤 {args.get('step_id', '?')} → {args.get('status', '?')}"
        if name == "create_plan":
            steps = args.get("steps") or []
            return f"{prefix} create_plan · {len(steps)} 步"
        if name == "read_memory":
            return f"{prefix} read_memory · 记忆已在 RUNTIME_STATE 注入"
        if name == "write_memory":
            key = args.get("key") or args.get("type") or "已写入"
            return f"{prefix} write_memory · {key}（正文在 memory.md）"
        if name in {"get_skill", "drop_skill"}:
            return f"{prefix} {name} · {args.get('name', '?')}"
        return f"{prefix} {name}"

    # ---------- 批级移动窗口：语义切点优先，预算兜底 ----------

    def _mask_batch(self, batch: list[ToolBlock]) -> list:
        """把一批里的 eligible 内容压成批级时间线；同时清理写操作的大 args。"""
        content_calls = [call for block in batch for call in block.calls if call.eligible]
        arg_blocks = [
            (block.ai_message, [call for call in block.calls if call.arg_eligible])
            for block in batch
        ]
        arg_blocks = [(ai_message, calls) for ai_message, calls in arg_blocks if calls]
        if not content_calls and not arg_blocks:
            return []

        replacements: list = []
        if content_calls:
            pointer = self._batch_pointer(content_calls)
            stub = f"{self.MASK_MARKER_PREFIX} 同批已压缩"
            for index, call in enumerate(content_calls):
                tool_message = call.tool_message
                if tool_message is None:
                    continue
                replacements.append(
                    tool_message.model_copy(
                        update={"content": pointer if index == 0 else stub}
                    )
                )

        for ai_message, calls in arg_blocks:
            replacements.append(self._clear_args(ai_message, calls))
        return replacements

    def _batch_pointer(self, calls: list[ToolCallInfo]) -> str:
        """批级时间线：工具×次数 + 目标/关键行；不复制正文。"""
        counts: dict[str, int] = {}
        groups: dict[str, list[str]] = {}
        order: list[str] = []
        for call in calls:
            name = call.name
            counts[name] = counts.get(name, 0) + 1
            if name not in groups:
                groups[name] = []
                order.append(name)
            pieces: list[str] = []
            target = self._mask_target(name, call.args)
            if target:
                pieces.append(target)
            if call.tool_message is not None:
                key = self._key_line(call.tool_message, name)
                if key:
                    pieces.append(key)
            if pieces:
                groups[name].append(" · ".join(pieces))

        parts: list[str] = []
        for name in order:
            details = groups[name][:3]
            suffix = "；".join(details)
            if len(groups[name]) > 3:
                suffix += "；…"
            part = f"{name}×{counts[name]}"
            if suffix:
                part += f"（{suffix}）"
            parts.append(part)
        text = (
            f"{self.MASK_MARKER_PREFIX} "
            + " · ".join(parts)
            + " · 需要时重新读取/执行"
        )
        return text if len(text) <= 600 else text[:597] + "…"

    @staticmethod
    def _mask_target(name: str, args: dict) -> str:
        """指针里的目标（路径 / 命令 / 查询 / 进程 id），尽量让人和模型都能定位。"""
        if name == "read_file":
            path = args.get("path")
            if isinstance(path, str) and path.strip():
                rng = parser.read_range(args)
                if rng is None:
                    return f"path={path.strip()} · 全文"
                start, end = rng
                if end is None:
                    return f"path={path.strip()} · 第 {start} 行起"
                return f"path={path.strip()} · 第 {start}-{end} 行"
        if name == "search_content":
            path = args.get("path", ".")
            query = args.get("query", "")
            return f"path={path} · query={query}"
        if name == "run_command":
            return f"cmd={parser.one_line(str(args.get('command', '')), 80)}"
        if name in {"process_wait", "process_read", "process_kill"}:
            return f"process_id={args.get('process_id', '?')}"
        if name in {"list_dir", "get_directory_tree"}:
            return f"path={args.get('path', '.')}"
        if name == "glob":
            return f"pattern={args.get('pattern', '?')} · path={args.get('path', '.')}"
        if name in {"write_file", "edit_file", "create_file", "create_dir", "delete_file", "delete_dir"}:
            return f"path={args.get('path', args.get('dir_path', '?'))}"
        if name == "copy_path":
            return f"{args.get('source', '?')} → {args.get('destination', '?')}"
        if name == "read_note":
            return f"note={args.get('note_id', '?')}"
        return ""

    @staticmethod
    def _key_line(message: ToolMessage, name: str) -> str:
        """只给需要"结果摘要"的工具取首行；文件/目录类已有 target，不重复。"""
        if name not in {
            "run_command", "search_content", "start_process",
            "process_wait", "process_read", "process_list",
        }:
            return ""
        text = str(message.content or "").strip()
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if not first:
            return ""
        return first if len(first) <= 80 else first[:80] + "…"
