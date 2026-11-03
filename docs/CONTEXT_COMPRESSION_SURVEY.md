# 上下文压缩 / 上下文工程外部实践调研（2026-10-18，2026-10-24 代码核对修订）

> **性质**：外部实践调研，不是目标态设计。目的是为 Theta 当前 P0「图内 token 压缩」提供
> 可借鉴的机制、阈值与反直觉结论；与代码现状不一致处以代码为准。
> **一句话结论**：业界已经从"超限后做一次 LLM 摘要"演进到**分层、分级、可恢复**的压缩体系：
> 写入侧整形 → 观察遮蔽 → 文件卸载/按需取回 → 摘要压缩 → 结构化记忆/子 agent 隔离。
> 对本仓库最关键的两条是：
> 1. **简单观察遮蔽在编码 agent 上不输 LLM 摘要**（JetBrains，SWE-bench）；
> 2. **单轮长任务的折叠必须发生在轮内**——现有 `CompactNode._plan` 只折"最后一条
>    HumanMessage 之前"，对这次 44 次调用的单轮会话**一块也折不了**（见 §5.2）。

## 0. 对 Theta 的设计总览（当前草案，2026-10-18）

目标：把**每轮实际送入模型**的上下文压到高信号集合，同时不丢执行状态、失败信号与可恢复性。

```text
ToolResult
   │
   ├─[写入侧压缩] 终端输出头尾截断 + 完整日志落盘；文件读取不做中段截断
   ▼
ToolMessage（带 success / error_type / exit_code 戳）
   │
   ├─[轮内压缩] 旧工具块观察遮蔽：保留工具名/退出码/首尾行；失败块不遮蔽
   ▼
历史（最近 K 块 + 原始请求 + current_plan 原样）
   │
   ├─[摘要] 厂商标称窗口 40%–50% 触发：结构化摘要 + 原位替换
   ▼
下一轮 LLM 调用
```

| 层 | 触发 | 手段 | 目标 | 现状 |
| --- | --- | --- | --- | --- |
| 写入侧压缩 | 每次工具返回 | 终端输出头尾截断、剥进度、完整日志落盘 | 冗余日志 | **部分落地**（2026-10-20）：终端中间截断已下沉 `mcp_service/terminal.py::_compress_output`（超 `TEXT_BUDGET_CHARS` 保留头尾各 1/4）；**完整日志落盘/恢复路径未做** |
| 轮内压缩 | 工具循环中、按移动窗口 | 旧工具块观察遮蔽（保留元数据与首尾） | 已消费的旧观测 | **部分落地**（2026-10-20）：`CompressNode` 已接线（`tool/orchestrate → compress → llm`），当前只做**当前轮 read_file 同文件重复读取指针化**；**移动窗口遮蔽未做**，且是无条件必经边、无预算/delta 触发 |
| 摘要 | **厂商标称窗口 40%–50%** | LLM 结构化摘要 + `SystemMessage`/`RemoveMessage` 原位替换 | 已压缩多轮的语义历史 | **未做**：`CompactNode` 轮末无条件折叠（已不看 60K 预算门）仍是**确定性单行摘要**（工作日志·工具·状态），只折"最后一条 HumanMessage 之前"；单轮长任务一块折不了 |
| 记忆 / 技能 | 会话开始 / 按需 | 分层记忆注入；技能目录 + 按需正文 | 跨轮背景 | 已有（工作区记忆、skill 按需加载） |
| 子 agent | 任务分发 | worker 独立窗口、只回结论 | 上下文隔离 | 已有 `dispatch_subtasks` |
| 缓存友好排序 | 每次请求 | 稳定前缀 → 历史 → 动态尾部 | prefix cache 命中 | **已落地**（Phase 1，2026-10-20）：动态状态（plan/memory/handoff/skills）内联 `<RUNTIME_STATE>` 到最后一条 HumanMessage 尾部；稳定前缀只留 SYSTEM_PROMPT / 工作区 / 画像；`llm_usage` 日志记录 input/cache_read/hit/miss |
| 确定性事件日志 | 每次 tool_call | 每调用一行 JSONL 审计（状态戳/目标/args_hash/审批） | 上下文只放指针、审计可无上限落盘 | **已落地**（Phase 2，2026-10-20）：`app/resource/event_log.py` + 只读工具 `read_event_log` |

**不变量**：

- `status` / `exit_code` 永不截断，且排在日志正文之前；
- 失败块**不遮蔽、不折叠**（MVP），完整错误至少保留 `error_type`/退出码/首尾行；
- `tool_use`/`tool_result` 配对不能拆断；
- 在途块、原始 HumanMessage、`current_plan` 永不折叠；
- 可恢复：完整日志路径 / `notes#rN`；
- 缓存：写入侧压缩纯收益；轮内遮蔽批量低频；摘要一次性摊销；动态 plan/记忆放尾部；
- 触发：压缩连续做；摘要在**厂商标称窗口 40%–50%**。

**当前缺口（按优先级；2026-10-24 按代码核对修订，~~删除线~~ = 已解决）**：

1. ~~工具输出没有上限/落盘（本会话三份安装日志占累计 prompt 71.3%）~~ → 终端截断已落地；
   **残余**：被截正文**不落盘、无恢复路径**（§5.5 的 `full_output` 方案未做）；
2. `CompactNode._plan` 的折叠区是"最后一条 HumanMessage 之前"，单轮长任务一块也折不了；
3. 轮内遮蔽未做：`CompressNode` 只有 read_file 去重一条规则，无移动窗口、无预算/delta 触发
   （无条件必经边，不触发时返回 `{}` 白跑一拍）；
4. **失败块会被折**（2026-10-24 核对发现 `_plan` 不跳过 failed/denied 块；§5.9 MVP 只落了
   "识别"一半——戳有、`command_failed` 细化与 exit_code 未做、折叠跳过未做）；
5. ~~`current_plan` 注入在 messages 之前，plan 更新会破坏 prompt cache~~ → Phase 1 已改
   `RUNTIME_STATE` 内联 Human 尾部；
6. ~~没有 usage/缓存命中率观测~~ → `llm_usage` 日志已落地；**残余**：无聚合观测面板，
   缓存命中率要靠翻日志；
7. **Phase 3 未开始**：低频 checkpoint 触发 + recent K 窗口 + LLM digest + 确定性
   fallback + `summary_version` / `covers_until_message_id`（见 §8）。

