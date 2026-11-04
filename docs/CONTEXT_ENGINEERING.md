# Theta $\theta$ 上下文工程设计

> 本文档记录 $\theta$ 的上下文管理（消息留存 / 折叠 / 笔记区）设计讨论与结论，作为实现参考。
> 核心主题：**工具返回是"高时效、低重取成本"的信息，不该无限占住模型窗口；跨轮只留"不可重取的结论"，不流"可重取的 dump"。**
>
> 状态标注：`[已落地]` = 已完成；`[前置]` = 本设计依赖、已完成；未标注 = 目标态（设计已收敛，尚未实现）。
> 相关：[[docs/EXCEPTION_DESIGN]]（异常语义过滤器，决定什么进模型）。

---

## 1. 背景与问题

- `AgentState.messages` 用 `add_messages` 累加（`app/agent/state.py`）；**已有轮末折叠**（`app/agent/nodes.py::CompactNode`，用 `RemoveMessage` 原位替换，见 §8），但没有**行内滚动折叠**（单轮超长编排内部仍会无限涨）。
- 跨轮靠 checkpoint 持久化（现为 `AsyncSqliteSaver`，装配在 `app/platform/runtime.py`），所以每轮喂给模型的都是"当前历史 + 每轮重拼的系统消息"。
- Token 大头几乎全在 `ToolMessage`：整文件读取、目录树、检索命中、命令输出、网页正文。而这些内容大多**时效极强**——文件一旦被改，几轮前的整文件 dump 就是纯噪声。

## 2. 现状：messages 里到底有什么

进 `state["messages"]` 的只有四类（系统消息**不落库**，每轮由 `LLMNode` 现拼，见 `app/agent/nodes.py::LLMNode`）：

| # | 消息 | 来源 | 特征 / 体积 |
|---|---|---|---|
| 1 | `HumanMessage` | 前端（`app/tui`）起 turn、`evaluation/runner.py` | 小；任务源头，不可重取 |
| 2 | `AIMessage`(带 tool_calls) | `LLMNode` 返回 | 正文多为填充语；tool_calls 块负责**协议配对** |
| 3 | `AIMessage`(不带 tool_calls) | 同上 | 任务的成品答复 |
| 4 | `ToolMessage` | `ToolNode` / `OrchestrateNode` 回执 | **体积大头** |

两个结构性事实（折叠必须利用）：

- **计划不靠历史回显**：`current_plan` 独立存 state，`LLMNode` 每轮把当前计划重注入为 `# 当前任务`（见 `LLMNode.__call__` 的系统消息拼装处——**不复述行号**，它会漂）。于是 `create_plan`/`update_plan_step` 每次整份回显的 ToolMessage 快照对模型**双重冗余**。
- **系统消息不进历史**：`SYSTEM_PROMPT` + 工作区上下文 + 计划回显都是临时拼装。折叠只作用于消息历史，与它们无关。

## 3. 不变量（折叠的安全边界，违反了模型调用就会炸）

1. **块不可拆**：`AIMessage(tool_calls=[c1..ck])` + 紧随其后、兑现各 id 的 `ToolMessage` 是一个不可分割的连续"块"。不能只删其中任一消息——会留下孤儿 ToolMessage（OpenAI 兼容 API 直接拒绝）。
2. **只折叠"已消费"的块**：一个块只有在**其后又出现了一条 AIMessage**（模型已产出反应它的下一步）之后，才算被消费过。正在等模型反应的块（最新那批工具结果）绝不能折叠。
3. **被拒调用先兑现**：审批被拒的 tool_call 会收到一条带 `[approval_denied]` 前缀的 `ToolMessage`（`[已落地]` `ReviewNode`），把历史里悬空的 tool_call 块闭合。没有这条前置，折叠会把"未兑现的 assistant tool_call"与下一条 Human 拼到一起，正是本设计要根除的形态。
4. **中断续跑不经过 START**：`Command(resume=...)` 从被中断节点继续，不重跑 START。因此挂在 START 之后的节点**只在"新一轮用户消息"时触发一次**——天然只在上一轮已 END、checkpoint 已落定时做折叠，不会在任务中途偷走模型正要用的信息。

## 4. 价值模型：两轴判定，先分轴再下结论

| 轴 | 问题 | 决定 |
|---|---|---|
| **可重取性** | 能否从工作区/git **低成本重新拿到权威值**？ | 能 → 原始 dump 不留，丢/折代价≈0 |
| **证据性** | 是不是磁盘上重取不到的**事实**（如"我改过 X 且 pytest 通过"）？ | 是 → 必须落进独立通道（work_log/notes），**而不是**靠原始 ToolMessage 兜底 |

