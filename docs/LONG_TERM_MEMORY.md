# Theta $\theta$ 长期记忆 + resource 目录重构设计

> 状态标注：`[已落地]` = 已实现；未标注 = 目标态（设计方向）。
> 相关：[[docs/CONTEXT_ENGINEERING]]（notes/work_log 折叠）、[[docs/MULTI_AGENT]]、`docs/README.md`。
> 目标先行实现**最简单的长期记忆**，resource / sqlite 目录重构是它的前提。

---

## 0. 背景与目标

**现状（改动前的样子，2026-09-10 前）**：`resource/agent.db` 是**单一 sqlite 库**（~105MB，含 wal/shm；当时路径由 `app/tui/driver.py::get_agent_db_path` 算出，该模块已拆），所有会话共用一个库、thread 硬编码 `conversation_456`；没有**跨会话**的长期记忆——重启后模型不认识用户偏好 / 项目的稳定结论 / 之前敲定的约定。

**目标（本设计）**：
1. **resource 目录重构**：按「工作区沙箱」分区；每个工作区下再分 `memory/`（长期记忆 md）与 `sessions/<session_id>/`（该会话的 sqlite checkpoint）。
2. **最简单的长期记忆**：记忆落成 md 文件，每轮以 `SystemMessage` 注入上下文；prompt 写明"何时该写入记忆"；提供**专门的记忆写入/读取工具**。
3. 只做"能跨会话记住稳定事实"的最小闭环，不做记忆检索/摘要分层/多会话 UI。

**为什么要现在动目录**：长期记忆与 checkpoint 都要按 (工作区, 会话) 定位落点，现在单库 + 固定 thread 没有这个维度；先重构出"按工作区/会话分目录"的地基，记忆才能干净落地。

---

## 1. 边界：长期记忆 ≠ 已有通道

| 通道 | 作用域 | 载体 | 用途 | 与本设计的关系 |
|---|---|---|---|---|
| `work_log` / `notes`（CONTEXT_ENGINEERING） | 会话内 | AgentState + ToolMessage | 折叠时留"不可重取的结论"，`read_note` 按 ref 取回 | **不混**：notes 是会话内短期归档 |
| `promote 进 repo`（CONTEXT_ENGINEERING 提法） | 仓库 | 写入工作区 | 结论沉淀进项目真值 | 方向不同：那是写进 workspace |
| **长期记忆（本设计）** | **跨会话、按工作区** | `resource/<ws>/memory/*.md` | 记住用户偏好 / 项目稳定事实 / 敲定的约定，每轮注入 | 独立新通道 |

与 **RAG 姿态**的关系：记忆 md 是**磁盘上的一等真值文件**（`resource/<ws>/memory/`），每轮原样注入 SystemMessage；不做"向量索引 = 第二权威源"，遵循仓库既有"绝不缓存游离副本当第二权威"的结论（见 CLAUDE.md RAG 段）。记忆只存**工作区里重取不到的稳定结论**（偏好/约定/决策史），不存可重取的 dump。

---

## 2. resource 目录重构 `[已落地]`

目标布局：

```
resource/
  <workspace_key>/                  # 一个"工作区沙箱"一个目录
    reviewed.json                   # 用户"免审 allowlist"：被用户认可、此后免审的工具（见 §5）
    memory/
      memory.md                     # 本工作区长期记忆（**单文件**，Phase B，见 §3）
    sessions/
      <session_id>/
        agent.db                    # 该会话的 LangGraph checkpoint（AsyncSqliteSaver，不变，只换目录）
```