**第一步（MVP）**：终端运行错误识别——大部分已存在（`ToolResult.success=False` →
`ToolNode` 打 `error_type` 戳），只需补两处：失败戳带出退出码/区分 `command_failed`；
`CompactNode._plan` 跳过含失败块的折叠。详见 §5.9。

## 1. 先划清压缩的对象、时机与约束

业界讨论"上下文压缩"时，实际在压四类不同的东西：

| 对象 | 典型占比 | 常用手段 |
| --- | --- | --- |
| 工具输出（命令/文件/检索结果） | 编码 agent 里通常最大 | 截断、遮蔽、落盘、按需取回 |
| 旧对话/工具块 | 随回合线性增长 | 摘要压缩（compaction）、滑动窗口 |
| 系统提示 / 记忆 / 画像 | 固定但每轮重发 | 预算分片、按需加载（skill）、分层记忆 |
| 中间推理/思考 | 模型相关 | thinking block clearing（Anthropic API） |

三个必须一起定义的约束：

1. **触发点**：按 token 比例（如 50%/70%/90%）、每 N 轮、工具循环中、溢出错误兜底、手动。
2. **保真**：不能破坏 `tool_use`/`tool_result` 配对；不能把"失败信号"摘要掉；被压内容最好
   可恢复（文件路径、URL、编号）。
3. **缓存**：改动历史前缀会让 prompt cache 失效。Manus 把 KV-cache 命中率称为生产 agent
   "最重要的单一指标"；因此出现了"只追加不删除""遮蔽而不移除""服务端 cache_edits"等做法。

## 2. 横向对照

| 系统 | 触发 | 压缩单元 | 机制 | 备注 |
| --- | --- | --- | --- | --- |
| **Anthropic API（context editing）** | 可配置 trigger / 手动 | 旧 `tool_result`、thinking blocks | `clear_tool_uses_20250919`、`clear_thinking_20251015`、`compact_20260112`；支持 `keep` / `clear_at_least` / `excludeTools` | 官方明确提示会失效缓存，用 `clear_at_least` 保证"值得清" |
| **Claude Code** | 接近窗口上限（约 92%）+ 溢出兜底 | 旧工具结果、整段历史 | 分级：snip（删旧消息）→ microcompact（清旧工具结果）→ auto-compact（LLM 摘要）→ reactive（`prompt_too_long` 兜底）→ context collapse | 具体实现来自第三方逆向，**非官方**；摘要后保留最近 5 个文件等上下文 |
| **Cursor** | 动态、按需 | 长工具输出、历史、MCP 工具说明 | "dynamic context discovery"：工具输出写文件让 agent 自己 tail；摘要时给历史文件引用；MCP 工具描述同步到目录，按需 grep | 官方称"调用过 MCP 的 run"总 token 降 **46.9%** |
| **OpenAI Codex CLI** | 约 90% 窗口 | 旧历史 | LLM 摘要 compaction；较新的 TokenBudget 路径直接换新窗口 + 剩余 token 提醒 | 阈值/保留 20K 用户消息等细节来自第三方逆向，**非官方** |
| **Gemini CLI** | 50%–70%（版本/配置而异） | 工具输出 + 旧历史 | 三阶段：工具输出按 token 预算从新到旧截到约 30 行并落临时文件 → LLM 摘要（保留最近 30%，落在 user 消息上）→ 摘要失败则只截断 | 阈值有 0.5/0.7/80%/95% 多种版本说法，需按版本核对 |
| **OpenHands** | 每步检查阈值 | 旧事件 | 可插拔 `Condenser`：`LLMSummarizingCondenser`、`ObservationMaskingCondenser`、`RecentEventsCondenser`、`PipelineCondenser` 等 | 官方称每轮 API 成本最多降 2×，SWE-bench 约持平（54% vs 53%） |
| **Roo Code / Cline** | 接近窗口上限（可配） | 旧对话 | 智能摘要 + `context_length_exceeded` 时的截断兜底；保留 slash commands/工作流 | 有摘要前后 token 数与摘要调用成本的审计展示 |
| **Aider** | 每次请求前 | 代码库上下文 | repo map：tree-sitter 抽符号 → PageRank 排序 → 二分拟合 token 预算（默认 1,024 tokens） | 只给"目录/骨架"，详细内容按需读；上下文利用率仅 4.3–6.5% |
| **Manus** | 设计期约束 | 工具结果、工具可见性 | KV-cache 优先；mask 不 remove；文件系统当外部记忆；todo.md 复述；保留错误；避免 few-shot 化 | 生产经验总结，不是开源实现 |
| **LangGraph / Deep Agents** | 阈值/中间件 | 旧消息 | `SummarizationNode` / `trim_messages` / `SummarizationMiddleware`；被逐出的消息可落文件供 `read_file` | 中间件在 `wrap_model_call` 里重建消息列表，不改图状态 |
| **ACON（Microsoft，研究）** | 压缩模块 | 观测 + 交互历史 | 用"全量成功/压缩失败"的轨迹对，让 LLM 迭代**压缩准则**；再蒸馏到小模型 | 峰值 token 降 26–54%，任务表现基本保持 |

## 3. 六类机制（按侵入性从低到高）

### 3.1 写入侧整形：在工具结果进入上下文之前就压

- **截断/头尾保留**：Gemini CLI 按 token 预算把旧工具输出截到约 30 行；Claude Code
  microcompact 把旧结果替换为占位符。
- **落盘 + 指针**：Cursor 把长 shell/MCP 输出写入文件，只给 agent 文件路径，让它自己
  `tail`/读；Gemini CLI 也把完整输出落到临时文件。好处是**无损可恢复**，压缩不再等于丢信息。
- **进度噪声清理**：pip/conda 的 `\r` 进度条、重复空行对模型几乎零信息量，属于最廉价的一刀。
- **按需描述**：Cursor 把 MCP 工具描述同步到目录，静态上下文只留工具名，按需检索；报告
  46.9% 的总 token 降幅。

### 3.2 观察遮蔽（observation masking）：保留结构，清空正文

- 做法：把超过窗口的**旧工具观测**替换为占位符，保留 `tool_use`/`tool_result` 结构、工具名、
  退出码、截断提示（如 `[test output, 2,847 tokens, exit 1, elided]`）。