推论：模型历史里大多数 ToolMessage 内容同时满足"可重取 + 低证据" → 折叠后唯一的损失是"当初看到过"的出处；而出处要么由文件/git 真值承担，要么该压成一行结论进日志。**换工作区/文件是逃生阀，不是风险。**

## 5. 工具处置分类（结论表）

判定时机统一为"该块已被后续 AIMessage 消费后"。行内指"同一次图编排内进一步迭代"，跨轮指"下一条用户消息开始前"。

| 工具类 | 行内 | 跨轮留全量？ | 理由 |
|---|---|---|---|
| `read_file` / `list_dir` / `get_directory_tree` / `search_content` / `glob` | 该文件此后被 edit/write 改过 → 旧读全废；可折 | 否 | 磁盘即真值，重取便宜，一次性 scaffold |
| git 只读（`git status/diff/log/branch/fetch`，经 run_command） | 消费后可折 | 否 | repo 即真值 |
| 写类回执 `edit/create/delete/commit/copy` | 记 work_log，回执内容小可不折 | 否 | "做了 X" 是事实，进 log |
| `run_command` | **调错迭代期内留错误文本**；被后续成功替换后压成 `exit=N` 一行 | 否（verdict 进 log） | 输出可重跑重取但**有成本/副作用**；log 存证据 |
| `process_read` / `process_wait` 增量 | 只在该进程仍被等待时有用 | 否 | 输出流**不可重取**但极瞬态，关键行进 log |
| `web_search/extract_urls/crawl_website/deep_research` | 合成进答复后即弃原始 | 否（正文进 **notes**，§7） | 不可免费重取 → 原文归档，**结论进答复/被 promote**，不靠历史兜底 |
| 计划回执（`create_plan/update_plan_step` 快照） | 永远冗余 | 否 | 被 live `current_plan` 取代 |

> 一句话：**跨轮"几乎什么都不留全量"，只留 work_log + 最终答复；行内只折"已消费 + 易陈旧 + 可重取"的旧块。** 三段兜底见 §8：可重取 → 重取；小事实 → work_log；大而不可重取 → notes。

## 6. `work_log`：结构化"意图 + 事实"双半行拼接

不做自由文本，每条是结构化条目，state 里存结构（机器可断言/剪裁/去重），注入模型前渲染成文本行。

```python
# AgentState.work_log: list[WorkLogEntry]（随 checkpoint 持久化）
{
  "id": "w7",
  "reason": "修正 src/x.py 校验，给缺省项补默认值",       # ← 意图半行：模型 content
  "op":     {"tool": "edit_file", "target": "src/x.py"}, #   （action 前自述）
  "status": "ok" | "failed" | "denied",                 # ← 事实半行：系统补
  "verdict": "exit=0 · 12 passed",                      # ← 事实半行：系统补，一行
  "note_ref": "notes#r3",                               # ← 大结果指针（§7），无则 null
}
```

写入规则：

- **意图半行（reason）只信模型，但 best-effort**：取该 tool_call 所在 `AIMessage` 的 `content`。prompt 要求带工具调用时 content 写**一句话**人话工作日志（在做什么、为什么），并明确"不是给用户的最终答复"。模型可能给空/敷衍 → 空则回退机器模板"工具名 + 主参数"。设计为 **upgrade 而非 dependency**（强制自述有每轮 token 成本，需实测，见 §11）。
- **事实半行（status/verdict）只信工具结果，绝不来自 content**：执行后由系统填，来源 = `ToolResult.success/error_type`、`guard`、`run_command exit code`。防止模型"宣称成功"式自欺。
- **denied 也要记一条**：审批被拒 → 追加 `status="denied"` 极短条目（无 verdict/note_ref，reason 可留空）。理由："人被否决过 X"是不可重取、跨轮有意义的决策事实——折叠会清掉 `[approval_denied]` ToolMessage，不记则模型下轮可能原样重提同一命令再被拒。
- **容量**：条数上限，溢出丢最旧。

渲染给模型（`LLMNode` 置顶注入 `# 会话工作日志`）：

```
[w7] 修正 src/x.py 校验，给缺省项补默认值 · edit_file → ok（12 passed）
[w8] · delete_dir foo/ → denied（未执行）
```

## 7. `notes` 笔记区：大结果的会话级落点，跨会话只走 promote