**workspace_key 规则（稳定、可读、防路径冲突）** `[已落地]`：
- 由工作区**绝对路径**（与 file_io/graph 一致的 resolve 后路径）稳定映射而来；
- 形如 `<全路径可读 slug>_<sha1(abspath)[:8]>`（例：`E:\code\python\Myllm` → `E-code-python-Myllm_3f9a1c2b`）——
  slug = 把 `\` `/` 及盘符 `:` 替换成 `-`，避免含 `/`、`.`、盘符等非法目录名的路径直接当目录名；
- 同一个工作区永远落在同一 key（跨进程/跨重启一致）；
- **哈希是消歧必需、不是装饰**：`-` 替换不可逆，`C:\work\a-b` 与 `C:\work\a\b` 的 slug 都是
  `C-work-a-b`，不加哈希这两个工作区就共用一份 `resource/<key>/`（checkpoint 与 memory 一起串味）。
  8 位十六进制（32 bit）在同一台机器的工作区数量级下碰撞概率可忽略，又不过度拉长目录名；
- slug 与哈希**同源**（都用同一个 resolve 后字符串），所以 key 是「解析后绝对路径」的纯函数；
  resolve 顺带归一符号链接与 Windows 真实大小写/短名，无需再 normcase。
- **旧 key 目录的处置**：换 key 形态前落盘的 `resource/<旧 key>/` 不再被读，**代码不自动删**
  （判据不可靠：路径末尾若恰好形如 `proj_deadbeef`，旧 key 也满足「`_`+8 位 hex」收尾；且
  `resource/` 下可能有其它机器/其它路径的合法工作区）。副作用：旧会话 checkpoint 与旧 memory
  一并不可达，同一工作区也等于开新会话。是否手工清理整个 `resource/` 由使用者自行决定。

**session_id 规则（新开程序 = 新开对话，2026-09-08 定）**：
- **默认每个程序启动 = 一个新会话**：**当前实现只有 `uuid4().hex`**（`app/tui/runner.py`）；设计里 `config.configurable.session_id` → env `AGENT_SESSION_ID` → uuid 的**三级解析尚未实现**（2026-09-10 核对：`AGENT_SESSION_ID` 全仓只出现在本文档），接多前端 / LangGraph Platform 时再补；
- LangGraph `thread_id` 沿用 `session_id`；
- 想续旧对话 → 用 **`/session`**（见 §7）列出 `resource/<ws>/sessions/` 下的历史会话并切换（切到该 id 重开 checkpointer、接着它的消息栈/thread 走）；
- 每个 session 一个独立 sqlite 文件 → 多会话 checkpoint 天然隔离，不再挤一个 105MB 单库。

**装配点（改动前 → 已落地）**：改动前 db 路径由 `app/tui/driver.py::get_agent_db_path()` 无参算出（固定 `resource/agent.db`），工作区在 `get_main_agent_graph` 内解析，两者的工作区来源不统一。**已落地**：路径统一到 `app/resource.py`（`session_db_path(workspace, session_id)` / `workspace_key`），基座 `app/platform/runtime.py::build_session_runtime` 在建 checkpointer 前就拿到工作区（由前端解析后**作参量传入**）；未另造"单一解析出口"函数——"前端定工作区 → 参量传 → resource 只算 key"已经够清楚（见 §7.1）。

---

## 3. 长期记忆 md 格式（Phase B 最简：单文件）

`resource/<ws>/memory/memory.md`，逐条追加，**每条一行可解析**，便于注入截断与工具按 key 改：

```markdown
# 长期记忆（工作区 <ws_key>）

## [m1] user-preference · 2026-09-08
用户要求：输出与注释用中文。

## [m2] decision · 2026-09-08
多 agent 子 agent 传输定案：当前 stdio，HTTP 等 DAG/@mention 再实现。
```

- **key**：单调递增 `m1, m2, …`（或语义 slug），供 `read_memory`/改写定位；**type** ∈ {user-preference, decision, convention, project-fact, …}；带**时间戳**。
- **体积上限**：注入用整份 md 截断（建议 cap ~6–8k 字符，超出部分系统只注入最近 N 条 + 提示"需要更早记录用 read_memory"）；超限触发的**压缩/归档**列为 Phase C（不做进最简版）。
- **幂等**：写工具只追加新条目或按 key 覆盖，不做全文自由改写（防模型把记忆越改越乱）。

---

## 4. 记忆注入（SystemMessage，每轮）`[已落地 2026-09-10]`

在 `LLMNode`（`app/agent/nodes.py`）每轮拼系统消息时，追加一条：

```
# 长期记忆
<memory.md 全文 / 截断到 cap>
```

拼装顺序（`SYSTEM_PROMPT` → `workspace_context_block` → `# 当前任务`(计划) → **`# 项目画像`** → **`# 长期记忆`**）。
- 记忆只在主 agent 每轮注入；worker（子 agent）是临时资料收集器，**不注入不写记忆**（隔离）。
- 读取方 = 系统注入本身即"读取"；`read_memory` 工具用于注入被截断后取更早/更细条目（见 §5）。