- 代表：SWE-agent、OpenHands `ObservationMaskingCondenser`、Manus 的"mask, don't remove"
  （后者主要针对工具可见性/缓存，思想一致）。
- **关键证据**：JetBrains Research 的 *The Complexity Trap*（NeurIPS 2025 DL4Code）在
  SWE-bench Verified 上对比：
  - 观察遮蔽与 LLM 摘要在成本/解决率上**没有显著差异**，遮蔽始终位于效率前沿；
  - 某配置下比原始 agent **便宜 52% 且解决率高 2.6%**；
  - LLM 摘要会让轨迹**变长 13–15%**——摘要平滑掉了"该停手"的失败信号；
  - "遮蔽为主 + 选择性摘要"的混合再降 7%（相对纯遮蔽）、11%（相对纯摘要）。
- 适用边界：作者提醒实验集中在软件工程域，长而啰嗦的工具输出天然利于遮蔽；交互很简短的
  领域未必同样成立。

### 3.3 文件系统卸载 / 按需取回

- Manus："上下文窗口 = RAM，文件系统 = 磁盘"；压缩必须**可恢复**——丢网页内容要留 URL，
  丢文档正文要留文件路径。
- Anthropic：just-in-time retrieval，保留轻量标识（路径/URL/查询），需要时再取。
- Aider repo map 是另一端的极端：**从不预载正文**，只给 1K token 的符号骨架，让模型按需读。
- 与 §3.2 的关系：遮蔽负责"省 token"，落盘负责"省了还能拿回来"。

### 3.4 摘要压缩（compaction）

- 全量摘要：把旧历史压成一段结构化摘要，保留最近 N 条/最近 30% 原文。
- 结构化摘要的常见字段：目标、已确认事实、改过的文件、失败/未决、下一步计划。
- 代表：Claude Code auto-compact、Codex、Gemini CLI、OpenHands、Roo/Cline、LangGraph。
- **反直觉结论**：在编码 agent 上，摘要不一定优于遮蔽（§3.2）；它的价值主要在**跨轮次
  保留语义**（决策、目标、未决问题），而不是处理啰嗦工具输出。
- 失败模式：摘要调用本身可能超窗口（Codex 的阈值 bug 被指会把摘要请求挤到没有余量）；
  摘要会隐藏失败信号导致轨迹变长；摘要产生的新文本也可能再次膨胀（compress-expand-recompress）。

### 3.5 结构化笔记与记忆

- Anthropic：structured note-taking，把进度/决策写进 `NOTES.md`，重置上下文后读回。
- Manus：持续重写 `todo.md`，把全局计划推到近期注意力，对抗 "lost in the middle"。
- Theta 已有近似物：`notes`（轮末折叠归档）、`current_plan`（每轮回显）、长期记忆分层。
  区别是 `notes` 目前只在轮末生成，且只归档联网四件套。

### 3.6 子 agent 隔离

- Anthropic：主 agent 派发子任务，子 agent 可在自己的干净窗口里消耗几万 token，只回
  **约 1–2K token 的结论**——压缩发生在"跨进程边界"而不是上下文内部。
- Claude Code Task、Roo Orchestrator（只收 `attempt_completion` 摘要）同构。
- Theta 已有 `dispatch_subtasks`（worker 独立 session、只回结论），但 worker 能力受限
  （只读 + 联网 + 下放终端），不能写文件/推进计划。

### 3.7 学习式压缩与缓存友好

- ACON（Microsoft）：用"全量成功、压缩失败"的轨迹对让 LLM 迭代压缩准则，再把压缩器蒸馏到
  小模型；峰值 token 降 26–54%。
- KV-cache：Manus 把缓存命中率置于一切之上——稳定前缀、不塞时间戳、只追加、确定性序列化；
  缓存 token 与未缓存 token 可差 10× 价格。Anthropic 的 context editing 也提供
  `clear_at_least` 与 `cache_edits` 这类"清得足够多才值得破坏缓存"的机制。

## 4. 关键证据与反直觉结论

1. **遮蔽 ≥ 摘要（编码域）**：JetBrains 的 SWE-bench 实验是当前最直接的对照证据；摘要的
   优势在语义保留，不在削 token。
2. **上下文退化早于窗口上限**：Chroma 测 18 个模型，200K 窗口在约 50K 就明显退化；"够大"
   不等于"该塞满"。这也是"压缩越早越好"的依据。
3. **保留错误**：Manus 明确主张保留失败动作与 stack trace，让模型更新信念；摘要若把失败
   信号抹平，会延长轨迹（与 JetBrains 的 13–15% 轨迹变长互证）。
4. **压缩必须可恢复**：落盘/留路径/留 URL 的压缩几乎无信息损失；纯摘要不可逆。
5. **缓存是隐藏成本**：改历史前缀会失效缓存；分级压缩要按"清得够多才值得破坏缓存"设计。
6. **"压缩"与"摘要"是两级，触发点不同**：多数系统在 50%–90% 之间触发摘要；越晚留给
   摘要调用的余量越小（Codex 的 90% 阈值问题就是反例）。对 Theta 的口径：
   - **压缩**（截断/遮蔽/落盘）发生在**写入侧与轮内移动窗口**，连续、确定性、针对冗余的
     原始工具输出；
   - **摘要**（LLM 语义压缩）在**厂商标称窗口的 40%–50%** 触发。
   为什么不等到 80%–90%：到这个量级，历史已经过多次压缩，剩下的多是决策、结论、失败
   信号——**再压缩收益很小**，而且模型在长上下文的外推区效果不保证；此时该做的是摘要
   收口，不是继续削 token。分母按厂商标称窗口（见用户口径），实测更早退化只作为额外护栏，
   不把摘要点推后。
7. **单轮长任务需要轮内压缩**：多数系统的 masking/compaction 都是按"事件/工具窗口"推进的，
   不以 HumanMessage 为边界；这对 Theta 的现状是直接提醒（§5.2）。

## 5. 对 Theta 的映射

### 5.1 现状对照