### 7.1 它解决什么

`web_search/deep_research/crawl/extract` 之类的结果**花钱、不可免费重取**，直接折叠=真丢，留在历史=每轮膨胀。笔记区给这类产出一个"指针常驻、正文按需取回"的会话级落点。

### 7.2 会话边界（产品问题得出的切法）

"跨会话有没有用"拆两层，答案决定介质：

| 情形 | 记忆机制 | notes 需要跨会话吗 |
|---|---|---|
| **同 thread**，进程重启接着聊 | checkpoint（AsyncSqliteSaver）自动持久化 notes | 否——state 白拿 |
| **新 thread / 新会话**，同一个 repo | repo 本身（含被 promote 的结论/文档/代码） | 否——靠正常读 repo |
| 不同 repo / 无上下文 | 无 | 无意义 |

结论：**notes 管"这一趟"，repo 管"下一趟"。** 值钱的结论由 agent 主动 **promote 进 repo**（见 7.5），剩下的研究原料随会话散掉。**不做跨会话笔记库 / 不引关系型数据库**——其能力（跨会话查询、收敛）在否掉"跨会话库"后没了用处，且与 RAG/第二真值同理：游离于文件+git 之外的副本不可 diff、用户无感、随项目演进必然陈旧。**值钱的知识应成为文件（git 跟踪、diff 可见、审批可见），不是系统静默囤的库。**

### 7.3 形态与字段

```python
# AgentState.notes: dict[str, NoteEntry]（键 ref 稳定；会话级，随 checkpoint 存亡）
{
  "ref": "notes#r3",
  "topic": "market",            # ← 主题：归档时留空，懒分类（见 7.4）
  "tags": ["光伏", "2026"],      # ← 可多标签
  "kind": "research",           # research | web | crawl | extract | command_output …
  "title": "光伏市场调研",
  "source_tool": "deep_research",
  "created": 1699...,
  "size": 2100,
  "content": "# 光伏市场调研\n\n## 2026 市场…",   # markdown payload
}
```

- **envelope 结构化**（ref/kind/topic/…）供系统剪裁、过期、过滤，不用解析正文；**payload 用 markdown**（消费者是模型，Tavily/crawl 本就返回 md；json/xml 只加大段文本转义噪声，无查询收益）。
- 归档规则：系统按 **kind + 体积阈值**把大结果自动归档，verdict 只写 `ok · 详情见 notes#r3`；模型也可显式要求归档。小结果不进 notes。

### 7.4 主题分类 = 元数据能力，做懒分类

- 归档时只给粗分（`kind` 默认），`topic/tags` 留空——**不每次归档都付模型钱**。
- 需要整理时模型主动调 `update_note_tags(ref, topic, tags)` / `notes_by_topic(topic)` / `list_topics()`。
- 用途：**会话内**按主题召回这次研究产出 + **判断哪些主题值得 promote**。不是跨会话语义库（那会踩回被搁置的 RAG）。

### 7.5 工具集与 promote

- **工具**（agent 侧 `InjectedState`，仿 orchestrate 模式）：`read_note(ref)` `[已落地]`；`search_notes(q)` / `notes_by_topic(topic)` / `list_topics()` / `update_note_tags(...)` / `drop_note(ref)` **均未实现**。全文经 `read_note` 取回的是**短命 ToolMessage，随折叠走**。
- **索引注入尚不存在**：设计的"每轮注入一行 `[notes#r3] …（research · 2.1k）`"**没有实现**——`LLMNode` 只注入 系统提示词 / 工作区 / 计划 / 项目画像 / 长期记忆 五条；模型目前**只能从折叠摘要行尾的"详情见 notes#rN"**感知笔记存在。（`app/agent/tools.py` 里 `read_note` 的 docstring 声称"notes 本身只注入一行索引"，与实现不符，待随本节一并校准。）
- **tool.json 登记**：现状只登记 `read_note`（免审）。（设计的 `drop_note` 需审等，随工具未实现而不存在。）
- **promote 不是新机制，是收尾动作**：研究告一段落、结论值得留档时，agent 用现有 `create_file`/`edit_file` 把提炼结论写进 repo（如 `docs/决策记录-<topic>.md` 或代码注释），走正常审批；notes 正文/URL 作引用来源。prompt 把这条列为研究类工具后的收尾习惯。

## 8. `compact_node`：滚动块折叠

### 8.1 触发位置

第一版只做**轮边界折叠**。设计原写"挂在 START 之后"，**实现改挂在 END 之前**（`llm_node` 判无 `tool_calls` → `compact_node` → `END`）：