**memory 读取器**（原始设想）：由 memory 提供器（见 §7）读 `resource/<ws>/memory/memory.md`，经 LLMNode 构造参数传入（`memory_provider`/内容注入）——**实现时未采用这条**：LLMNode 本就持有 `workspace_path`，直接每轮读盘更直（见本节的"已落地实现"）。

**已落地实现（2026-09-10）**：

- `LLMNode.__call__` 在计划块之后追加 `SystemMessage(memory_block(workspace_path))`——**没有**引入 §7 设想的 `memory_provider` 构造参数：LLMNode 本来就收着 `workspace_path`，直接每轮经 `asyncio.to_thread` 读盘（避开 langgraph dev 的 blockbuster）比再造一层提供器更直。若日后要换来源（如多文件/远端记忆）再抽 provider。
- `memory.py::memory_block(workspace_path, cap=MEMORY_INJECT_CAP)`：注入**剥掉 HTML 注释**的可见文本（模板的格式示例不给模型）；无条目返回空串、调用方跳过该条系统消息；超 `cap`（**8000 字符**，§3 暂定 6–8k 内取上限）**从最新往前装**，被挤掉的旧条目退化成一行"未注入 + 编号清单"，模型据此用 `read_memory` 取回。不做压缩/归档（Phase C）。
- **worker 隔离**：`LLMNode` 加注入开关（2026-09-10 更名为 `inject_session_context`：画像与记忆同属"会话级上下文"），`get_sub_agent_graph` 传 `False`——worker 与主 agent 共用 LLMNode，不给这个开关就会顺带把记忆注进 worker。

### AGENT.md（项目画像）≠ memory（约束/偏好/策略）——2026-09-08 定

两条都要注入，但**内容与分工不同**，别混成一个文件：

| | AGENT.md（工作区根） | memory.md（`resource/<ws>/memory/`） |
|---|---|---|
| 位置 | 工作区根 `/AGENT.md`，程序可检测 | resource（agent 私有，不进仓库） |
| 内容 | **项目整体画像**：这是什么项目、目录/技术栈/主流程、怎么构建/跑测试等大面 | **对它的执行约束**：针对代码/用户要求的约束、偏好的执行策略、运行环境细节、敲定的小约定 |
| 回答的问题 | "这是什么项目？"（首轮快速掌握大体） | "做这个项目时怎么按用户/代码约束来"（每轮记得） |
| 谁写 | 人工维护，或 `/init` 生成骨架（随仓库走） | agent 经 `write_memory` 写（免审） |
| 注入时机 | **会话第一次启动时读入**，作为该会话"项目画像"进入系统上下文（首轮全量；内容大时后续可降级，Phase B 不裁） | **每轮** SystemMessage 注入（§4） |

实现注记：AGENT.md 在会话建立时被程序检测/读入，作为该会话的常驻项目画像与 SYSTEM_PROMPT/workspace 同源拼装；memory.md 每轮实时读盘。AGENT.md 的生成不归 `write_memory`（那是 resource 私有记忆），归 `/init` 命令（写进工作区、随仓库走）。

**读取侧已落地（2026-09-10）**：`app/agent/profile.py::agent_md_block()` 读工作区根的 AGENT.md（加一行 `# 项目画像` 块标题；缺文件/空白 → 空串；超 cap 8000 截断），`LLMNode` 在 `inject_session_context` 打开时注入——顺序 `SYSTEM_PROMPT → 工作区上下文 → 计划 → 项目画像 → 长期记忆`，画像**实例内 memo**（会话首启读入，不每轮重读；改画像重开会话即生效），worker 与记忆一起关掉。独立成模块而非并入 `memory.py`：两通道不混一个文件（本表的分工）。

---

## 5. 记忆工具（写入 / 读取）`[已落地 2026-09-10]`

**已落地形态**：**主 agent 侧的 state 工具，走 OrchestrateNode**（复用 plan/notes 的注入范式——路径对模型隐藏），tool.json 登记 `source="memory"`、并入 `ORCHESTRATE_SOURCES`。worker 侧的隔离由 `worker_tools` 的 source 过滤（只放行 file_io/git/web_search）结构性挡住，无需额外代码——§9 那条"worker 不带记忆"由此保证。