| 能力 | Theta 现状 |
| --- | --- |
| 工具输出截断/落盘 | **无**；`run_command` 原样返回，`format_tool_result` 不截断 |
| 观察遮蔽 | **无** |
| 轮末摘要折叠 | 有：`CompactNode`，确定性、不调 LLM，只折"最后 HumanMessage 之前"的完整工具块 |
| 轮内（工具循环中）压缩 | **未落地**（TODO） |
| notes 归档 | 有，但仅联网四件套成功正文，且只在轮末折叠时产生 |
| 文件卸载/按需取回 | 无（技能正文/README 已是"按需读"，但工具结果没有） |
| 子 agent 隔离 | 有 `dispatch_subtasks`（独立 session、只回结论） |
| 结构化记忆 | 有长期记忆 + 画像 + 计划回显 |

### 5.2 这次会话暴露的判据问题（必须先定）

现有 `CompactNode._plan` 的折叠区是**最后一条 HumanMessage 之前**。这次会话只有一条
HumanMessage，44 次工具调用全部在当前轮内——所以即使把 `compact_node` 放进工具循环，
按现有判据仍然**一块也折不了**（终态 139,380 字符，`compact_node` 被路由到但返回 `{}`）。

结论：**"图内压缩"不能直接复用现有判据**。轮内压缩需要把折叠单元从"旧轮次"改成
"**已完成的工具块 + 移动窗口**"：

- 折叠对象：已兑现（每个 `tool_call` 都有对应 `ToolMessage`）且早于最近 K 个块的工具块；
- 永不折叠：当前在途块（有 `tool_call` 尚无 `ToolMessage`）、原始用户请求、当前计划；
- 保留信号：失败块的 `error_type`/退出码/首尾若干行（Manus 的"保留错误"+ JetBrains 的
  "摘要抹平失败信号会拉长轨迹"）；
- 可恢复：被折正文的摘要/路径写进 `notes`，需要时 `read_note`/`read_file` 取回；
- 触发分两级：**压缩**（写入侧 + 轮内移动窗口）持续、确定性；**摘要**在**厂商标称窗口
  的 40%–50%** 触发。到摘要点时剩余内容已被压缩多轮，再压缩收益很小，摘要负责语义收口。
  当前 `_CONTEXT_BUDGET_DEFAULT = 60000` 只是压缩侧的字符代理，不能当摘要阈值。

### 5.3 建议的落地顺序（按性价比）

1. **P0-a 写入侧整形**（最小改动、立刻见效）：在 `mcp_service/terminal.py::_render_finished`
   或 `app/platform/tool_results.py::format_tool_result` 做头尾截断、剥 `\r` 进度、连续空行；
   超长输出落盘并回传路径。本会话三份安装日志占累计 prompt 的 **71.3%**，截到 2K 字符可
   降 **67%**。
2. **P0-b 轮内确定性遮蔽**：按移动窗口把旧工具结果替换为占位符（保留工具名、退出码、
   首尾行、字符数），不调 LLM。对照 JetBrains：这是最稳的效率前沿。
3. **P0-c 摘要收口**：在**厂商标称窗口的 40%–50%** 触发（不是 60K 字符代理）；此时先做
   一次确定性遮蔽，再把剩余语义历史压成结构化摘要；保留最近 K 块与失败信号；摘要写入
   `notes` 原位替换（沿用现有 `SystemMessage + RemoveMessage` 机制）。
4. **P1 可恢复性**：被遮蔽/折叠的正文落 `notes` 或工作区日志文件，模型用 `read_note`/
   `read_file` 取回；这与 Cursor/Gemini CLI 的 file-first 一致。
5. **P2 缓存友好**：确认当前 provider 的缓存行为后再决定是否采用"只追加 + 遮蔽旧块"的
   策略；不要在未确认缓存语义前做大规模前缀重写。

### 5.4 设计不变量（建议写进实现前的验收）

- `tool_use`/`tool_result` 配对不能被拆断（LangGraph/OpenAI 都会 400）。
- 当前在途工具块、原始 HumanMessage、`current_plan` 永不折叠。
- 失败/拒绝信号至少保留结构化摘要（`error_type`、退出码、首尾行）。
- 折叠是幂等的：重复触发不应继续缩小或破坏已经折叠的内容。
- 触发口径分两层：压缩按写入侧/轮内移动窗口持续做；**摘要在厂商标称窗口的 40%–50% 触发**。
  当前 `60000` 字符只是压缩侧的代理，不能当摘要阈值；摘要必须能在工具循环中途触发。
- 折叠后要能回答："模型怎么拿回被折内容"（`notes#rN` / 文件路径）。

### 5.5 截断的语义安全（草案）：控制元数据与日志正文分开

**第一原则：执行状态永远不从可截断的文本里推断。** `run_command` 现在第一行就是
`exit_code: 0`；截断必须保证这一行与 `status` 永远保留、且排在输出正文之前。模型判断
"有没有下完"看的是 `status`/`exit_code`，不是"有没有看到最后一行日志"。

建议的结果形状：

```text
exit_code: 0
status: finished
duration: 1111.4s
truncated: true (kept first 40 + last 120 of 3842 lines; 3682 lines / 105169 chars omitted)
full_output: .theta/logs/run-20261018-044921-p27.log
--- output [head] ---
...
--- output [tail] ---
Successfully installed torch-2.8.0+cu128 torchaudio-2.8.0+cu128
```

- `status` 取 `finished` / `running` / `timeout`；`running` 仍走现有 `_render_running`
  （带 `process_id` 与 `process_wait` 指引），与"已完成"结构上不同。
- `truncated` 只描述**日志正文**，不影响 `status`；"中间省略 3682 行"是明确的，不是静默。
- `full_output` 落在工作区 `.theta/logs/`，模型需要时用 `read_file`/`search_content` 取回。

**按工具语义决定截断方式**（不要一刀切 head+tail）：

| 结果类型 | 保留 | 截断方式 | 恢复 |
| --- | --- | --- | --- |
| `run_command` 成功 | status/exit_code/duration | 头 40 行 + 尾 120 行，中间省略并计数 | 完整日志路径 |
| `run_command` 失败 | status/exit_code + **错误摘录** | 先抽 `error/traceback/failed/exception` 命中行，再头尾；上限比成功高 | 完整日志路径 |
| `run_command` 超时/运行中 | status=running + process_id | 现有 `read_new(limit=8000)` 不变 | `process_wait`/`process_read` |
| `read_file` | 模型请求的行区间 | **不在文件中间挖洞**；超硬上限时只返回**连续前缀**并注明"共 N 行，继续用 start_line=…" | 模型自己指定下一段 |
| `search_content`/`glob` | 命中总数 | 显示前 M 条 + "缩小查询"提示 | 重查 |
| 内容型命令（`git diff`/`git show`/`cat`） | 正文 | 放宽上限，或提供 `full_output=true` 逃生口 | 完整日志路径 |