```
START ─► llm_node ──(无 tool_calls)──► compact_node ─► END
             └──────(有 tool_calls)────► …
```

两者时机等效（都在"上一轮已收尾、下一条用户消息之前"）。2026-10-20 起轮末压缩
**无条件启用**：没有字符预算门，没有可折块时节点自己返回 `{}`。`[已落地]`

轮内滚动折叠已落地为 `CompressNode`（`tool_node` / `orchestrate_node` → `compress_node`
→ `llm_node`），做 read_file 去重、批级移动窗口与状态回执变薄，详见 §8.6。`[已落地]`

### 8.2 折叠算法

轮末无条件运行；没有可折块时原样直通（零成本、幂等）：

1. 把消息解析成工具块；没有可折块 → 不变。
2. **保留区** = **最终答复**（末条无 `tool_calls` 的 `AIMessage`）及其后的内容；最终答复是结论载体，原样保留。当前轮已闭环的工具块可以折（§8.6 调研结论：在逻辑停止点压缩）。
3. **折叠区** = 最终答复之前的消息。没有最终答复（steering 提前收口等）时退回"最后一条 `HumanMessage` 之前"，当前轮在途结果保持原样。沿块边界剪除（见 §3.1）：只折**完整且已消费**的工具块；失败/被拒、编排回执、worker 结论保留原文；联网正文归档 notes；其余按调用折叠。
4. 产出新 `messages` 写回 state（决定性地；对相同输入产生相同输出，便于无 LLM 单测固化）。多个 plan step 会分成多个 `FoldGroup`，每组一条摘要。

一个前提：**别先污染再清理**——`OrchestrateNode`/`ToolNode` 仍按现状把回执写入历史（这是协议需要的），折叠在轮边界统一处理冗余，不在写入时做特判。

### 8.3 剪裁边界（防误伤）

- 只允许整块剪；绝不留下孤儿 ToolMessage（§3.1）。
- 绝不允许剪掉"仍待模型反应"的最新块（§3.2）——保留区的最小值必须 ≥ 1 个未消费块。
- 一条兜底：折叠后必须仍保留 ≥1 条 `HumanMessage`（当前用户请求）与其后的全部内容。
- 折叠不可见地改变语义。折叠丢掉内容的**三段兜底**：

| 被折叠丢的东西 | 兜底 |
|---|---|
| 文件/目录/检索/git | 重取（真值在磁盘） |
| "做过 X / 验过什么 / 被否决过 X" | `work_log`（结构化小条目） |
| 大体积、一次性、不可免费重取的研究产出 | `notes`（指针常驻，正文 `read_note` 取回） |

> 笔记区让折叠可以更激进：`web_search` 等"不可重取"不再是折叠禁区，原文进 notes 即可。

### 8.4 预算与触发阈值

- 无 tiktoken 类本地分词：DeepSeek 走 OpenAI 兼容，token 数在 agent 侧不可精确算。
  **轮末不再使用字符预算门**（2026-10-20 起无条件）；字符阈值只服务轮内
  `CompressNode` 的高/低水位（`TURN_HIGH_WATERMARK_CHARS` / `TURN_LOW_WATERMARK_CHARS`，
  经构造参数可调）。`needs_compact(state, budget)` 与 `_CONTEXT_BUDGET_DEFAULT`
  仅测试/兼容保留，不在生产路径上。
- **不走 `app/config.py` 的 Settings**：阈值属于可调内部参数，用模块常量 + 构造参数
  即可，避免每次加参都动 `.env`。（工作区自 2026-09-10 起是图的构造期参量，不再是
  env 输入。）

### 8.5 折叠前后示意

一段超长同轮对话的中间（伪码）：

```text
# 折叠前（历史里躺着几轮前的整文件 dump / 目录树 / 命令输出）
Human: 读 README 摸清项目，然后实现 X
AIMessage(tool_calls=[get_directory_tree])
ToolMessage: <数百行目录树>
AIMessage(tool_calls=[read_file src/x.py])
ToolMessage: <x.py 数百行全文>
AIMessage(tool_calls=[edit_file src/x.py])   # ← 模型已跨过上面两块
ToolMessage: ok
AIMessage(tool_calls=[run_command pytest])
ToolMessage: <测试输出 N 行>
AIMessage(no tool_calls): 已实现并验证通过…     # ← 最终答复（保留区尾部）
```