- `write_memory(type, content, key="")`：省略 `key` → **追加**一条（编号由系统按现有最大号 +1 分配，`m1, m2, …`）；给了已有 `key` → **覆写**那一条的 type 与正文，**保留编号与首次记入日期**（改写不重排、不重新编号）。返回**回执 + 当前总条数**，不回全文（写记忆会频发，回全文会把上下文推高；要看全貌另调 `read_memory`）。工作区经 `InjectedWorkspace`（`InjectedToolArg` 子类）注入、对模型隐藏，模型只给 type/content/key。
- `read_memory(key="")`：`key` 为空 → 返回全文（剥掉模板的 HTML 注释）；给 key → 返回单条。只读——文件不存在也不建。
- **解析纪律（实现要点）**：模板刻意把"追加格式示例"放在 HTML 注释里，而示例正文本身就含一行 `## [m1] …`。解析器只认**注释外、且位于行首**的标题——否则示例会被当成真实条目（编号凭空 +1，覆写还可能改到示例那行）。
- **路径安全**：记忆文件路径由 worker 用注入的 `<ws>/memory` 根 + 白名单名构造（`memory.md`），不接用户/模型任意路径——model 只给 `type/content/key`，不碰路径。
- **审批策略（定，2026-09-08）**：`write_memory` **免审**——默认、无特殊情况不进审批（记忆是 agent 自己沉淀的稳定结论，写在 `resource/<ws>/memory/`，成本/风险低，靠 prompt 纪律 + 长度上限兜底）；若日后出现"坏记忆跨会话污染"，再把 `write_memory` 提为 `need_review:true`，工具层不变。
- 与 `read_note` 的区别：notes 读会话内折叠归档（短命、ref=rN）；memory 读跨会话记忆（长命、ref=mN）。两者并存、语义分开。

**reviewed.json（用户免审 allowlist，2026-09-08 引入）**：
- `resource/<ws>/reviewed.json` 记录用户在某次审批时选择"该工具以后都免审"的工具名清单——语义 = 用户在**该工作区**维度的一次性授权：命中清单的工具即使 tool.json `need_review:true` 也免 interrupt（auto-approve）。只对当前 `workspace_key` 生效、不跨工作区扩散。
- 写入入口 = TUI 审批面板提供"记住并允许（以后免审）"选项（或直接手工编辑该 json）；`write_memory` 属默认免审、**不进这张表也不需要**。
- **本轮先不做**（2026-09-08）：reviewed.json 的加入交互（面板"记住并允许"）与 ReviewNode 接线**延后实现**——概念与 memory 写免审先在文档定案，进代码的时间另排，Phase A/B 不含它。
- 实现位（将来）：main 侧把清单并入 ReviewNode 判定（interrupt 前查 allowlist，命中即 approved）；worker/子 agent **不吃这张表**（保持只读调查隔离，避免把免审扩到 worker 联网等）。

---

## 6. prompt 增补（SYSTEM_PROMPT）

新增一小节"长期记忆"给主 agent 的纪律（放"工作基调"或独立小节）：
- **该写**：一段有结论的工作收尾时，把**跨会话仍有用的稳定事实**沉淀进记忆——用户的明确偏好/约束、敲定的架构决策、工作区的既有约定/特殊之处（如"此仓库注释用中文""子 agent 传输定案=stdio"）。
- **别写**：临时过程、可随时从工作区/git 重取的 dump、猜测/未定的事、个人操作流水。
- 写法：`type` 分 `user-preference / decision / convention / project-fact`；`content` 一句话成条、带出处或日期更佳。

---

## 7. 装配与迁移（改动面）

1. **路径解析收敛为一处**（Phase A）`[已落地]`：
   - 新增轻量 helper（实为 **`app/resource.py`**——放 app 根而非 `app/agent/`：只依赖 stdlib/pathlib，import 不触发 .env）暴露：
     `workspace_key(workspace_path) -> str`、`memory_root(workspace_path) -> Path`、
     `session_db_path(workspace_path, session_id) -> Path`；
   - `get_agent_db_path` 已删除，路径统一走 `session_db_path`；工作区一致性靠**参量传递**保证（前端解析 → 基座/图收同一个 `workspace` 字符串），未再抽"共用解析出口"函数——`_resolve_workspace`（`app/agent/graph.py`）仍只管"参量为空 → 默认 `<项目根>/tmp`"这一个分支。