两个关键边界：

1. **下载/安装类命令**：完成信号在 `status/exit_code`，不在日志尾；因此头尾截断是安全的。
   本次会话三份安装日志占累计 prompt 的 71.3%，截到 2K 字符可降 67%，且"Successfully
   installed"仍在尾段。
2. **文件读取不能这么做**：文件正文就是模型要的数据，中间挖洞会让它必须再读一次；而且
   第二次请求会重发历史，成本反而上升。`read_file` 已经有 `start_line/end_line`，应让模型
   自己选区间；工具只在单次响应过大时返回**连续前缀**，不做头尾拼接。

### 5.6 已有先例：skill 就是"静态索引 + 按需取正文"

这一点与当前设计契合度很高——**skill 通道已经是按需加载的落地形态**：

- 构图期 `skill_tools_for()` 把技能目录（名字 + 一句话 + 是否带工具）写进 `get_skill` 的
  docstring，静态上下文里只有轻量索引；
- 模型调用 `get_skill` 后，state 只记名字，正文由 `LLMNode` 下一轮从盘上读取并注入；
- 能力型技能的 `server.py` 工具也是加载后才 `register_server`，`drop_skill` 反向卸载；
- 正文注入受 `SKILL_BODY_BUDGET = 8000` 字符约束。

这与 Cursor 的 skills/MCP 描述按需检索、Anthropic 的 just-in-time retrieval、Aider 的
repo map 是同一模式：**上下文里只放索引，正文/能力按需取**。

差别在于：skill 是**指令/能力**的按需加载，工具输出是**数据**的按需取回。前者加载后持续
注入（直到 `drop_skill`），后者应只取需要的行区间。两者共用"索引 + 取回"的模式，但不需要
共用代码：工具输出落盘后，回执里的 `full_output: <path>` 就是索引，
`read_file(start_line/end_line)` / `search_content` 就是取回路径，**不需要新增工具**。

### 5.7 缓存经济性：glm-5.3-flash 的实际价格与压缩的盈亏线

智谱 glm-5.3-flash 的价格（用户提供；官方称支持"上下文缓存"）：

| 项目 | 价格 |
| --- | ---: |
| 输入（未命中缓存） | 0.8 元 / M token |
| 缓存命中输入 | 0.23 元 / M token |
| 输出 | 2.8 元 / M token |
| 缓存存储 | 限时免费 |

- 缓存命中价是未命中的 **28.75%**（省 71.25%，约 3.5 倍差距）。比 Manus 举例的 10 倍小，
  但仍是单价上最大的杠杆；DeepSeek 的长对话 KV-cache 命中率可达 99%，说明稳定前缀的
  收益可以非常大。
- **本会话 0.47 元的反推**：若输出 token 不大、混合中英按 2.5 字符/token 估，
  `0.47 / 0.8 ≈ 58.75 万` 输入 token，恰好等于 `146.9 万字符 / 2.5`——**与"全部未命中
  缓存"的量级吻合**。若有大量思考/输出 token，这个推断要打折扣，但强烈提示命中率很低。
- **为什么可能没命中**：`LLMNode` 把 `current_plan` 注入在 messages **之前**，而本会话
  `update_plan_step` 改了 12 次；每次改动都会让从该位置起的整个前缀失效。`profile` 是
  memo 的、`memory` 本次为空，所以 plan 是主要嫌疑。
- **缓存友好的排列**：稳定内容在前（`SYSTEM_PROMPT` + `workspace_context` + `profile`），
  messages 居中，**动态 plan / 记忆提醒放尾部**。这与 Manus 的 recitation（把 todo 推到
  近期注意力）方向一致，但需要实测模型对尾部提醒的遵循度。落地前先用 usage 里的
  `cached_tokens` / 命中字段验证，不要凭猜。

**压缩的盈亏线**：设缓存命中价 `Pc=0.23`、未命中 `Pu=0.8`、压缩后保留比例 `r`、压缩后
还有 `K` 次调用。不压缩成本 `K·Pc·N`；压缩后 `Pu·rN + (K-1)·Pc·rN`。划算需：

```text
r < K·Pc / (Pu + (K-1)·Pc)
```

| 后续调用数 K | 需要砍掉的比例 |
| ---: | ---: |
| 1 | > 71% |
| 2 | > 55% |
| 5 | > 33% |
| 10 | > 20% |
| 20 | > 11% |

据此：

- **写入侧压缩**（结果进入 history 之前就截断/遮蔽/落盘）没有缓存惩罚，纯收益；
- **轮内遮蔽旧块**会破坏前缀，必须**批量、低频**，且满足上面的盈亏线；
- **摘要**是一次性破坏缓存，摊销到后续调用；40%–50% 触发时通常还有很多调用，划算；
- 若 provider 支持服务端 `cache_edits`（Anthropic 那类），可以删缓存内容而不破坏前缀；
  智谱的"上下文缓存"是否支持需确认。

### 5.8 错误识别：机械错误易标，语义错误只能保守处理

保留错误是对的，但"识别错误"要分层：

| 层级 | 信号来源 | 自动识别 | 处理 |
| --- | --- | --- | --- |
| 机械错误 | `exit_code != 0`、`ToolResult.success=False`、`error_type`、异常、审批拒绝 | 可靠 | 自动打标，写入 failure ledger |
| 验证错误 | pytest / lint / typecheck / build 的退出码 | 结果可靠，归因需启发式 | 标记验证失败，关联最近编辑 |
| 用户纠正 | 用户说"不对/失败/报错" | 半自动 | 保留原文，把上一轮动作标为 suspect |
| 模型自述 | AI 文本里的"失败/无法/没成功" | 不可靠 | 只作提示，不作事实 |
| 语义错误（计划错、判断错、结果无关） | 无机械信号 | 不能 | 默认"未验证/未决"，不标成功 |
| 未知 | 无信号 | 不能 | 保留摘要/原文，别猜 |

- 建议在 state 里维护一个很小的 **failure ledger**：工具名、`tool_call_id`、错误类型/
  退出码、证据、关联调用、是否已解决。它体积很小，压缩与摘要**永远不能丢**。