```text
# 折叠后（同一次编排仍要继续 / 或下一轮开始时）
Human: 读 README 摸清项目，然后实现 X
AIMessage(tool_calls=[edit_file src/x.py])     # 最近的块原样保留
ToolMessage: ok
AIMessage(no tool_calls): 已实现并验证通过…
# work_log（另注入）：[w?] 读 README 摸清布局 · read_file → ok
#                        [w?] · run_command pytest → exit=0
# notes（另注入索引）：[notes#r2] 某主题研究（…）——如需细节 read_note 取回
```

### 8.6 批级压缩：周期 / 原子 / 批（2026-11-03 设计稿，Phase 1 部分落地）

**动机**：轮内 `CompressNode` 目前按单条 `ToolMessage` 选择与改写；单条压缩会拆散工具调用组、指针数量多，也无法利用相邻消息的语义关系。把操作单元提升到「批」：先分组，再对组做一次原子操作。

**周期（cycle）**：一次用户输入到模型给出最终答复（图路由到 END）为一个周期：

```text
H, B1(A(tc n1), T…), B2(A(tc n2), T…), …, A_final
```

- `H`、`SystemMessage`、`A_final` 是**边界原子**，不进批；
- `B*` = `AIMessage(tool_calls)` + 兑现它的全部 `ToolMessage`，是**结构原子**，不可拆、不可单独删；
- 轮内压缩作用于**未完成周期**；轮末压缩作用于**已完成周期**。现状（§8.1）：`CompactNode` 折的是最后一条 Human 之前的周期；调研结论建议把刚完成的周期也纳入轮末折叠区（保留 `A_final`），见下。

**两级模型**：`Atom → Batch`，不设“组下面再分批”的第三层；将来若需要语义摘要，叫 `Segment`，只服务 LLM digest，不参与确定性 mask。

**语义优先于预算（2026-11-03 定）**：预算只决定“何时必须收口”，不决定“在哪切”。切点优先取语义边界（plan step、里程碑/子目标完成、成功↔失败转换、工具族或目标变化）；预算到达上限时，在容差内找最近的语义切点，找不到才按预算硬切。`max_batch_chars` 是最后的安全上界，不是主分组维度。

> 外部证据与逐条未决回应见 `docs/extra/批级压缩与分组模式：业界实践对照与下一步策略.md`（2026-11-03）：三项独立工作（自我分割轨迹、CAT、SelfCompact）均显示语义阶段边界优于固定窗口/最近优先。

**分批规则（优先级）**：

1. **硬边界**：`H` / `SystemMessage` / `A_final` / 未消费 block / 不完整 block；
2. **plan step 语义边界**：从 `create_plan` / `update_plan_step` / `clear_plan` 的调用历史重建 active step，key = `(plan_epoch, step_id)`；`step=None` 自成一段；含 `in_progress` 更新的 block 归新 step（调研结论，见下）；
3. **step 内语义子边界（优先切点）**：里程碑/子目标完成、成功↔失败转换、`start_process`/`process_kill`、写操作完成、工具族或目标（路径/URL/命令域）切换——它们是**候选切点**，不是硬边界；
4. **预算（软目标 + 硬上界）**：同一 step 段内按可压 `ToolMessage` 字符累计；接近 `max_batch_chars`（先取 6k–8k）时，先在 ±20% 容差内找最近的语义子边界，找不到才按预算硬切；
5. **软边界（远期/可选）**：关键词 + 词嵌入；只在无 plan 或 step 内没有明确子边界时使用，不能覆盖 plan step；调研显示生产级收益证据不足，降级为实验；
6. **无 plan 回退**：连续窗 + 语义子边界 + 预算，不按工具族硬切；
7. **工具族只做摘要元数据**，可作预算附近的 tie-breaker，不作硬边界（真实会话里 read/run/read/run 交替太频繁）。

**可压 `ToolMessage`（逐条判定，不是整块判定）**：

- `status == ok`；工具不在 web / ask / dispatch / state；已被消费；有稳定 id；正文 ≥ `mask_min_chars`（300）；
- 失败/被拒、受保护、写回执保持原文；
- 一个 block 只要有一条可压 `ToolMessage` 就能进批；混合块/含失败块允许只压其中的成功结果——否则低水位不可达（见下方样本）。

**批内表示（Phase 1：只改 content，不删消息）**：

- 批内第一条被压 `ToolMessage` 放批级时间线指针：
  `…[已压缩·批3] read_file×3 (paths) · run_command×2 (cmd, exit=0) …；失败/受保护结果保留，需要时重读`