2. **memory 提供器 / 节点**（Phase B）：
   - `app/agent/memory.py`：读 md、格式化 SystemMessage、追加/覆盖条目、cap 截断（纯函数为主，便于单测）；
   - LLMNode 收 memory 注入（读一次文件 → SystemMessage）；
   - `write_memory`/`read_memory` 工具 + `source="memory"` 分流 + tool.json 登记。
3. **session/thread** `[已落地]`：前端每次启动 = 新会话（`uuid4().hex`），同时作 `thread_id` 与 db 目录名；硬编码 `conversation_456` 已删除（三级解析见 §2 的说明）。
4. **旧数据** `[已落地]`：`resource/agent.db` 是 dev 运行产物（gitignore）。Phase A 切换路径后它不再被读，且**启动时自动删除**（`app/platform/loop.py` 调 `app/resource.py::remove_legacy_single_db`，用户已确认）。
5. **测试** `[已落地]`：`test_agent_db_path_resolves_under_resource_dir` 已随新布局删除，改由 `tests/test_resource.py`（路径布局）等覆盖；另已有 md 读写 / cap / 工具注入 / 会话命令（`tests/test_sessions.py`、`test_commands.py`）的单测。`reviewed.json` 因未实现，仍无对应测试。

**TUI 命令层（/init · /session，2026-09-08 引入；2026-09-10 起落 `app/platform/commands/` 包）**：新开程序 = 新会话，续旧会话与沉淀项目约定走命令。命令是**基座的控制面事件（图之外）**——基座解析并处理，前端只渲染其输出（未识别命令走 `Notice` 事件，不喂给模型）；`q/quit/exit` 的退出词仍在 `loop.py`，未纳入命令表。
- `/init` `[已落地 2026-09-10]`：**投一段预设提示词**（`INIT_PROMPT`，放在 `commands/prompts.py`——它是这条命令的载荷，不是每轮行为契约），随后走一条**正常 turn**：模型用已有只读工具通读工作区 → 在工作区**根目录**生成或刷新 `AGENT.md`（已存在则先读再 `edit_file` 刷新）。不加工具、不加节点、不加线程；写文件照常**撞审批**，用户过目内容再落盘。AGENT.md 是项目画像、随仓库走，不是把 resource 私有 memory 搬进去（memory 属约束/偏好层）。
- 会话三条命令 `[已落地 2026-09-10]`（原设计的"列出并选号切换"落地时**拆成三条**，不做"列完就地等选号"的半途状态）：
  - `/list session`：列出当前工作区沙箱的全部会话（`*` 标当前；短 id · 最后活动时间 · 消息条数 · 末条 AI 正文摘要），按最后活动倒序，只给最近 10 个读 checkpoint；
  - `/new session`：`uuid4().hex` 新建并切过去（库在下次输入时才建）；
  - `/session <短 id 或完整 id>`：切到已存在的会话（唯一前缀即可；歧义/未命中只提示、不动当前会话）。切换 = 关旧连接 → 换 `session_id` → 丢弃 step（下一条消息按新 thread_id 懒重建），当前会话的 checkpoint 本就逐步落盘、无需额外"落定"。
- 识别 `/` 前缀命令的位置**在基座**（`app/platform/loop.py` 拿到输入行后先问 `parse_command`），**不在输入层/前端**——前端只负责把一行输入交上来、把 `Notice` 渲染出去；`q/quit/exit` 的退出词仍留在 `loop.py` 的 `EXIT_WORDS`，未纳入命令表。