- 终端的错误可以直接从 `_render_finished` 的 `exit_code` 与 `ToolResult.error_type` 拿到，
  这是最可靠的一层；`nvidia-smi`、`search_content`、`ffmpeg.exe` 这三次失败都属于此类。
- 更抽象的层没有可靠的自动信号：不要把模型自述当事实。能做的只有——多做验证步骤
  （测试/lint/构建）、保留用户纠正、在摘要里保留"未决/未验证"字段。
- 摘要提示词必须写明：**不得把失败/未验证写成已完成**，必须保留 open questions 与
  failure ledger。

### 5.9 第一步（MVP）：终端运行错误识别

**现状：识别已经有一半，缺的是"压缩时不丢"。**

| 终端入口 | 失败信号 | 现状 |
| --- | --- | --- |
| `run_command` | `_render_finished` 返回 `success=(code==0)`；非零 → `ToolResult.success=False` | `ToolNode` 已打戳 `error_type="tool_error"` |
| `start_process` | 启动观察期内退出 → `success=(code==0)` | 同上 |
| `process_wait` / `process_read` | 收取到已结束进程 → `_render_finished`；非零 → `success=False` | 同上 |
| `guard` 拦截 | `invalid_argument` / `workspace_violation` / `internal_error` | 已带语义 `error_type` |
| 超时 / 仍在运行 | `_render_running` 返回 `success=True` | **不是失败**，是未完成 |
| 审批拒绝 | `ReviewNode` 写 `decision=denied` 戳 | 已识别（另一类） |

**最小改动（两处）**：

1. **失败戳更明确**：`ToolNode` 对终端工具（`run_command` / `start_process` /
   `process_wait` / `process_read`）的非零退出，把泛化的 `tool_error` 细化为
   `command_failed`；如需要，可把 `exit_code` 一并放进 `additional_kwargs`（扩展
   `_tool_stamp`，`_tm_status` 仍只需读 `error_type`）。
2. **压缩不丢失败块**：`CompactNode._plan` 在折叠一个完整工具块前先检查：
   若块内任一 `ToolMessage` 的 `_tm_status()` 为 `failed`/`denied`，**跳过该块**
   （保持 AIMessage + ToolMessages 原样）。失败是稀有事件，保留原文的成本远小于
   让模型重踩一次；后续再考虑"完整错误归档进 notes + 摘要行带 ref"的省空间版本。

**验收**：

- 非零退出、启动即崩、`process_wait` 非零 → 对应 ToolMessage 带 `failed` 戳；
- 超时/`running` → **不带** `failed` 戳；
- `compact_node` 之后，失败块的原文仍在（或 notes 里有可恢复引用）；
- 模型可见的 content 不变（只加结构化戳，不改正文）；
- 单测覆盖 `_tm_status` / `_plan` / `ToolNode` 打戳。

**明确不做（下一步再做）**：语义错误识别（测试失败归因到哪次编辑、用户纠正、模型
自述）、failure ledger、跨工具的错误关联。

### 5.10 轮内压缩的触发与节点放置（草案）

**术语**：轮末 = 模型给出最终答复之后；轮内/拍间 = 同一用户回合内、两个工具批次之间。
"取消轮末、只留轮内"不建议——两者折叠的区域不同，见下。

**节点放置**：

```text
llm_node → queue_node → review_node → tool_node ──(needs_compress?)──→ compress_node → llm_node
                                          │                    └────→ llm_node
                                          └→ orchestrate_node ─┬→ tool_node
                                                              └─(needs_compress?)─→ compress_node → llm_node

llm_node（无 tool_calls）→ compact_node（轮末兜底）→ END
```

- `compress_node` 放在 **工具执行之后、下一次 LLM 调用之前**。这是唯一"新的 ToolMessage
  已经写进 state，但还没有为下一次调用付 prompt 钱"的时点。
- 用**条件边**而不是每次必经的节点：不触发时直接回 `llm_node`，不产生额外 checkpoint
  （checkpoint 会复制整份 `channel_values`，见 §5.5 的落盘放大）。
- 不要放在 `queue_node` / `review_node`：那里工具还没执行，折不到本轮新结果；也不要放进
  任何 `interrupt` 节点（resume 会重跑节点，压缩副作用不可回滚）。
- **轮末 `compact_node` 保留**，作为跨回合兜底：
  - 轮内节点折叠"当前轮内、早于最近 K 块的完整工具块"；
  - 轮末节点折叠"最后一条 HumanMessage 之前"的旧轮次（现状行为）；
  - 轮内已经压到阈值下时，轮末 `needs_compact` 为假，直接 END，成本只有一次扫描；
  - 轮末还是归档 `notes` 的唯一位置（现有行为）。
  两块共用 `_plan` 的"完整块/失败戳/原位替换"逻辑，但**区域规则不同**

**触发**：

| 层 | 条件 | 动作 |
| --- | --- | --- |
| 压缩（确定性遮蔽） | 当前上下文越过 `mask_threshold`（建议先按有效窗口 **30%** 量级），且存在早于最近 K 块的完整工具块，且自上次压缩以来增长 > `delta`（建议窗口 5%–10%） | 把旧块替换为占位摘要 |
| 摘要（LLM） | **厂商标称窗口 40%–50%**，且仍有较多工具轮次 | 结构化摘要 + 原位替换，重置计数 |

- 每拍只做一次 O(messages) 的规模计算；**不每拍都改历史**。用 `delta`/hysteresis 批量
  触发，避免每轮都破坏 prompt cache（盈亏线见 §5.7）。
- 没有可折块（刚开始、在途块、失败块）→ 返回 `{}`，不产生任何 state 写。
- 永不折：在途块、原始 HumanMessage、`current_plan`、失败/拒绝块（MVP）。
- 参数起点：`K = 4–6` 个完整工具块；`delta = 窗口 5%–10%`；`mask_threshold ≈ 30%`；
  `summary_threshold = 40%–50% of advertised window`。全部按模型实测调整。
- 外部实现校准（详见 §5.12）：触发点与 LangChain 的 `before_model` / OpenHands 的
  "每步 condense" 等价；保留最近 N 条工具结果是通用做法；Claude Code 只在**缓存已冷**时
  做时间型 microcompact；OpenHands 要求最小缩减比以防摘要循环；JetBrains 提醒遮蔽对
  thinking 模型可能降约 10% 解决率，需实测。

