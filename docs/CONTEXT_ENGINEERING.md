# CodingAgent 上下文工程设计

> 本文档记录 CodingAgent 的上下文管理（消息留存 / 折叠 / 笔记区）设计讨论与结论，作为实现参考。
> 核心主题：**工具返回是"高时效、低重取成本"的信息，不该无限占住模型窗口；跨轮只留"不可重取的结论"，不流"可重取的 dump"。**
>
> 状态标注：`[已落地]` = 已完成；`[前置]` = 本设计依赖、已完成；未标注 = 目标态（设计已收敛，尚未实现）。
> 相关：[[docs/EXCEPTION_DESIGN]]（异常语义过滤器，决定什么进模型）。

---

## 1. 背景与问题

- `AgentState.messages` 用 `add_messages` 无限累加（`app/agent/state.py`），仓库里**没有任何裁剪/摘要机制**（`grep RemoveMessage|summar|trim 零命中`）。
- 跨轮靠 checkpoint（`app/main.py` 现为 `AsyncSqliteSaver`）持久化，所以每轮喂给模型的都是"全量历史 + 每轮重拼的系统消息"。
- Token 大头几乎全在 `ToolMessage`：整文件读取、目录树、检索命中、命令输出、网页正文。而这些内容大多**时效极强**——文件一旦被改，几轮前的整文件 dump 就是纯噪声。

## 2. 现状：messages 里到底有什么

进 `state["messages"]` 的只有四类（系统消息**不落库**，每轮由 `LLMNode` 现拼，见 `app/agent/nodes.py::LLMNode`）：

| # | 消息 | 来源 | 特征 / 体积 |
|---|---|---|---|
| 1 | `HumanMessage` | `app/main.py`、`evaluation/runner.py` | 小；任务源头，不可重取 |
| 2 | `AIMessage`(带 tool_calls) | `LLMNode` 返回 | 正文多为填充语；tool_calls 块负责**协议配对** |
| 3 | `AIMessage`(不带 tool_calls) | 同上 | 任务的成品答复 |
| 4 | `ToolMessage` | `ToolNode` / `OrchestrateNode` 回执 | **体积大头** |

两个结构性事实（折叠必须利用）：

- **计划不靠历史回显**：`current_plan` 独立存 state，`LLMNode` 每轮把当前计划重注入为 `# 当前任务`（`nodes.py:46-53`）。于是 `create_plan`/`update_plan_step` 每次整份回显的 ToolMessage 快照对模型**双重冗余**。
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
| git 只读 `git_status/diff/log/branches/fetch` | 消费后可折 | 否 | repo 即真值 |
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

- **工具**（agent 侧 `InjectedState`，仿 orchestrate 模式）：`read_note(ref)` / `search_notes(q)` / `notes_by_topic(topic)` / `list_topics()` / `update_note_tags(...)` / `drop_note(ref)`。全文经 `read_note` 取回的是**短命 ToolMessage，随折叠走**；每轮注入模型只有索引行 `[notes#r3] 光伏市场调研（research · 2.1k）`。
- **tool.json 登记**：read/search/by_topic/list 免审（只读/整理 state）；`update_note_tags` 免审（state 内元数据）；`drop_note` **需审**——丢的是尚未 promote 的一次性研究产出。
- **promote 不是新机制，是收尾动作**：研究告一段落、结论值得留档时，agent 用现有 `create_file`/`edit_file` 把提炼结论写进 repo（如 `docs/决策记录-<topic>.md` 或代码注释），走正常审批；notes 正文/URL 作引用来源。prompt 把这条列为研究类工具后的收尾习惯。

## 8. `compact_node`：滚动块折叠

### 8.1 触发位置

第一版只做**轮边界折叠**：

```
START ─► compact_node ─► llm_node ─► …
```

依据 §3.4：新用户消息从 START 进入、`Command(resume)` 不从 START 进入 → 该节点**每轮只跑一次、恰在上轮结束后**，是折叠的最安全时机。

二期（单个超长编排内部的滚动折叠）在 `tool_node → llm_node`、`orchestrate_node → llm_node` 回边加预算触发，靠 §8.3 的"保留区"保护刚返回、待模型反应的块。

### 8.2 折叠算法

预算超限才动手，否则原样直通（零成本、幂等）：