**命令分两类（2026-09-10 定型）**：命令不只是"投提示词"，基座侧还有要动/读运行时状态的：
- **提示词型**（`PROMPT_COMMANDS`）：把预设提示词当本轮用户输入投出去，走正常 turn —— `/init` 即此类（`/compact`「要求模型压缩上下文」将来也走这一路）。
- **基座动作型**（`ACTION_COMMANDS`）：基座自己执行、不起 turn，结果经 `Notice` 事件回话 —— 已落地 `/help`（列全部命令）、`/list session`、`/new session`、`/session <id>`；规划中（**位置已留**）：`/content`（显示当前上下文占用量）、`/compact`（要求模型压缩上下文）。
- 两表都只存**名字 + desc + 载荷/handler**，帮助文案由 `help_text()` 从表生成（对齐靠格式串，不手打空格）——加一条命令即出现在 `/help` 与"未识别命令"提示里，不必改 `loop.py`（`parse_command` 认表、`loop` 按类型分派，已有测试分别钉住这两条通路）。

---

## 8. 分期

- **Phase A · resource + 会话目录重构** `[已落地 2026-09-10]`：路径解析收敛 + workspace_key + `sessions/<sid>/agent.db` 落位 + 每次启动用新 session_id + 旧库不读（并删除）+ 测试更新。会话的**列表/切换命令**随后在 Phase B 一并落地（§7 会话三条命令）。此阶段无记忆，纯地基。
- **Phase B · 最简单长期记忆闭环 + 项目画像**：memory.md 格式 + 写入/读取工具 + tool.json/source + LLMNode SystemMessage 注入 + SYSTEM_PROMPT 纪律 + **AGENT.md 检测/会话首启注入 + `/init` 生成骨架** + 单测 + 手工冒烟（两段会话：第二段能看到第一段写的 memory；重开会话能看到 AGENT.md 画像）。`reviewed.json` 的加入交互/接线不在 A/B。
  - **进度（2026-09-10）**：memory.md 格式 + `write_memory`/`read_memory` 工具 + tool.json/source + **LLMNode 每轮注入（§4）** + 单测 `[已落地]`（§3/§4/§5）——记忆闭环已通（写入 → 落盘 → 下轮注入），worker 侧不注入、工具也被 `worker_tools` 挡住。**同日续做**：AGENT.md 读取侧（`app/agent/profile.py` + LLMNode 注入）与 `/init` 命令（`app/platform/commands/`，投预设提示词）`[已落地]`（§4/§7）——画像闭环亦通（`/init` 生成 → 下个会话注入）。**尚未做**：SYSTEM_PROMPT 的"长期记忆/项目画像"纪律小节（§6）。会话三条命令（`/list session`/`/new session`/`/session`）与 `/help` 亦已落地（§7）。
- **Phase C · 明确不做（后续再议）**：记忆压缩/分层摘要、记忆全文检索、跨工作区共享记忆、多会话选择 UI、reviewed.json 交互接线、把记忆 promote 进 repo（与 CONTEXT_ENGINEERING 的 promote 复用/分合另议）。

---

## 9. 开放问题 / 待定

**已定（2026-09-08）**：
- 单 `memory.md`；`write_memory` 免审（无特殊情况）；
- 新开程序 = 新会话（默认自动 session_id）；`/session` 列出/切换会话；同工作区多会话**共享** memory.md；
- `/init` 在工作区根生成/刷新 `AGENT.md`（**项目画像**，会话第一次启动注入）；AGENT.md=项目画像（这是什么项目），memory=执行约束/偏好/策略/运行环境（怎么做这个项目）——两通道分工，见 §4；
- `resource/<ws>/reviewed.json` 用户免审 allowlist 概念定案，但其加入交互与接线**本轮先不做**。

**待确认 / 后置**：
- [ ] memory.md 条目上限与注入 cap 的具体阈值（暂定整份 cap ~6–8k 字符）。
- [x] `/session` 切换细节（2026-09-10 定）：**只在空闲时**处理命令（`loop` 的空闲分支），故不存在"切到一半的 run"；当前会话 checkpoint 逐步落盘、无需额外落定；列表摘要 = 该会话 checkpoint 里**末条有正文的 AI 消息**首行（停在工具链中间时往前找），只读一个 tuple、不编译图。
- [ ] reviewed.json 的加入交互与 ReviewNode 接线（已排本轮外，Phase A/B 不含）。
- [ ] memory 是否给 worker/子 agent 只读入口（并行收集器要不要带记忆？当前倾向**不带**，隔离最简单）。