**为什么保留轮末而不是取消**：

1. 轮内节点只折当前轮；跨回合的旧历史（最后一条 HumanMessage 之前）仍需要轮末节点。
2. 轮末是最终答复之后唯一还能归档 `notes` 的位置。
3. 轮内已经压到位时，轮末只是一个廉价空操作；取消它反而失去"跨回合收口"的唯一落点。

**缓存友好顺序（与你的排序一致）**：

```text
SYSTEM_PROMPT → 工具定义（保持 toolset 稳定）
→ 工作区上下文 → profile（慢变）
→ 对话历史（append-only）
→ 动态尾部：current_plan / 记忆提醒 / 技能提醒
→ 当前输入
```

- 实现上要注意：**不要把动态 SystemMessage 插在 `AIMessage(tool_calls)` 与它的
  `ToolMessage` 之间**（破坏配对）。安全插入点是"最后一条 HumanMessage 之前"或"全部消息
  之后"；两者都只让尾部失效，保留前面整段前缀缓存。
- Anthropic 用显式 `cache_control` 在稳定前缀末尾打断点；OpenAI/Gemini 的自动前缀缓存
  奖励同样的顺序。
- 技能加载/卸载会改 toolset，进而使工具定义前缀失效；这是低频事件，接受即可。

### 5.11 缓存友好排序的落地草案

目标顺序（越靠后越易变）：

```text
[稳定前缀] SYSTEM_PROMPT → 工具定义（toolset 稳定）
          → 工作区上下文 → profile → 交接指针
[历史]     对话消息（append-only）
[动态尾部] current_plan → （可选）记忆提醒 / 技能提醒
[当前输入] 最后
```

现状与目标的三处差异：

| 现状（`LLMNode.__call__`） | 目标 | 原因 |
| --- | --- | --- |
| `current_plan` 在 messages **之前** | 移到**动态尾部** | 本会话改了 12 次；每次改动让整段 messages 缓存失效 |
| `profile` 在 plan 之后 | 移到**稳定前缀**（plan 之前） | profile 由 memo 提供，慢变 |
| `memory` / `skills` 在 messages 之前 | 低频变化，可留在稳定前缀；若更新频繁再移尾部 | 记忆只在 `write_memory` 时变；技能只在 load/drop 时变 |

实现要点：

1. **拆分构建**：`stable_system`（静态提示 + 工作区 + profile + 交接指针）、
   `history`（`state["messages"]`）、`dynamic_tail`（plan、可选记忆/技能提醒）分开拼。
2. **安全插入点**：动态 SystemMessage **不能**插在 `AIMessage(tool_calls)` 与兑现它的
   `ToolMessage` 之间（会破坏配对）。两个安全点：
   - **全部消息之后**（推荐给 plan）：轮内最新计划可见、配对安全、缓存爆炸半径最小；
     代价是"当前输入"不再字面最后。
   - **最后一条 HumanMessage 之前**：保持当前输入最后；代价是轮内 plan 更新滞后一拍。
3. **工具定义稳定**：`bind_tools` 只在 toolset 版本变化时重绑（技能 load/drop）。保持这个
   行为；技能加载是低频事件，接受一次前缀失效。
4. **显式 cache_control**（若 provider 支持）：在稳定前缀末尾、以及历史末尾各打一个断点；
   Anthropic 需要显式标；OpenAI/Gemini/智谱的自动前缀缓存奖励同样的顺序。`langchain-openai`
   若不暴露该字段，可通过 `extra_body` 传（先确认智谱是否支持）。
5. **观测**：在 `LLMNode` 记录每次调用的 `prompt_tokens` / `cached_tokens` / 命中率
   （provider 字段名不同：`prompt_tokens_details.cached_tokens`、`prompt_cache_hit_tokens`
   等）。没有这个数，排序调整无法验证。
6. **回归**：改顺序后跑一次 `--live` 评估 + 本会话重放，确认模型仍遵循计划/记忆提示；
   尾部提醒可能比头部弱，需要实测。

### 5.12 其他 agent 的轮内压缩实现对照（用于校准触发点）

| 系统 | 调用位置 | 触发 | 保留 | 机制 |
| --- | --- | --- | --- | --- |
| **OpenHands** | **每个 agent step 前** `condense()` | 事件数 > `max_size`（默认 120；社区在改 token-aware） | `keep_first` + 最近尾部 | LLM 摘要；`Condenser` 可插拔 |
| **SWE-agent**（JetBrains 实验） | 构造 prompt 时 | **固定窗口 M 轮**（非自适应） | 最近 M 轮观测 | 观察遮蔽，保留推理/动作 |
| **Claude Code microcompact** | API 调用前后 | ① 计数：保留最近 N 条工具结果；② **时间：仅当缓存已冷**（空闲 > 60 min）；③ API 原生：约 180K 触发、保留 40K、`clear_at_least` 140K | 最近 5 条工具结果 | 占位符 + 服务端 `cache_edits` |
| **LangChain / Deep Agents** | `before_model` / `wrap_model_call`（**每次模型调用前**） | token 比例 / 绝对 token / 消息数；溢出异常兜底 | 最近后缀 | `RemoveMessage` + 摘要；大工具结果落 `large_tool_results/{id}` |
| **Codex CLI** | 约 90% 窗口 | token 阈值 | 最近 20K 用户消息（第三方逆向） | LLM/远程 compaction |
| **Gemini CLI** | 每轮后 / 溢出 | 50%–70% | 最近 30% | 工具输出截断 + LLM 摘要 |
| **Theta（建议）** | `tool/orchestrate → 条件边 → llm` | 遮蔽：30% 量级 + delta；摘要：40%–50% 标称窗口 | 最近 K 块 + 失败块 | 先确定性遮蔽，摘要兜底 |

可借鉴的六条：

1. **触发点几乎都在"下一次模型调用前"**（OpenHands 每步、LangChain before_model、Claude
   Code API 调用周边）。`tool → 条件边 → llm` 与它们等价；不需要在工具执行中途插节点。
2. **保留最近 N 条工具结果是通用做法**：Claude Code 保留 5 条、SWE-agent 保留 M 轮、
   OpenHands 保留 keep_first + 尾部。Theta 的 K=4–6 与之一致。