- 同批其余被压消息放短戳 `…[同批已压缩]`；
- 不改 `tool_call` / `ToolMessage` 配对。

**Phase 2（2026-11-04）：旧写操作的大 args 也压**——`write_file` / `create_file` 的
`content`、`edit_file` 的 `old_string` / `new_string` 换成带原长度的指针
（`…[参数已省略 原 N 字符；文件为准]`），`path` 等定位信息保留；`tool_call_id` / 调用
关系不变。可清 args 与可压正文一起计入 `compressible_chars`，共享高低水位与批预算。

**保留与触发**：

- 保留最近 K 个 **block**（K=8–10），不是最近 K 条消息 / K 个批；最新未消费 block 永不压；
- 触发/停止用 `compressible_chars`（可压 `ToolMessage` 正文），不要用总 content；另记 `total_chars`（含 args）观测；
- 参数关系：`指针开销 < min_batch_chars < max_batch_chars < low_watermark_chars`；`max_batch_chars` 建议不超过低水位的 1/2–1/3，避免一批过冲；
- `max_batch_chars` 是**最后的安全上界**，不是主分组维度：预算到达时先在 ±20% 容差内找最近的语义切点（里程碑/成功失败转换/目标变化），找不到才硬切；语义边界优先于预算；
- 上限的意义是**单次压缩决策的影响面上界**：防过冲、让批摘要可信、限制误判的影响面、约束摘要成本；它不是缓存优化（缓存靠滞回与批量触发）；
- 没有可压批时返回 `{}`，不每拍重扫。

**样本证据（IndexTTS2，step 758 checkpoint）**：

- 当前轮 57 个 block：content 127,225，tool_call args 33,530，合计 160,755；30 条可压 `ToolMessage`（27 个 block），可压正文 113,343；
- 同族连续 ≥2 只覆盖 36,105；任意族连续 ≥2 覆盖 72,843；逐 block 覆盖 97,934——**工具族不是分批维度**；
- 整块严格（任一失败/受保护就跳过）节省 92,964，终态 content 34,261（高于 25k 低水位）；按 `ToolMessage` 部分压缩节省 109,743，终态 content 17,482、合计 51,012；
- plan step 段：`None` 39,243 / step2 33,923 / step4 31,299 / step5 8,878；step + 8k 预算约 18 批，与连续窗 + 8k 同批数，但不跨 step；
- 无 `max_batch_chars` 时 4 批、单批 31k–39k，会过冲低水位；8k 时可停在目标附近；
- args 仍剩 33.5k（写操作 args 是大头）：content-only 到不了更低。Phase 2 已把写操作的
  大 args 换成指针并计入可压池；整块墓碑（视图化）仍未做。

**与现状的差异 / 落地顺序**：

1. 抽共享的 `_iter_atoms` / `_iter_blocks`（`CompactNode._plan` 已有块解析）；
2. `CompressNode` 从逐条 mask 改为 `_plan_batches`：硬边界 → step → step 内语义子边界 → 预算兜底；
3. 可压判定改为逐 `ToolMessage`，允许混合块/含失败块部分压缩；
4. 批级时间线指针 + 短戳；保留最近 K 个 block；
5. 触发改 `compressible_chars`，补 `min/max_batch_chars`；预算到达时先找语义切点，再硬切；
6. 轮末折叠区扩到刚完成周期（保留 `A_final`），并与 `CompressNode` 统一到 `CycleBatchPlanner`；
7. Phase 2：写操作 args 清理已落地（2026-11-04，`ARG_CLEAR_FIELDS`）；整块墓碑 / 视图化
   仍未做；
8. 远期：关键词/词嵌入软边界（可选实验），边界冻结并缓存 `segmentation_version`。

**调研结论（2026-11-03，详见 extra 专题）**：

- 轮末**压刚完成的周期**（保留 `A_final` 作结论载体）：与 SelfCompact/CAT 的“逻辑停止点压缩”一致；
- Phase 1 维持 content 改写；Phase 2 的写操作 args 清理已落地（照 `clear_tool_inputs`
  先例，只清 content / old_string / new_string）；**整块墓碑**（checkpoint 原文保留、
  mask 是视图）仍未做；
- K=8–10、`max_batch_chars`=6k–8k 有样本与业界参照；`min_batch_chars` 按“指针开销 × 批内条数”动态下界；
- 混合 block 的 `in_progress` 更新归**新 step**；
- 软边界（关键词+嵌入）降级为可选实验：三项独立工作用的是 agent 自声明/结构信号，不是事后嵌入聚类；
- `CompactNode` 与 `CompressNode` **统一**到 `CycleBatchPlanner`（只剩区域与处置策略差异）。