1. 算消息总成本（字符或块数，见 §8.4）；未超预算 → 不变。
2. **保留区** = 最近 `keep_blocks` 个块 + 最后一条消息起的尾部，原样保留（覆盖模型正在迭代/收尾的部分；最新一批工具结果必须留到 END 供模型总结）。
3. **折叠区** = 保留区之前的消息。沿块边界整块剪除（见 §3.1）：从头部起，只允许把剪裁点定在"紧挨某 `AIMessage(tool_calls)` 之前"或历史起点；被剪块的可重取内容不重建为消息（省 token）。
4. 产出新 `messages` 写回 state（决定性地；对相同输入产生相同输出，便于无 LLM 单测固化）。

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

- 无 tiktoken 类本地分词：DeepSeek 走 OpenAI 兼容，token 数在 agent 侧不可精确算。用**字符数与块数双阈值**近似，留环境变量覆盖（如 `CONTEXT_BUDGET_CHARS`、`CONTEXT_KEEP_BLOCKS`），默认值按目标模型上下文保守取，实测再调。
- **不走 `app/config.py` 的 Settings**：该文件字段全无默认值、须在 `.env` 出现（历史约定）。预算阈值属于可调内部参数，用模块常量 + `os.environ` 覆盖即可，避免每次加参都动 `.env`（与 `WORKSPACE_PATH` 走纯 env 的先例一致）。

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

## 9. 落地清单（改动面）

| 文件 | 改动 |
|---|---|
| `app/agent/state.py` | 新增 `work_log: list[WorkLogEntry]`、`notes: dict[str, NoteEntry]`（含 WorkLogEntry/NoteEntry 类型） |
| `app/agent/nodes.py` | `ToolNode` 执行后追加"意图(content)+机器 verdict"日志；`ReviewNode` 被拒时并行补 `status=denied` 条目；新增 `compact_node`；`LLMNode` 注入 `# 会话工作日志` 与 notes 索引 |
| `app/agent/tools.py` | 新增 agent 侧 note 工具（read/search/by_topic/list/update_tags/drop，`InjectedState`）；定义"state 消费"工具集及消费节点路由（仿 orchestrate 或收敛为一个 state-tool node） |
| `app/agent/graph.py` | `START → compact_node → llm_node`；二期加 tool/orchestrate 回边预算触发 |
| `app/agent/prompt.py` | tool 轮 content 写一句话工作日志（非最终答复）；notes 存在与 `read_note` 取回；promote 收尾习惯；折叠语义（"旧工具输出不会回来，需重取/读 notes"） |
| `app/agent/tool.json` | note 工具审批登记（读类免审；`drop_note` 需审） |
| `app/agent/mcp.py` 或常量模块 | 预算阈值常量 + env 覆盖 |
| `tests/`（新增 `test_context_*`） | scripted AIMessage 驱动：折叠块不拆、保留区不动、幂等、预算触发、悬空不产生；work_log 双半行拼接；denied 条目 |

> 折叠是**纯确定性逻辑**（不依赖 LLM），机制层单测即可固化——复用 `tests/test_review_routing.py` 的"假消息驱动节点"模式，无需真实模型/token。

## 10. 分期

- **P0（首期）**：`work_log` 结构化双半行 + `denied` 条目；notes 会话级 state dict + `read_note/search` + 懒分类工具；START 轮边界 `compact_node`；promote 收尾引导。收益最大、风险最低（§3.4 保证只在轮界动手）。
- **P1**：同轮超长编排的滚动折叠（§8.1 回边触发），加 `keep_blocks` 保留区压力测试；notes 归档阈值/容量标定。
- **P2（评估接线）**：evaluation 增加"上下文/轮数/token"断言，量化折叠前后达成率与成本；为 §8.4 阈值与"意图半行 token 增量/遵从率"提供实测依据。

## 11. 开放问题

- **意图半行的成本与遵从**：强制 tool 轮 content 有一句日志，每轮都多付 token、且模型偶发不遵从——默认值/回退策略要实测（§6、§10 P2）。
- **notes 数值**：单条容量上限、总数上限、过期策略；归档触发阈值（kind + 体积）标定。
- **promote 约定**：落点文件是否给模板（`docs/决策记录-<topic>.md`？）、是否让模型先列出"建议 promote 清单"再逐条过审批。
- **`drop_note` 审批**：定为需审后，与"折叠清 notes 索引"的交互要一致（折叠不影响 notes 实体，实体只由显式 drop / promote / 会话结束清理）。
- **阈值标定**：无本地分词器，字符↔token 换算靠实测；默认保守到"宁可少折"，因为折错了会丢正在迭代的上下文。