3. **缓存感知触发**：Claude Code 的时间型 microcompact **只在缓存已冷时**运行（空闲 > 60
   分钟）。Theta 若没有服务端 `cache_edits`，也可以在"前缀本来就要变"时（长时间空闲、
   plan/profile 刚变过）再做遮蔽，避免专门为压缩破坏缓存。
4. **防循环**：OpenHands Issue #2034 出现过"摘要后立刻再次触发"的死循环；修复方向是
   要求最小缩减比例（如 ≥10%）。Theta 的 compress 节点也应设"缩减不足就不提交"的护栏。
5. **思考模型的遮蔽风险**：JetBrains 的实验中，遮蔽对使用 extended thinking 的模型可能
   降低约 10% 解决率。`CHAT_THINKING` 开启时需要实测；必要时保留更多块或改用摘要。
6. **大工具结果落盘是共识**：Deep Agents 落 `large_tool_results/{tool_call_id}`，
   Cursor/Gemini CLI 落文件——与 §5.5 的 `full_output` 方案一致。

## 6. 来源

- Anthropic, *Effective context engineering for AI agents*（2025-09）：
  https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Anthropic, *Context editing*（API 文档）：
  https://platform.claude.com/docs/en/build-with-claude/context-editing
- Claude Cookbook, *Context engineering: memory, compaction, and tool clearing*：
  https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools
- Manus, Yichao "Peak" Ji, *Context Engineering for AI Agents: Lessons from Building Manus*
  （2025-07）：https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
- Lindenbauer et al., *The Complexity Trap: Simple Observation Masking Is as Efficient as
  LLM Summarization for Agent Context Management*（arXiv:2508.21433，NeurIPS 2025 DL4Code）：
  https://arxiv.org/abs/2508.21433
- Cursor, *Dynamic context discovery*：https://cursor.com/blog/dynamic-context-discovery
- OpenHands, *Context Condenser*：
  https://docs.openhands.dev/sdk/guides/context-condenser
- OpenHands, *Context Condensation for More Efficient AI Agents*：
  https://openhands.dev/blog/openhands-context-condensensation-for-more-efficient-ai-agents
- Roo Code, *Intelligent Context Condensing*：
  https://roocodeinc.github.io/Roo-Code/features/intelligent-context-condensing/
- Gemini CLI, *Chat Compression and Context Management*（DeepWiki 逆向）：
  https://deepwiki.com/google-gemini/gemini-cli/4.12-system-prompt-generation
- OpenAI Codex CLI 逆向规格（第三方）：
  https://github.com/AlexKenbo/codex-harness-internals
- Aider, *Repository map*：https://aider.chat/docs/repomap.html
- LangChain Deep Agents, *SummarizationMiddleware*：
  https://github.com/langchain-ai/deepagents
- Kang et al., *ACON: Optimizing Context Compression for Long-horizon LLM Agents*
  （arXiv:2510.00615，Microsoft）：https://arxiv.org/abs/2510.00615
- Chroma, *Context Rot*（2025）：https://research.trychroma.com/context-rot
- Claude Code 上下文管理（第三方逆向，仅作参考）：
  https://github.com/openedclaude/claude-reviews-claude/blob/main/architecture/11-compact-system.md
- Claude Code microcompact 缓存/触发讨论（官方 issue，含第三方源码指路）：
  https://github.com/anthropics/claude-code/issues/42542
  https://github.com/anthropics/claude-code/issues/98204
- LangChain middleware（`SummarizationMiddleware` / `before_model`）：
  https://reference.langchain.com/python/langchain/middleware
- LangChain Deep Agents summarization（`wrap_model_call` / 溢出兜底 / 大工具结果落盘）：
  https://reference.langchain.com/python/deepagents/middleware/summarization/create_summarization_middleware
- OpenHands Condenser 循环 bug 与最小缩减比（Issue #2034）：
  https://github.com/OpenHands/software-agent-sdk/issues/2034
- OpenHands token-aware condenser 讨论（PR #912）：
  https://github.com/OpenHands/software-agent-sdk/pull/912

> 标注"非官方/逆向"的实现细节会随版本变化，引用前请以对应版本的官方文档或源码为准。

## 7. 决策记录（2026-10-18）

**已拍板**：

1. **压缩与摘要分两级**：压缩（截断/遮蔽/落盘）在写入侧与轮内持续做；摘要在**厂商标称
   窗口的 40%–50%** 触发。到这个量级历史已经被压缩多轮，再压缩收益很小。
2. **保留错误，先做终端 MVP**：`ToolResult.success=False` 已有结构化戳；补
   `command_failed` 细化，并让 `CompactNode._plan` 跳过含失败/拒绝块的折叠。语义错误
   识别（验证/用户纠正/模型自述）后续再做。
3. **缓存友好排序**：稳定前缀（SYSTEM_PROMPT → 工具定义 → 工作区 → profile → 交接指针）
   → 历史 → 动态尾部（`current_plan`，可选记忆/技能）。动态 SystemMessage **不能**插在
   `AIMessage(tool_calls)` 与 `ToolMessage` 之间。先在 `LLMNode` 记录 `cached_tokens`，
   再验证排序收益。
4. **轮内压缩位置**：`tool_node` / `orchestrate_node` → **条件边** → `compress_node` →
   `llm_node`；轮末 `compact_node` 保留为跨回合兜底。保留最近 `K=4–6` 块；失败/在途/
   原始 Human/`current_plan` 不折；一次压缩至少缩减 10%，防摘要循环。
5. **外部校准**：Claude Code 在缓存已冷时再做时间型 microcompact；OpenHands 要求最小
   缩减比；SWE-agent/JetBrains 只遮蔽观测、thinking 模型需实测；LangChain 用
   `before_model`；大工具结果落盘是共识。

**待定**：

- glm-5.3-flash 的**厂商标称窗口 token 数**（用于 40%–50% 摘要触发）。
- 智谱是否支持显式 `cache_control` / 服务端 `cache_edits`；usage 里缓存字段的名称。
- `K`、`mask_threshold`、`delta` 的实测值。
- `CHAT_THINKING` 开启时遮蔽强度的实测结论。

**下一步**：终端错误识别 MVP → `compress_node` 条件边 + 移动窗口骨架（先只做确定性
遮蔽）→ usage/缓存命中率观测 → 再决定摘要实现与缓存排序调整。