**仍待实测**：

- K、`max_batch_chars`、`min_batch_chars` 的具体值（P2 用 `llm_usage` 的 cache/token 字段校准）；
- 语义子边界的精确集合（哪些信号作为预算附近的候选切点）；
- 软边界若启用，权重、阈值、迟滞参数与 `segmentation_version`。

**落地状态（2026-11-03）**：

- `[x]` `CompressNode` 批级分组：`_iter_tool_blocks` / `_mark_eligibility` /
  `_plan_batches` / `_semantic_cut` / `_mask_batch`；触发改用 `compressible_chars`；
  保留最近 K 个 block；默认 `max_batch_chars=8000`、`min_batch_chars=300`；只改
  `ToolMessage.content`；
- `[x]` 轮末逐调用处置（2026-11-04，Stage 3）：编排 / 失败 / 被拒调用保留，联网正文
  归档 notes，其余折叠；保留回执必须排在折叠回执之前（邻接约束），否则整块保留；
- `[x]` 算法修复（2026-11-04，`NODES_REFACTOR_PLAN.md` Stage 1）：逐批压到低水位即停；
  `max_batch_chars` 成为硬上界（语义切点只落在预算内）；read 去重目标受保护、不再被
  mask；失败的 plan 调用不改变分组边界；
- `[x]` 按 `FoldGroup` 分组折叠（Stage 3）：同一 plan epoch/step 内、硬边界隔开的连续
  块为一组，每组一条 `SystemMessage`；轮末与轮内共享 `group_segments()` / `parser` /
  `policy`（轮末不切预算）；
- `[x]` 轮内保护表补上编排工具：`read_note` / `read_event_log` 等 state 回执不再被
  mask；`dispatch_subtasks` 的 worker 结论轮末也保留（`policy.KEEP_TOOLS`）；
- `[ ]` 摘要有容量上界（2026-11-04 决定**暂不设上限**：截断会丢 notes ref，等真实
  会话基线再定）；
- `[x]` 轮末压刚完成的周期（2026-11-04，§8.6 未决点 1）：有最终答复时折到答复之前，
  当前轮工具块也折；无最终答复时退回最后一条 Human 之前，保留在途结果；
- `[x]` Phase 2 写操作 args 清理（2026-11-04）：`write_file` / `create_file` 的
  `content`、`edit_file` 的 `old_string` / `new_string` 换成带长度指针，计入可压池；
- `[ ]` 整块墓碑 / 视图化（checkpoint 原文保留、mask 是视图）；
- `[x]` `CompactNode` 与 `CompressNode` 统一到共享分组/解析/策略（`group_segments` +
  `parser` + `policy`；轮末只分组不切预算）；
- `[ ]` 关键词/词嵌入软边界（可选实验）；
- 验证：全量 **718 passed / 7 skipped**（2026-11-04）；IndexTTS2 step 758 样本上，当前轮
  content 129,459 → 22,372（节省 107,087），15 条批级指针 + 9 条状态回执指针 + 14 条
  同批短戳，失败/被拒原文保留；
- 新增测试：`test_plan_step_change_starts_new_batch` /
  `test_mixed_block_masks_only_successful_sibling` / `test_keep_blocks_protects_last_blocks` /
  `test_batch_pointer_lists_same_batch_targets` /
  `test_protected_content_does_not_trigger_batch_mask` /
  `test_budget_cut_prefers_semantic_milestone`；CompactNode 的三条 failed/denied 用例改为
  “整块保留”；Stage 1–3 回归网见 `tests/test_compression_characterization.py`（10 例），
  按组摘要与逐调用处置的用例在 `tests/test_compact_node.py` /
  `tests/test_compress_node.py`。

## 9. 落地清单（改动面）

| 文件 | 改动（`[x]` = 2026-09-10 已核对落地，`[ ]` = 未做） |
|---|---|
| `app/agent/state.py` | `[x]` `notes: dict[str, NoteEntry]`（`NoteEntry` 在 `app/schema/agent_schema.py`）；`[ ]` `work_log: list[WorkLogEntry]` |
| `app/agent/nodes.py` | `[x]` 新增 `CompactNode`（轮末折叠）；`[x]` `ReviewNode` 被拒时回灌带 `decision=denied` 戳的消息；`[ ]` ToolNode 的"意图 + verdict"日志条目；`[ ]` `LLMNode` 注入 `# 会话工作日志` 与 notes 索引（现只注入 提示词/工作区/计划/画像/记忆）；`[x]` 批级压缩 Phase 1（CompressNode 批级分组 + CompactNode 失败块保护，§8.6）；`[x]` 2026-11-04 压缩引擎抽出到 `app/agent/compression/`，本文件只留两个 state 适配器；`[ ]` 轮末刚完成周期 / 统一 planner |
| `app/agent/compression/` | `[x]` 2026-11-04 抽出：`models` / `parser` / `policy` / `planner` / `compact` / `compress`；`[x]` Stage 3：`FoldGroup` / 按组摘要 / 逐调用处置已落地 |
| `app/agent/tools.py` | `[x]` `read_note`（`InjectedState`，source=`notes`）；`[ ]` `search_notes` / `notes_by_topic` / `list_topics` / `update_note_tags` / `drop_note` |
| `app/agent/graph.py` | `[x]` `compact_node` 已接线（位置：`llm_node` 无 tool_calls 且超预算 → compact → END，见 §8.1）；`[ ]` 二期回边预算触发 |
| `app/agent/prompt.py` | `[x]` tool 轮 content 写一句话工作日志（"工作基调 8"）；`[x]` 折叠语义（"旧输出不会回来、需重取"）；`[ ]` notes/`read_note` 用法与 promote 收尾习惯 |
| `app/agent/tool.json` | `[x]` `read_note` 免审登记；`[ ]` 其余 note 工具（`drop_note` 需审那条随未实现而缺席） |
| 预算阈值 | `[x]` 常量 `_CONTEXT_BUDGET_DEFAULT = 60000` + `CompactNode(content_budget_chars=…)` 构造参数；`[ ]` env 覆盖与块数阈值（§8.4） |
| `tests/` | `[x]` `tests/test_compact_node.py`（脚本化消息驱动：只折完整块、保留当前轮、幂等、预算门）与 `tests/test_review_routing.py`；`[ ]` work_log 双半行与 denied 条目的测试（随该机制未做） |

> 折叠是**纯确定性逻辑**（不依赖 LLM），机制层单测即可固化——复用 `tests/test_review_routing.py` 的"假消息驱动节点"模式，无需真实模型/token。

## 10. 分期

- **P0（首期）**：`work_log` 结构化双半行 + `denied` 条目；notes 会话级 state dict + `read_note/search` + 懒分类工具；轮边界 `compact_node`；promote 收尾引导。收益最大、风险最低（只在轮界动手）。
  - **进度（2026-09-10 核对）**：`notes` 会话级 dict `[已落地]`、`read_note` `[已落地]`、轮边界 `compact_node` `[已落地]`（位置改在 END 前，见 §8.1）、审批被拒时回灌 `[approval_denied]` 消息 `[已落地]`（但**不是**设计里的 work_log `status=denied` 条目）。**未做**：`work_log`（含 verdict/意图双半行）、`search_notes`/懒分类工具、notes 索引注入、promote 收尾引导。
- **P1**：同轮超长编排的滚动折叠（§8.1 回边触发），加 `keep_blocks` 保留区压力测试；notes 归档阈值/容量标定。批级压缩 Phase 1（CompressNode 批级分组 + CompactNode 失败块保护）已落地；Phase 2 的 args 清理、刚完成周期折叠、统一 planner 已落地；整块墓碑与软边界仍未做，见 §8.6。
- **P2（评估接线）**：evaluation 增加"上下文/轮数/token"断言，量化折叠前后达成率与成本；为 §8.4 阈值与"意图半行 token 增量/遵从率"提供实测依据。

## 11. 开放问题

- **意图半行的成本与遵从**：强制 tool 轮 content 有一句日志，每轮都多付 token、且模型偶发不遵从——默认值/回退策略要实测（§6、§10 P2）。
- **notes 数值**：单条容量上限、总数上限、过期策略；归档触发阈值（kind + 体积）标定。
- **promote 约定**：落点文件是否给模板（`docs/决策记录-<topic>.md`？）、是否让模型先列出"建议 promote 清单"再逐条过审批。
- **`drop_note` 审批**：定为需审后，与"折叠清 notes 索引"的交互要一致（折叠不影响 notes 实体，实体只由显式 drop / promote / 会话结束清理）。
- **阈值标定**：无本地分词器，字符↔token 换算靠实测；默认保守到"宁可少折"，因为折错了会丢正在迭代的上下文。
