# Theta $\theta$ 待办 / 收口清单

> 当前工作树仍在演进：文档与代码不一致时以代码为准（约定同 CLAUDE.md 与 docs/README.md）。
> 每条记录动机/现状，避免"当初为什么没做"再次翻车；勾掉前应能指到验证它的提交。
> **已完成的条目移入 [docs/DONE.md](DONE.md)**（原文留档）；本文件只留未完成与进行中（`[~]`）。

## [~] 枚举 / 标签的真相在哪（2026-09-22 审计；第一批已落地，四项未做）

**判据已落纸**（`docs/ARCHITECTURE.md` §4.6）：一个词表只有一份真相，落在"谁定义这个词"的那一侧——
跨边界 → `app/schema`（能派生就派生）；停在单模块 → 留模块但用 `Literal`（别裸 `str`）；住在数据文件
（`tool.json`）→ 数据是登记处、**取值清单要在代码里有一处**供派生与校验。外加两条：同义不许两套拼写、
名字清单不许手抄。

**实测分布（四类；按"静默失败"风险排）**：

**A · 已有单一真相（标杆，别动）**：`PlanStatus`（+`PLAN_STATUSES` 用 `get_args` 派生）、
`SearchDepth/Topic/TimeRange`（贴上游 Tavily）、`Decision.kind` / `ApprovalStatus` / `GATE_*`、
`_ORCHESTRATE_TOOL_NAMES`（由工具对象派生）。**例外两处**：`LLMNode.format_plan_status` 的
`status_map` 手抄三种状态、`approvals._record_to_value` 的 `or "one"` 抄了 `ASK_SELECT_DEFAULT`。

**B · 同义两套词（同一件事两种拼写）**：

| 词表 | 两套写在哪 | 唯一映射点 | 问题 |
| --- | --- | --- | --- |
| 闸门种类 | `Decision.kind ∈ {approval, answer}`（schema）↔ payload `type ∈ {tool_approval, ask_user}`（`GATE_*`） | 只有 `turn.decision_to_resume` 的 if 分支 | **没有一处同时写着两张词**，人得读两个模块才敢确定对应关系 |
| 可选模态 | `"one"/"many"`：`tools.ask_user` 的 `Literal`、`gates.ASK_SELECT_MODES/_DEFAULT`、`approvals` 的 `or "one"`、`panels` 的 `== "many"`、`tui/ui.py` 的 `== "many"`、`prompt.py` 的说明 | 无 | **8 处字面量（6 处代码判断）**；加第三种模态要改 6 处 + 提示词，漏一处就静默不一致 |

**C · 词表只活在散文里（写错不报错，最危险）**：

| 词表 | 现状 | 消费链 | 漏了会怎样 |
| --- | --- | --- | --- |
| 记忆 `type`（`user-preference`/`decision`/`convention`/`project-fact`） | `write_memory(type: str)`、`MemoryEntry.type: str`——**只有 docstring 与工具说明** | 模型 → `memory.append_entry` 落盘 → `read_memory` 展示 | **写错静默落盘成垃圾条目**，无人报警 |
| `NoteEntry.kind`（`web`/`extract`/`crawl`/`research`；docstring 还写了从没产出的 `command_output`） | `kind: str`；事实上的取值表是 `CompactNode._notes_kind` 的 dict | CompactNode → 注入 / `read_note` | 同上（只影响展示，危害小一档） |
| `error_type` 取值 | 一半由 `guard._type_token` 从异常类名派生，一半**手写字面量**（`file_io` 的 `io_error`/`invalid_argument`、`web_search` 的 `upstream_error`/`no_results`、`nodes` 的 `tool_error`/`unknown_tool`） | 各 server / ToolNode → `format_tool_result` 的 `[前缀]` 约定 + `CompactNode._FAIL_PREFIXES` 回退表 | **没有取值清单**，谁都能造新值；回退表已漂过一次（`[config_error]` 从来匹配不上） |
| `worker_id` 形态（`main` / `worker-xxxxxxxx`） | `LOCAL_WORKER_ID` 单点 ✅，构造在 `sub_agent` | 基座 / 面板渲染 | 轻 |

**D · 名字清单的副本（"枚举 = 一组名字"）**：`source` 标签（`tool.json` 是登记处，代码里 **5 处**副本：
`ORCHESTRATE_SOURCES` / `ASK_SOURCE` / `tools._WORKSPACE_SOURCES` / `_WEB_SOURCES` /
`skills._skill_server_name` 的 `f"skills/{dir}"`）；`CompactNode.ARCHIVE_TOOLS`（4 个联网工具名，与
tool.json 的 `source: mcp_service/web_search` 同义）；`_EXTRA_SLICE_KEYS`（`state.py` 字段名的副本）；
`_FAIL_PREFIXES`（C 组副本）；`evaluation.DEFAULT_DENY_TOOLS`（评估自己的，可接受）。

**第一批已落地（2026-09-22）**：

- **删悬空**：`NoteEntry.kind`（只写不读，连带 `CompactNode._notes_kind` 与其"联网四件套工具名"
  的第二份副本）——判据"生产了却没人按它拆解 → 删"，同当年 `topic/tags`。
- **删字典键常量**：`STAMP_ERROR_TYPE` / `STAMP_DECISION` / `STAMP_DENIED`——字典的键用变量承接与
  写字面量没有区别（用户拍板）；保留真正有价值的 `_tool_stamp()` 构造器（保证两个生产端同形）。
- **多处使用的词表进 schema**：`AskSelectMode` / `ASK_SELECT_ONE` / `ASK_SELECT_MANY` /
  `ASK_SELECT_DEFAULT` / `ASK_SELECT_MODES` 落 `app/schema/agent_schema.py`；六个消费点（gates 校验、
  tools 签名、approvals 透传、tools 回执渲染、panels、tui）全部改 import，`"one"/"many"` 字面量清零。
- **半悬空 → 给可引用常量**：`DECISION_APPROVAL` / `DECISION_ANSWER`（`turn.py` 的分派、`approvals`
  与两个前端造 Decision 处）、`STATUS_PENDING` / `STATUS_DECIDED`（主侧写、**worker 跨进程读**）；
  并在 `approval_schema.py` 一处写明 **`GATE_*` ↔ `DECISION_*` 两套词的映射**（此前只靠
  `decision_to_resume` 的 if 隐含）。
- **配置键的取值域跟键走**：`_THINKING_TYPES` 从 `nodes.py` 挪进 `app/config.py::THINKING_MODES`。

**仍未做**：

1. `source` 标签的 5 处副本（`ORCHESTRATE_SOURCES` 应改为从 tools.py 里既有的五组工具列表派生，
   再加一条契约测试与 `tool.json` 对齐）——D 组主体；
2. `CompactNode.ARCHIVE_TOOLS` 与 `tool.json` 的 `source: mcp_service/web_search` 同义（"谁进 notes"
   ≡ "谁是联网四件套"）；
3. `_EXTRA_SLICE_KEYS`（`state.py` 字段名的副本）；
4. `LLMNode.format_plan_status` 的 `status_map` 手抄三种状态（A 组例外）。

****原始建议（留档）****：

1. 给 `记忆 type` / `NoteEntry.kind` / `select` 加 `Literal`，校验表用 `get_args` 派生（照
   `PLAN_STATUSES` 的先例）——把 C 组的两处"写错静默落盘"变成当场报错；
2. 把 `ORCHESTRATE_SOURCES` 的来源改成 tools.py 里**既有的五组工具列表**（`orchestrate_tool` /
   `note_tools` / `ask_tool` / `memory_tool` / `skill_tool` 的分组本身就是"source → 工具"的真相），
   再用一条契约测试把 `tool.json` 的 `source` 与它对齐——消掉 D 组的 5 处副本；
3. 闸门那两套词（`kind` ↔ `GATE_*`）在 schema 里写明映射常数，别再只靠 `decision_to_resume` 的 if 隐含。

**相关背景**：`docs/ARCHITECTURE.md` §4.6（判据）、§4（tool.json 的键语义与"表与解析器同住"那条纪律——
注意 `ORCHESTRATE_SOURCES` 属**分流规则**，仍留代码，只是它的**词表**该与数据侧对齐）。

## [ ] MCP 这条线的职责漂移（2026-09-22 记）

**现象**：这条线起家是"给 agent 提供工具（后来加技能）"，现在额外承担三件事——**管 MCP 进程的
生命周期**、**当 `get_skill`/`drop_skill` 的底层**、**管子 agent 的进程**。名字（`mcp.py`）只覆盖
第一件，所以后来者会误判"新东西该放哪"。

**实测**（`app/platform/mcp.py` 669 行混着四类：真 MCP 连接 ~90 / 工具 schema 与 shim ~120 /
**运行体机制 `_ServerWorker` 一族 ~200** / 会话级工具视图 ~140）；三件新职责分散在**三个层**：
技能运行体的**胶水**是 `app/agent/tools.py` 里那 466 行 + `SessionToolset`(124)，子 agent 进程在
`mcp_service/dispatch.py` + `sub_agent.py`，审批回流在 `app/platform/approvals.py`。
**职责图、判据化归属与"哪些现状是对的"已写进 `docs/ARCHITECTURE.md` §4.7**——这里只留待办。

**分批计划（2026-09-22 定，建议顺序 A → C → B）**：

- **A · 只补声明（最小，建议先做）**：§4.7 已经落了纸 ✓（本轮已做）。剩下把"运行体机制 ≠ MCP
  工具面、技能胶水该与机制同侧"这两句同步进 CLAUDE.md 的 MCP 小节即可。
- **C · 技能运行体胶水归位（最实的错位）**：把 `app/agent/tools.py` 的 466 行加载机制
  （`_register_skill_runtime` / `_check_skill_tools` / `_probe_skill_startup` / 体检 / 依赖预检）
  与 `SessionToolset`(124) 挪到**运行体同侧**（候选：`app/platform/skill_runtime.py`）；`tools.py`
  只留 `get_skill` / `drop_skill` 的**工具定义**（模型可见的 action 与回执）。收益：`tools.py` 回到
  判据 C 的定义侧（它 1165 行里约一半是这段基础设施），技能机制与它驱动的进程住一起。
  ⚠️ 这条与 [DONE.md](DONE.md)「节点过度实现」条的「记账 2」（`skills.py` 的加载机制半）是同一族，
  **别分两次做**。
- **B · 运行体机制从 `mcp.py` 独立出来**：`_ServerWorker` / `get_worker` / `_shutdown` /
  `close_session_pool` / `close_all_pools` 抽成 `app/platform/runtime_pool.py`（名字待定），
  `mcp.py` 只留连接 / 工具 / schema。收益：名字与职责对齐，将来 HTTP MCP、常驻 worker 都能复用；
  代价是一次机械搬迁（含 `_reset_pools_for_tests` 等测试接缝）。**放在 C 之后做**——那时池的边界
  比现在清楚。

**相关背景**：`docs/ARCHITECTURE.md` §4.7（职责图与判据）、§4（四问 / A-B-C）、
`docs/SKILL_DESIGN.md` §12（运行体常驻机制；§12.2 的 anyio 承重约束改动前必读）、
`docs/MULTI_AGENT.md` §5/§9/§10（常驻 worker / HTTP MCP 传输 / DAG 编排：**未落地**）。

## [ ] 散落的数值口径：归属清单（2026-09-22）

**规则（2026-09-22 定）**：每个散落的数值常量都要能回答四问——**它是谁的**（owner 模块）/
**谁读它** / **可不可调** / **跟谁同族**。答不上来就说明它没主。配套两条：**同组合并**（同族的
值只留一个出处）、**多处调用 → 进 `app/config.py`**（config 是全局权威来源，见 §4.3 的两类内容）。
命名与可见性：**跨模块读 → 公开名；只服务本模块 → `_` 前缀**；**名字要带单位/量词**（字符数 vs 条数）。

| 常量 | 归属（owner） | 消费者 | 可调缝 | 同族/约束 |
| --- | --- | --- | --- | --- |
| `TEXT_BUDGET_CHARS = 8000` | **`app/config.py`**（同组合并后的**唯一出处**） | `memory.MEMORY_INJECT_CAP` / `profile.AGENT_MD_INJECT_CAP` / `skills.SKILL_BODY_BUDGET` / `nodes._NOTE_MAX`（各自名字保留——语义不同） | 四个 `cap=` / 构造参量 | 同族自身（此前 8000 抄四遍、靠注释互指"同量级"） |
| `_CONTEXT_BUDGET_DEFAULT = 60000` | `nodes.CompactNode` | CompactNode | `CompactNode(content_budget_chars=)` | — |
| `SKILL_CATALOG_CAP = 60` | `resource/skills.py` | `catalog_block` | 参量 `cap` | — |
| `MAX_ASK_OPTIONS = 5` | `agent/gates.py` | `ask_request` 校验（提示词文案里也有"5"，属文案） | — | — |
| `_MAX_IMAGES_PER_MESSAGE` / `_MAX_IMAGE_BYTES` | `resource/images.py` | `attach_images_to_payload` | — | 同族（一条消息的图） |
| `_MAX_STEPS = 100` | `mcp_service/sub_agent.py` | worker 图 `recursion_limit` | 调用处 | — |
| `_APPROVAL_TIMEOUT_S` / `_HTTP_TIMEOUT_S` | `sub_agent` | `_await_main_decision` / httpx | 函数参量 | **约束**：HTTP 超时必须 > `INBOX_BLOCK_SECONDS`（这条曾真出过 bug） |
| `INBOX_BLOCK_SECONDS = 120` | **`app/schema`**（主侧与 worker 两侧共用） | 主侧长轮询 + worker 客户端 | — | 跨进程 → 归 schema（与 `DEFAULT_INBOX_PORT` 同理） |
| `_PREFLIGHT_TIMEOUT` / `_STARTUP_PROBE_TIMEOUT` / `_STARTUP_PROBE_CHARS` | `agent/tools.py`（技能加载组） | 体检 / 启动探针 | — | 同族（技能加载的超时与截断） |
| `_SUMMARY_CHARS = 60` | `platform/commands/session.py` | `_short_line` | 参量 `limit` | **不是**与 `DEFAULT_SUMMARIZE_LIMIT` 同类（字符数 vs 条数）——2026-09-22 改名消歧 |
| `DEFAULT_SUMMARIZE_LIMIT = 10` | 同上 | `list_sessions` | 参量 `summarize_limit` | 同上 |
| `_TIMEOUT_SECONDS` / `_MAX_CHARS` | `skills/github/server.py` | 该技能自己的 HTTP | — | 技能自带（跟技能目录走 ✓） |
| 工具签名里的默认值（`run_command(timeout=300)`、`startup_wait=5` …） | 各 MCP server 的工具定义 | **模型**（schema 可见） | 调用参数 | **不是配置**：属工具契约，随服务端工具定义留原处 |

**本轮已做**：`8000` 同组合并进 config（`TEXT_BUDGET_CHARS`）；`_SUMMARY_LIMIT` → `_SUMMARY_CHARS`；
归属清单成形（本表）。**未做**：跨模块读的私有名（`nodes._EXTRA_SLICE_KEYS`）改公开名；`_CONTEXT_BUDGET_DEFAULT`
之外还有几个"可调缝"缺失（如 `MAX_ASK_OPTIONS` 没有参量口）。

**相关背景**：`docs/ARCHITECTURE.md` §4.3（config 的两类内容 + 进入判据）、本文件「配置的归属」条。

## [ ] 函数返回值：多类型 → 值对象（2026-09-22 审计）

**判据已落纸**（`docs/ARCHITECTURE.md` §4.5）：返回值只有两种合法形态——**单一类型**（含
`X | None`，那是"有没有"）与**数据结构体**；禁止"把互斥结果塞进元组"与"三元组以上的位置返回"。
配套约定：只有一句错误文案载荷的函数统一用 `str | None`（`None` = 通过）。
**列表同形的正例**：`ToolResult`、`parse_command → PromptCommand | ActionCommand`。

**实测清单（2026-09-22 grep `-> tuple[` + 多值 `return`；共 12 处）**：

| # | 位置 | 现在返回 | 判定 |
| --- | --- | --- | --- |
| 1 | `gates.ask_request` | `tuple[dict \| None, str \| None]` | **该改**：请求 ↔ 问题说明互斥 |
| 2 | `tools._probe_skill_credential` | `tuple[bool, str]` | **该改**：结果 + 原因 |
| 3 | `commands.session.resolve_session_id` | `tuple[str \| None, list[str]]` | **该改**：命中一个 ↔ 候选清单互斥 |
| 4 | `evaluation.fixtures.resolve` | `tuple[Fixture \| None, str \| None]` | **该改**：样本 ↔ 陈旧原因互斥 |
| 5 | `sub_agent._value_to_approval` | `tuple[str, dict, str]` | **该改（复用）**：主侧已有 `ApprovalRequest`（`app/platform/turn.py` 就在用），worker 侧却自己拼三元组 |
| 6 | `images.attach_images_to_payload` | `tuple[list, list[ImageRef]]` | 成对（请求体副本 + 记账）→ 值对象 |
| 7 | `CompactNode._plan` | `tuple[list, list[str], dict]` | **三元组** → 值对象（将来搬 `compact.py` 时顺手） |
| 8 | `CompactNode._tm_status` | `tuple[str, str]` | tag + 附注 → 值对象 |
| 9 | `CompactNode._new_note_ref` | `tuple[str, int]` | ref + 序号（最小） |
| 10 | `skills.{_split_frontmatter, _read_skill_md}` | `tuple[dict, str]` | frontmatter + 正文（两个函数同形，一起改） |
| 11 | `runtime.build_session_runtime` | `(connection, step, mailbox)`，**且无返回注解** | 三个运行时句柄 → 值对象（形状现在只活在 docstring 里） |
| 12 | `turn._race` / `loop._read_user_line` | `tuple[int, value]` | **刻意例外**：select 原语（编号 + 值），§4.5 已写明 |
| 13 | `evaluation/checks.py` 各 `check` | `tuple[bool, str]` | 与 #2 同形，一起统一 |
| 14 | 编排工具 state 切片 / `approvals._record_to_value` 的 UI 载荷 | `dict` | **刻意例外**：LangGraph 合并语义 / 跨边界协议形状，§4.5 已写明 |

**分批计划（用户 2026-09-22 决定：先记录，暂不动代码）**：

1. **第一批 = 互斥载荷那 5 处（#1–#5）**——最伤：读的人得靠顺序猜哪个是错误；#5 是**复用已有值对象**、
   几乎零成本。值对象落点：`AskCheck` / `ProbeOutcome` 这类只服务生产模块的留生产者模块
   （`frozen dataclass`）；worker 侧直接用 `app/schema` 的 `ApprovalRequest`（`TypedDict`，跨进程形状）。
2. **第二批 = 成组/三元组（#6–#11、#13）**——纯清爽化；#7 与 #11 分别顺手做（前者等 `compact.py`、
   后者补返回注解时就做）。
3. **#12 / #14 只写明例外**（§4.5 已写），不改。

**相关背景**：`docs/ARCHITECTURE.md` §4.5（判据与例外）、`app/schema/approval_schema.py`
（`ApprovalRequest` / `Decision` —— 值对象与"用结构体表达互斥"的既有范例）。

## [ ] 配置的归属：`config.py` 收什么（2026-09-22 审计）

**判据**（已写进 `docs/ARCHITECTURE.md` §4.3）：`config.py`（`Settings`）收"**随环境 / 部署 / 人变，
且改它不该动代码**"的值——凭证、端点、模型名、开关、技能白名单。凡"改了要重审行为、要跟测试与
文档"的，都是**口径**（预算 / 上限 / 超时 / 词法 / 协议标记），留代码 + 构造参量缝。
**子进程读 `os.environ` 是运输、不是配置**：真值在 `get_settings()`，由 host 经
`stdio_connection(extra_env)` / `skill_env` 注入。

**实测分布**（2026-09-22 全仓 grep `os.environ`）：

| 读取点 | 性质 | 判定 |
| --- | --- | --- |
| `app/platform/approvals.py:280` 读 `AGENT_INBOX_PORT` | **部署配置**（用户可换端口） | **缺口一，见下** |
| `app/platform/mcp.py::_inbox_env` 读 `AGENT_INBOX_URL`（透传给 dispatch server） | 运行时**握手值**（收件箱起来后主侧写回 env） | 不是配置；读取点重复 3 处（+`sub_agent` / `dispatch`），可收口，登记即可 |
| `mcp_service/file_io.py`（import 期）+ `sub_agent` + `dispatch` 读 `WORKSPACE_PATH` | 子进程启动配置 / 图构造参量 | 不是配置（刻意不走 `.env`）；但**四处"取 env + resolve + 存在性校验 + 同文案报错"是真重复**（见本文件「减法审计遗留」条） |
| `mcp_service/web_search.py` / `skills/github/server.py` 读 `TAVILY_API_KEY` / `GITHUB_TOKEN` | 子进程侧运输 | ✓ 正确形态（值由 config 取出、经 env 注入） |
| `.env.example` 里的 `LANGCHAIN_*` / `LANGSMITH_*` | 第三方库自己读 `os.environ` | **写进 `.env` 不生效**，见缺口二 |

**缺口一：`AGENT_INBOX_PORT` 的承诺未兑现（真 bug 级）。—— ✅ 已修（2026-09-22）** `CLAUDE.md` 与 `docs/MULTI_AGENT.md`
都写着"主侧可 `AGENT_INBOX_PORT` 覆盖"，但它读的是 `os.environ`——而 `pydantic-settings` 读 `.env`
**不会**把值注入 `os.environ`（这条坑就写在 `config.py` 自己的 docstring 里）。**后果：用户按惯例
写进 `.env` 的端口被静默忽略，只有 shell 里 export 才生效。**
**修法（已落地）**：`AGENT_INBOX_PORT: int = DEFAULT_INBOX_PORT` 进 `Settings`（可选键、带默认——
默认值仍取自 `schema` 那份共用常量，主侧/worker 不会漂）；`ApprovalInboxServer` 改从 `get_settings()`
取，且**显式给了端口就不读配置**（保持 `ApprovalInboxServer(port=0)` 这类调用不需要 .env）。
走 Settings 后 **`.env` 与 shell env 都生效**（shell env 优先级更高）。
**实测两条**（2026-09-22）：① `Settings` 里 `int` 键写空值（`AGENT_INBOX_PORT=`）→ **ValidationError**，
所以 `.env.example` 里这一行是**注释掉的**（要用才取消注释），不是留空；② `.env` 里的键确实
**不会**进 `os.environ`（`CHAT_MODEL_NAME` / `TAVILY_API_KEY` / `LANGSMITH_PROJECT` 实测皆 False）。
**测试**：`tests/test_approval_inbox.py` 两条——`Settings.model_fields["AGENT_INBOX_PORT"].default`
钉"默认值 = 共用常量"（不实例化 Settings，该文件仍无需 .env）、`test_port_comes_from_settings_not_os_environ`
钉"真值只有 Settings 一个出口"（打桩 `get_settings`，并断言 `os.environ` 同名键不再被读）。

**缺口二：`LANGCHAIN_*` / `LANGSMITH_*` 写进 `.env` 同样不生效**——同一根因。两条路：
(a) 文档写明"**由 `os.environ` 读的键（`AGENT_*`、第三方库键）只认 shell env**"（最小代价）；
(b) 入口用 `dotenv_values` 把 `.env` 一次性灌进 `os.environ`（能一并修好所有第三方键；
代价是改变"哪些键在哪里可见"的心智模型——子进程 env 是替换制，不受影响）。
**2026-09-22 走了 (a) 的一半**：`README` 的 `.env` 小节、`.env.example` 的 LangSmith 段、
`CLAUDE.md` 都写明了这条（哪些键只认 shell env）；**(b) 仍未拍板**——真要做就是在 `app/main.py`
入口灌一次，届时把这段升级成机制。

**缺口三：`GITHUB_API_URL`（指向 GitHub Enterprise）不是漏了，是已知未做**——
`docs/SKILL_DESIGN.md` §13.5 末记着：技能 env 机制只有"声明了就必须非空"这一种，声明它反而会让
默认（官方 API）路径加载不上。要让用户真能指 GHE，得先给机制加"**可选键 + 默认值**"。在此之前它
是**测试缝**（E2E 用它把请求指到本地桩）。

**相关背景**：`docs/ARCHITECTURE.md` §4.3（判据）、`app/config.py`（`Settings` + `SKILL_ENV_WHITELIST`）、
本文件「减法审计遗留」条（`WORKSPACE_PATH` 的四处重复）。

## [ ] 运行中转向：用户消息队列（输入不等整轮结束才递进）（2026-09-19）

**场景**：用户给 agent 派了任务、指了 A 方向，agent 已开跑；跑到一半用户发现 A 不合意图、
想改 B——今天的现实是：**输入必须等整个图路由跑完才被写入**（现状见下），agent 把 A 一路
做完，仓库被写了一堆要删/回滚的东西，token 白烧一整段。目标 = 运行中敲的消息**进队列、
在最近的安全点递进图**，模型带着"用户改向 B"继续，而不是等整轮结束后才看到。

**现状**（三段都核过，2026-09-19）：

- 前端**已经在收**：stdin 单 reader 线程 + pump（`app/tui/input.py`）整轮不停，运行中敲的行
  就躺在 `_out_q` 里；面板出现前的预输入由 `TerminalUI._hold_pretyped` 挪进 `_held`
  （2026-09-15 审计），留给**下一个** turn——所以捕获这半已经在了，缺的是消费。
- 基座**不消费**：turn 在跑时主循环只 `_race(turn_done.wait, inbox.new_pending.wait)`
  （`app/platform/loop.py::run` 第 2 步），不读用户输入 → 这些行必然等到 turn 结束才被
  `read_line` 取走、起新 turn。
- 图内**没有承接通道**：state 没有"运行中插入的用户消息"切片，`LLMNode` 没有对应注入点；
  interrupt 的 resume 载荷只运闸门决定（审批的 `approved` / 提问的回答）。

**要点（2026-09-19 对齐后细化；库行为已按 langgraph 1.1.10 探针实测——一次性脚本
`tmp/probe_astream.py`（tmp/ 不入库），结论以本条为准）**：

- **封闭性只对入向成立**：出向库有现成通道、我们没用——`drive_turn` 现在是裸
  `await compiled.ainvoke` 拿终态（`app/platform/turn.py`），每个 superstep 发生了什么一概
  不可见。换 `astream(stream_mode="updates"|"custom")` 即可逐拍观察；`drive_turn` 的
  park/resume 循环形状不变，消费方式从"等终态"改"逐拍"。**这半与消息队列独立，可先行**——
  顺带为流式显示与「长回合止损」的活性观察铺路。**已实测**：interrupt 处流出
  `{"__interrupt__": (Interrupt(value,…),)}` 一拍后流**正常耗尽**（无异常、无残余拍）；
  resume = `astream(Command(resume=…), cfg)` 重进，续流含被中断节点**整节点重跑**的一拍 +
  余下节点，终态正确。
- **入向选型，定 (c)；形状定「转向 = 提前收口 + 新起一轮」（用户 2026-09-19 拍板，取代
  早先"消息并进本轮"的排水口形状）**：
  - (a) **interrupt/resume 顺带捎**（resume 载荷扩 user_messages）：只覆盖有闸门的轮，
    且把转向混进 `Decision` 值对象不干净——不选；
  - (b) **`update_state` 写运行中线程**：**已实测否决**——调用本身成功（返回新
    checkpoint_id），但注入**静默丢失**（终态不含它）：运行中的 superstep commit 按自己的
    内存视图落盘，外部写被穿透丢弃、全程无报错。最坏的失败形态，不选；
  - (c) **进程内信箱 + 提前收口（定案）**：langgraph 封闭的是 **API 层的 state 写入**，
    不是进程内对象——图与基座同进程同事件循环（探针 4 已验：运行中向图外队列投递，节点
    在 superstep 边界读得到）。**消息内容从头到尾不进图**：信箱由基座持有，图侧只读布尔
    信号；转向 = 当前轮在下一个 llm 边界提前收口，基座把信箱里的行当**下一轮输入**新起
    turn——模型经完全标准的输入路径（HumanMessage）看到转向，CONTEXT_ENGINEERING 的消息
    流转一律不动。收口时 `compact_node` 照常参与（超预算才动手），新 turn 轻装开局。
  - 备查：cancel + 从 checkpoint 续跑——None 输入的语义**已实测**：对停在 interrupt 的
    线程，`ainvoke/astream(None)` **原样回报 `__interrupt__`**（不推进、不吞闸门）；对已
    完成的线程是 no-op（0 拍 / 返回终态）。即 None 输入**不能当续跑通道**，续跑必须
    `Command(resume=…)`；"cancel 后带新消息重入"这条路本身仍悬而未测，留作备选。
- **图侧改动收敛到三点（比"三条条件边"的草图更小）**：
  1. `state.is_inject: bool`；`build_turn_state` 每轮置 False；
  2. **唯一 peek 点 = `llm_node` 入口**：信箱有货 → **跳过本次模型调用**、返回
     `{"is_inject": True}`（不产生新 AIMessage）→ 既有 `route_after_llm` 看到
     "末条非 AIMessage(tool_calls)" → 走既有 compact/END。**不需要新增任何条件边**——
     "模型这拍没说话"与"给了最终答复"在路由眼里同形，自然收口；
  3. 信箱接线：`runtime.py::build_session_runtime` 造信箱，图工厂与基座各持一份引用
     （探针 4 同款）；`llm_node` 加可选 mailbox 参量。
- **草图里的地雷（为什么不能在 llm 出口收口）**：若按"llm_node 条件边 → compact"，模型
  恰在本拍吐了 tool_calls 时会带着**悬空调用**进 END——历史里 AIMessage(tool_calls) 没有
  兑现的 ToolMessage，下一轮直接 API 400。入口**跳过**（而非出口拦截）天然没这个问题：
  跳过时不产生新调用，旧调用都已兑现。
- **转向时延上界 = 在途一批**：信号只在 llm 入口被读——正在跑的批次照常执行（已过人批）、
  orchestrate 切片照常合并，然后才收口。**刻意不做"在途批次入口丢弃"**：同样的悬空问题，
  要做就得 seal（给未兑现调用补 `[已收口未执行]` 回执，同 approval_denied 回灌的机制）——
  留作后续优化；默认"执行完再收口"，用户本就可以在面板上拒绝，拒绝回灌路径是现成的。
- **基座两端职责**：主循环第 2 步 `_race` 加第三路 `ui.read_line`——行在 `turn_done` 前
  到 → 投信箱 + 前端回执（"已收到，本回合将在边界收口"；`_held` 那句"已留给下一个回合"
  的提示语跟着分化）；turn 结束 → 信箱非空则取**第一行**当下一轮输入（其余留队，与既有
  "预输入一行一 turn"语义一致）。内容只在基座侧流动、图侧 peek 不消费——同一行既触发
  收口又成为下一轮输入，不丢不重。
- **收口回合的呈现（别漏）**：被收口的 turn 没有模型答复，`messages[-1]` 是 ToolMessage
  ——`loop.py` 现状会把它当 `TurnFinished` 正文打出来。基座须看 `is_inject` 翻成专门提示
  （"回合已按你的新消息收口"），别把工具输出当答复。
- **worker 是 no-op**：`llm_node` 被子图共用 → mailbox 是可选参量、worker 恒传 None；
  不设标记就永不触发（worker 图无 compact 也无妨）。
- **重放语义写明**：`llm_node` 入口读了图外可变状态（布尔），checkpoint 重放不再纯——
  本仓库不用 time travel，接受，注释留痕。
- **先验**：LangGraph Platform（server）对"run 运行中来新输入"的官方策略是
  `multitaskStrategy: enqueue/interrupt/rollback/reject`——需求普适的佐证；但 server 只能在
  **run 边界**处置（run 是它的黑盒），本设计在 **superstep 边界**（llm 入口）收口，
  粒度更细一档。
- **验证路径**：机制层假模型 + 真图（沿 `test_command_review_e2e` 的路子）：信箱有货时
  llm 跳过（该拍零模型调用）、在途批次已执行、`is_inject` 落终态、路由落 END；基座层沿
  `test_platform_loop`：运行中投行 → turn 提前结束 → 下一轮输入 = 该行。层 A 回放对转向
  是瞎的（回放模型不读输入），若动提示词照旧 `--live`。
- **范围**：只做主 agent——worker 短命、无用户交互，结构性不涉及。

**入向已落地（2026-09-19，出向 astream 化仍未做，本条目保持开着）**：

- **图侧三点**：`state.is_inject`；`LLMNode` 入口 peek（`__call__` 第一条语句——收口拍零模型
  调用且**零读盘**，画像/记忆/技能都不碰）；mailbox 经 `get_main_agent_graph(mailbox=)` 参量
  接线，`build_session_runtime` 造信箱、基座与图各持一份引用（`UserMailbox` 落在
  `app/platform/runtime.py`：基座 put/take_first/clear，图侧只有 has_pending 的 peek 权）。
- **基座三件**：`_race` 第三路收行（**EOF 即撤下输入臂**——None 秒回会把 race 变忙轮询）；
  空闲分支**信箱优先消费**（`take_first` 当下一轮输入、其余留队，命令解析与退出词判定与
  正常输入共用同一段）；收口回合翻成 `Notice("回合已按你的新消息收口")`，不把 ToolMessage
  当 `TurnFinished` 正文。运行中回执措辞"**已收到，将在最近边界生效**"——刻意不承诺收口
  （最后一次 peek 之后到的行只会成为下一条消息）。
- **`build_turn_state` 每轮显式置 `is_inject: False`**：那是**上一轮**留在 checkpoint 里的
  收口痕迹，不归零下一轮一进来就又跳过。
- **实测钉住的刻意行为**：连敲 N 行 = 前 N-1 轮空转收口、只有最后一行真进模型（所有行都在
  历史里，模型一并看到）；在途 `ask_user` 照常弹出、回答照常兑现，然后才收口。
- **切会话**：旧信箱随运行体作废；正常流程下到不了"有残留"（空闲分支优先消费），防御分支
  丢弃残留行并 `Notice` 明说数量。
- **验证**：`tests/test_steering.py`（3 例真图：零模型调用+零读盘 / 消息尾部形状=ToolMessage /
  在途 ask_user 序列 / mailbox=None 行为不变）+ `tests/test_platform_loop.py` 新 3 例
  （race 投递与续轮 / 两行连锁空转 / 切会话丢弃）。全量 **562 passed / 7 skipped**；
  层 A 回放 **8/8、退出码 0**。

**相关背景**：`app/tui/ui.py` 的 `_held`/`_hold_pretyped`（预输入那半已经在了）、
`app/platform/turn.py::drive_turn`（astream 化 + park/resume 都在这里改）、
docs/CONTEXT_ENGINEERING.md（消息流转改动前必读）、docs/ARCHITECTURE.md §4（图内 vs 基座）、
文末「相关但未立项」的**长回合止损**（取消 = 扔掉整轮，队列 = 带着上下文转向，互补不互替）。

## [ ] 运行中 compact：工具循环中间的上下文收口

**问题**（2026-09-07 长回合实测）：主 agent 一轮里大量工具调用（如一次 jupyter 审计
~67 次、20 分钟）时，历史里的**读文件/检索结果作为 ToolMessage 每轮原样重发**，上下文越滚
越大、单轮越来越慢；`compact_node` 却只在**回合末**（`llm` 不再调工具 → `route_after_llm`
走 compact/END）才可能折叠，**工具循环中间从不触发** → 长跑无中间收口。

**目标态**：历史 content 超预算（复用 `needs_compact` / `CompactNode._plan` 口径）时，
在工具循环中途也折叠"更早轮次、已被消费的完整工具块"，只保留当前正在进行的轮次。

**要点（实现前须钉死）**：
- 只能折**最后一个 HumanMessage 之前的旧轮完整块**（含 AIMessage(tool_calls) + 兑现它的
  ToolMessages），**正在推进的当前工具链不能折**——判据 `CompactNode._plan` 已有；
- 折叠落点仍是 `SystemMessage(摘要, id=…)` 原位替换 + `RemoveMessage`，不尾追加；
- 触发点候选：queue/review 之前或每 N 轮检查一次；须避免与 interrupt 审批的 checkpoint
  交互互相干扰（折叠发生在哪一步、会不会打断挂起 run）；
- 折叠后的摘要系统消息会再注入每轮，注意别把"当前轮"误判成旧轮折掉。
- 相关背景：[[docs/CONTEXT_ENGINEERING]]（只流结论 / notes / 折叠目标态）、
  根目录 CLAUDE.md 上下文工程一节。

## [ ] Reflect 节点：模型拟动手前，先让另一个（或同一个）模型审一遍

**动机**（2026-09-10 记）：README 路线图里的 "reflect-before-act"——在模型拟调用工具、**进入人工
审批之前**，用另一个模型审一次"这个动作对不对"，被否决就把理由回灌主模型重想。README 已定
"按实测**单次反思收益已足够，先只做一次**"。

**归位**：按 `docs/ARCHITECTURE.md` §4 的判据，这是**图内的一条边/一个节点**（一条 run 内部的
迭代），不是基座事件 → 落 `app/agent/nodes.py::ReflectNode`，**不进 `app/platform`**。

**要点（实现前须钉死）**：
- **插入点**：`llm_node →(有 tool_calls)→ reflect_node → queue_node`。放在 queue **之前**，被否决
  就直接回主模型重想，**不惊动审批面板与 broker**。
- **触发条件**：① 开关（`/reflect on|off` 之类）落 **state**（会话级运行态），别为此重编译图；
  ② 更值得的一条——**只对"需审批那一类调用"反思**（写/命令/联网），免审的读操作不值得花一次调用。
- **意见怎么回灌**：反思没有 tool_call 可兑现，**别塞成 ToolMessage**（读起来像"助手自己说的"）；
  建议走 state 的 `reflect_feedback` 字段，由 `LLMNode` 下一轮拼成 `# 审计意见（本轮）`——
  与 `current_plan` 的注入口径一致。
- **次数上限**：`reflect_count` 每轮重置、**最多 1 次**，否则"反思→重想→再反思"会打转。
- **模型选择**：用另一个模型（不同视角）还是复用 `CHAT_*`？选前者要加一组**可选**配置键，
  注意 `app/config.py`"键不可缺、值可空"的约定。
- **纪律**：反思是**机器审计**，**不能替代审批闸门**（否则等于用模型替人放行）——两条正交、可叠加。
- 相关背景：README 路线图"反思节点"条、`docs/ARCHITECTURE.md` §4（图内 vs 基座的判据）、
  `ReviewNode` 的 `log` 开关（节点逐条日志的既有做法）。

## [ ] 工具面缺口：一次真实运行的实测（2026-09-13）

**样本**：一个会话、3 轮用户请求（克隆 Luna-Agent → uv 补环境 → 查/拉分支），共 **55 次**工具调用。
下面的 `git_*` 计数是**当时的形态**——那批工具 2026-09-15 已删，保留原样是为了让结论可复核。

| 工具 | 次数 |
| --- | --- |
| `run_command` | **30（55%）** |
| `read_file` | 7 |
| `update_plan_step` | 6 |
| `list_dir` / `write_memory` | 各 2 |
| `get_directory_tree` / `create_plan` / `clear_plan` / `create_file` | 各 1 |
| `git_clone` / `git_branches` / `git_status` / `git_switch` | 各 1（**clone 与 switch 都失败过**） |
| `github_*`（整个技能） | **0** |

**三条结构性原因**（不是"模型不听话"）：

1. **第一步就脱轨**：本机 github 被 Watt Toolkit 劫持到 127.0.0.1，git 的 schannel 证书吊销检查
   失败 → `git_clone` 失败；绕过要 `-c http.sslVerify=false`，而**那个开关只能敲在 shell 里**（工具
   不收 git 配置）→ 此后所有 git 操作都走 shell（路径依赖）。
2. **工具参数面太窄**：`--depth` / `--unshallow` / `--track` / `-B` / `ls-remote` / `for-each-ref` /
   `reflog` / `diff --cached` / `config` —— 一个都不支持。该会话第三轮的 12 次 `run_command`
   **全部**是这类。
3. **github skill 结构性地插不上手**：它只有 7 个**只读平台**工具，没有克隆/分支/拉取——那些在核心
   `git_*` 里，而 `git_*` 又不支持上面那些参数。**模型没加载它是对的**（"克隆+补环境"本来就不是
   GitHub 平台操作）。（**数字是当日快照**：七天后的 2026-09-14 三期已长到 **14 只读 + 3 写**，
   见 `docs/SKILL_DESIGN.md` §13.10；"没有克隆/分支/拉取"这条**结论不变**——那族至今不在技能里。）

**收敛清单（原按日志频次排序）—— ❌ 已整条作废（2026-09-15）**：

下面这几条都是"给 git 原子工具补参数"，而 **git 专用工具已于 2026-09-15 整体删除**（`mcp_service/git.py`
连同 12 个工具；git 一律走 `run_command`）。这些诉求由 shell 天然满足：`--depth` / `--track` /
`ls-remote` / `--unshallow` 直接就是命令行。**不要再去给不存在的工具加参数。**
（原文留档，供理解当初的判断依据）

- ~~`git_clone` 加 `depth`（浅克隆；日志里它直接用了 `--depth 1`）；~~
- ~~`git_switch` 支持"从远端建跟踪分支"（`--track` / `-B` / `-u`）；~~
- ~~**远端分支清单**（`ls-remote` 语义）独立成只读工具、或给 `git_branches` 一个开关；~~
- ~~`git_fetch` 支持 refspec / `--unshallow`。~~

**不要做**：给 `git_clone` 透传 `-c`——那等于给模型一个正式"关掉证书校验"的开关（MITM 面）。
（**该顾虑已由命令白名单承接**：`git -c …` 一律不判免审，见 `app/agent/command_policy.json`。）

**审批摩擦（同一份实测）**：全程 **33 次人工 y**，其中 **30 次是 shell 命令**。"一次路由调用了多个
工具"的实际开销是**人的**：图里 `review_node` 逐条 drain（`review_node → review_node`，排空才去
执行），**模型每轮只调一次**，所以 N 个 tool_call ≠ N 次模型调用，但 = N 次审批（需审的那些）+ N 条
ToolMessage 进 history（token，由 compact 折叠兜底）。实测里还有**模型自己的重复劳动**：一条复合
命令（`uv --version; python --version; git --version`）批准执行之后，它又把三条**分别重跑了一遍**
——4 次审批换一份信息。

## [ ] 工作区语义：克隆下来的项目落在工作区**子目录**里（2026-09-13 实测）

**现象**：用户新建一个空目录当工作区、让 agent 克隆 + 补环境，落点是 `工作区/Luna-Agent/`——于是
**工作区 ≠ 项目根**。后果全在"以工作区为界"的东西上：项目画像 `AGENT.md` 写在外层；长期记忆按外层
分（同一外层下克隆的第二个项目会**共享一份记忆**）；终端每条命令都得自己 `cd`（实测 30 次
`run_command` 里 **19 次带 `cd Luna-Agent && …`**，其中还有用 `&` 而非 `&&` 的——cmd 里 `&` 是
**无条件**分隔符，`cd` 失败也照跑，`checkout -B`/`reset` 这类写操作会落到错目录）。

**待拍板（两条，二选一或都要）**：

- ~~**① 克隆落点**：`git_clone` 的 `dest` 改成**必填**~~ —— **已作废（2026-09-15）**：`git_clone`
  随 `mcp_service/git.py` 一起删了，克隆现在走 `run_command`，**工具面已不存在"落点参数"可改**。
  原来的两个诉求**都没接住**（2026-09-17 核）：审批面板看得见落点这件事，随工具一起消失
  （`run_command` 的命令行本来就整条可见，勉强算兑现）；而"工作区空就别套一层子目录"这条
  **提示词里没有**——`prompt.py:130` 只有一句"**clone 的目标目录要落在工作区内**"，管的是
  "别越界"，对"落根还是落子目录"**没有口径**。所以 ① 现在要么在 prompt 里补一句，
  要么只能靠 ② 的机制（后者不依赖模型自觉）。
- **② 更根本**：空工作区**默认**落根（零操作，但面板看不到落点）；或加 `/workspace <path>` **切换
  工作区**（工作区是"项目容器"时用它；要动基座：关旧运行体 → 换 `ws_key` → 重建图与库），后者顺带
  解决"TUI 起在了错的目录"。**这条是现在唯一还成立的解法**——克隆走 shell 之后，②不再有替代品。

**副作用（记在案）**：工作区=项目根时，`/init` 写的 `AGENT.md` 会落进**克隆下来的那个仓库**，成为
它的未跟踪文件。这跟"在一个项目里起 agent"的常态一样（想不提交就 gitignore），只是值得知道。

## [ ] 长期记忆分层化：设备环境层 → 用户层 → 工作区层

**动机**（2026-09-16 记）：agent 目前**没有任何地方知道"这台机器上有什么"**。要判断"能不能直接跑
`uv sync`""本机有没有 java""该敲 `python` 还是 `py`"，它只能**现场探**——而每一次探测都是一条
`run_command`，要么过闸门、要么走免审白名单，哪条路都有成本。这正是「工具面缺口」那次实测里
`run_command` 占 55 次调用中 30 次的一部分：真实会话里出现过"**一条复合命令
（`uv --version; python --version; git --version`）批准执行之后，模型又把三条分别重跑了一遍**
——4 次审批换一份信息"，以及用户侧的体感"克隆下来项目、**补环境老是补不全**"。

而这些事实的特点是：**慢变**（几个月不动）、**跨工作区通用**（同一台机器上每个工作区都成立）、
**体积极小**（十几行）。今天的长期记忆却只有**一层**——`resource/<ws_key>/memory/memory.md`，
按工作区私有。于是同一份"本机 python 3.13.9、uv 是 `/usr/sbin/uv`（别 `uv run`）、没有 java"
会在**每个新工作区里被重新学一遍**，或者干脆没学会、每次再探一遍。

**目标态**：长期记忆分成若干层，**越靠上的层越通用、越稳定、注入优先级越高**；最后一层仍是
工作区的项目记忆（现状不变）。

```
① 设备/环境层   本机 python/uv/git/node/java 的版本与"有没有"、OS/shell、包管理器、
                代理与网络约束（"本机 github 被 Watt Toolkit 劫持到 127.0.0.1"就是这类事实）
② 用户层(待拍)   跨工作区的个人偏好：语言、提交习惯、输出风格
③ 工作区层      resource/<ws_key>/memory/memory.md   ← 现状，不动
④ 项目仓库层    工作区根 AGENT.md                     ← 现状；与 ③ 是**不同的轴**（随仓库走 vs 私有）
```

**待拍（实现前须钉死）**：

1. **中间层要不要**：①③ 是点名的两端。② 用户层要不要单列？今天的 `write_memory` 把
   `type=user-preference` 写在**工作区**记忆里，于是"注释用中文"这种偏好在每个工作区各写一份。
   **倾向要**，但得先定它和 ① 的分界——"本机没有 java"是环境事实，"用 python 不用 java"是用户
   偏好，可两者常常同源，别切成两份自相矛盾的东西。
2. **`AGENT.md` 算不算一层**：它是**另一个轴**（进仓库、随真值收敛）而非另一层（私有、跨会话）。
   文档里要写明这个区别，别让"分层"把它也吞进去。
3. **谁写 ①**（最关键，两条路差别很大）：
   - (a) **host 侧自动探测并播种**：像 `ensure_memory_template` 那样，在建会话时探一次
     （`python --version` / `git --version` / 各工具在不在），写进设备层文件；环境指纹
     （主机名 + 关键版本）变了就刷新。**优点**：零模型成本、零审批、永远是最新事实，正对
     "别让 agent 再探一遍"这个动机。**代价**：多一张"探什么"的表 + 起子进程，而探测命令在本仓库
     有前科（terminal 的 stdin 继承坑，见本文那条 `[x]`）。
   - (b) **agent 探到后自己写**：加 `write_memory(scope="machine", …)`。**优点**：不动 host、
     探测范围由模型按需决定。**代价**：探那一次照样过闸门（动机只兑现一半），且得先"想到要记"。
   - (c) **两者都要**：host 探**客观版本**，agent 记**主观结论**（"这个仓库的测试得用
     `-m pytest`"这类留在 ③）。
4. **注入预算怎么分**：现在 `MEMORY_INJECT_CAP = 8000` 是**单份**上限。多层后是"每层各自 cap"，
   还是"合计一个 cap、按层优先级分配"？**倾向后者**：越靠上的层越该**整份**注入（它小且通用），
   ③ 才是需要"从最新往前装、装不下退化成编号清单"的那一层。等于把现有的截断策略**按层分别定义**。
5. **工具面怎么指定层**：`write_memory` / `read_memory` 现在靠 `InjectedWorkspace` 定位——**模型
   碰不到路径**，这是既有的安全不变量。分层后模型要能说"写哪层"：加 `scope` 参数（枚举，不是
   路径，不破坏那条不变量），还是"默认 ③、① 另有工具"？`read_memory` 取回时要不要标出条目来自
   哪一层？
6. **陈旧与失效**（分层真正的难点）：越靠上的层影响面越大——**一句错的"本机有 java"会污染所有
   工作区**。设备层不能只靠"人记得改"：环境指纹写进层头、不匹配就自动作废重探？还是只标
   "记于 2026-09-16"、注入时提示模型"这是快照，可能过期"？**这条决定了 ① 能不能自动写**：
   (a) 之所以敢自动写，前提就是有指纹兜底。
7. **落点**：① 放哪？`resource/` 现在的语义是"按工作区分区"，设备层**不属于任何工作区**——
   是 `resource/_machine/memory.md` 这类兄弟目录，还是挪出 `resource/`（如项目根 `.theta/`）？
   定它时连带定 ② 的落点。
8. **worker 吃不吃 ①**：现状 worker **不注入任何记忆**（`inject_session_context=False`）。可 ①
   对 worker 恰好最有用——它要跑命令、又只有自己那份上下文（还看不到主 agent 的对话）。给不给？
   （worker 的隔离是**设计原则**不是图省事，所以要显式拍板。）

**相关背景**：`docs/LONG_TERM_MEMORY.md`（§1 三条通道的边界表、§3 单文件格式与编号规则、§4 注入
与 cap、§5 工具与路径安全不变量、§8 分期）——**注意 §8 的 Phase C 把"跨工作区共享记忆"与
"分层摘要"列为"明确不做、后续再议"，本条正是把那条提前**；动手前先读该文，别与它的单文件格式 /
`m1,m2,…` 编号 / "只存工作区里重取不到的结论"这三条既有纪律打架。代码落点：`app/resource/memory.py`
（读写单点）、`app/resource/paths.py`（落点单点）、`app/agent/nodes.py::LLMNode`（注入点，
`memory_block` 在 `app/resource/memory.py`、`agent_md_block` 在 `app/resource/profile.py`）、
`app/agent/tools.py`（`write_memory`/`read_memory` 与
`InjectedWorkspace`）。证据见本文「工具面缺口」与「工作区语义」两条。

## [~] 技能（skill）：按需加载的"领域包"——**三期均已落地**（见 docs/SKILL_DESIGN.md）

> **设计已独立成文** → [[docs/SKILL_DESIGN]]。本条只留指针与当前状态，**别再往这里堆内容**。
> ⚠️ 下面按日期追加，**读最新一条**（`docs/README.md` 的写作约定）——中间那几段标着"当前状态
> （2026-09-12）"的是当日快照，其中"`skills/github/` 没有 `server.py`、当前是知识型"**已被三期取代**。

一句话：把某个领域要用的**工具**和这个领域的**纪律**打成一包、按需加载。典型 = **GitHub**（推送 /
PR / issue / 搜索 / 不克隆就读远端代码，外加"先开分支""master 不能随便提"等约束）。差异化在
**约束三分法**——软约束走 `SKILL.md` 正文（host 读取、注入系统提示）、硬闸门走 `tool.json`、
结构性拒绝写死在工具里。

**当前状态（2026-09-12）**：**两型共用的那条通道已落地**——`app/resource/skills.py` +
`get_skill`/`drop_skill` + state 的 `loaded_skills` + `LLMNode` 注入（落地清单见
SKILL_DESIGN §11）。`skills/github/` 没有 `server.py`，故**当前自动是知识型**、可加载。
**未做**：能力型（`server.py` 生命周期、会话级注册表 + 动态 `bind_tools`、idle 回收、
env 声明与白名单转发）——那是 §3.3/§3.4 那套，也是最重的一块。

**前置里程碑已落地（2026-09-12）**：SKILL_DESIGN **§12「MCP 运行体常驻化」**——MCP server 从
"每次调用重起的临时脚本"变成"会话期常驻的后台服务"（每 (工作区, 会话, server) 一个 owner task）。
顺带修好了 `mcp_service/terminal.py` 的跨调用进程管理（`start_process` 起的进程以前下次调用就认不到）。
承重约束（anyio 的 cancel scope 与 Task 绑定 × langgraph 每节点开新 Task）写在 §12.2，**改那套机制前必读**。

**二期已落地（只读子集，2026-09-12）**：SKILL_DESIGN **§13 能力型**——技能可以带 `server.py`，
`get_skill` 把它的工具拉进本会话、`drop_skill` 一并关掉（机制 = "技能就是按需加入的 server"，
复用 §12 的运行体）。首个能力型技能 `skills/github/` 落了 **7 个只读工具**（stdlib HTTP，不引 PyGithub）+ 一次**加载时凭证体检**（`skill.json` 的 `preflight`，替代了原 `github_auth_status` 工具）。

**三期已落地（2026-09-14，见 SKILL_DESIGN §13.10）**：`skills/github/` 从 7 个只读工具长到
**14 只读 + 3 写**（`github_pr_create` / `github_pr_review` / `github_issue_comment`，都要人批）。
越权动作在工具内**结构性拒绝**：`github_pr_review` 的 `event` 白名单不含 `APPROVE`；**"合并"连工具
都没有**（"合并权留给人"最彻底的落点是这个动作压根不存在，与"APPROVE 被拒"同构）。同批修掉了
`server.py` 的 8 处审计问题与 `skills.py` 两处真 bug（坏编码的 `SKILL.md` 会拖垮整次扫描；目录名是
Python 关键字时被误标 `[带工具]`）。

**未做**：技能运行体的空闲回收（已定不做，只靠 `drop_skill`）；`/skills` 控制面命令。口径见 §13.9。

**与本文其它条目的关系**：SKILL_DESIGN §6 与上面的「反思节点」打通——skill fork 到隔离子 agent 执行，
本身即一次结构性反思，且 `mcp_service/sub_agent.py` 那套骨架已经在了；但它卡在
`docs/MULTI_AGENT.md` §6「子 agent 写权限下放」这个里程碑上。

## [ ] 图像输入：@路径 附图通道已落地；工具返回图仍无通路（2026-09-14）

**用户通路已落地（本期）**：消息里写 `@路径`（相对工作区或绝对路径；带空格用 `@"…"`），
`LLMNode` 构造请求体副本时把图**临时**附上——**base64 从不进 messages/checkpoint**：state 里的
消息始终是带 @路径 的纯文本（本身就是"看过哪张图"的日志），轮末没有"剔除"这回事。机制收在
`app/resource/images.py::attach_images_to_payload`（解析/读盘/拼接/记账；2026-09-21 从 `LLMNode`
的静态方法剥到资源层，节点只剩"这一拍要不要附图"）；
记账 = state 的 `attached_images`（`ImageRef` 元数据，单调追加）→ 同轮不重发（**首次-only**，
重启后重读盘即得）。宽进策略（议定）：扩展名不像图片的 @token 是普通文本、原样放行；失败不炸
turn，处置（未找到 / 过大>5MB / 超 4 张 / 读取失败）写进正文让模型转告用户。worker
`attach_images=False`：dispatch prompt 不认 @，杜绝模型借 worker 附带本地文件（结构性排除）。
机制测试 `tests/test_image_attach.py`（13 例无网络；真调用 opt-in，见该文件尾）。

**模型层事实**（此前已通，`tests/test_vision_input.py` 固化）：

| 路径 | 工具绑定 | 结果 |
| --- | --- | --- |
| base64（LangChain 规范块 `{"type":"image","base64":…}`） | 无 | ✅ 认出一张左红右蓝的 64×64 图 |
| base64（`image_url` + data URI） | **有**（`bind_tools`） | ✅ 答对颜色 + 吐出规范 `tool_calls` |
| http URL（`image_url`） | 无 | ✅ 认出百度 logo（**厂端自己抓的图**） |

三条结论：

- **唯一前提是模型**：当日配置的 `glm-4.7-flash` 硬拒图片（`400 / code 1210 /
  messages.content.type 参数非法，取值范围 ['text']`）；实测可用的是 `glm-4.6v-flash`（base64 与
  URL 都行，**且能同时挂工具**）；免费层的 `glm-4v-flash` 只吃 URL、不吃 base64。
  **2026-09-15 复测：当前配置的 `glm-5.3-flash` 已支持视觉**（`tests/test_vision_input.py` 的
  live 用例三连过）——换模型后重跑一次，别照抄上面的历史结论。附图真调用曾持续 429（1305 访问量
  过大）——免费端点负载问题，非通道缺陷，限流缓解后跑 `-k apple` 即验。
- **库侧零障碍**：`ChatOpenAI` 原样透传 `image_url`，并把 LangChain 的规范图块**自动翻译**成
  `data:<mime>;base64,…`；`ToolMessage` 带图也被端点接受。
- **两条路各有代价**：base64 自包含（本地图直接给）但体积 +33%；URL 便宜但要求图在公网。
  用户通路选 base64（本地文件）；URL 留给公网图源。

**原勘察的拦路石，处置如下**（1/2/4 被通道设计整体绕开，3 未动）：

1. ~~预算与折叠对 base64 失控~~：base64 不进 messages，`needs_compact` 永远看不到它。
2. ~~块列表撞"按 str 消费"的老代码~~：state 消息保持 str，`TurnFinished` / `/list session` /
   `CompactNode._tm_status` 全部照旧。
3. **工具返回图的通路仍是断的（未动）**：`format_tool_result`/`_join_block_texts` 丢非文本块
   （`app/platform/tool_results.py:22-33`，**已被 `tests/test_format_tool_result.py:65-70` 钉住**），且
   `ToolResult.content: str`（`app/schema/agent_schema.py:9`）是硬墙——模型还不能把工具读到的图
   送进对话。要做时另开一期。
4. ~~一批测试会红~~：str 断言测试全部照旧（全量回归 401 passed）。

**顺带的独立缺陷已修（2026-09-14）**：`read_file` 读二进制时 `UnicodeDecodeError`（非 `OSError`
子类）曾冒到 `@guard` 被误判成 `internal_error`——现已改为字节读 + NUL 探测（`_is_binary`，
与 `search_content` 共享同一口径）+ 魔数猜类型，二进制/非 UTF-8 文本都给"它是什么 + 多大"的
可预期回执（success=True，不进错误分类）；行尾显式保持旧文本模式的 universal newlines 行为
（`\r\n`→`\n`），编辑锚定不受影响。`tests/test_file_io.py` 二进制回执一节共 5 例。

## [ ] 文档解析技能（skills/documents）：勘察完毕，暂缓（2026-09-14）

**结论**：值得做，但用户拍板 agent 提问模式（前面那条）效益更高、先做它——本条设计已收敛，
捡起即可开工，无需重新勘察。

**落点判定**：能力型技能 `skills/documents/`，不进 `mcp_service`（§8.1 并列不混入）。"不做、
让模型走 `run_command` + 临时脚本"能顶：但每次读都要审批、依赖装没装不可控、表格还原靠模型
肉眼拼、"怎么读 docx"的知识散在提示词里。技能侧的额外意义：这会是 `requirements` 通道
（`skill.json` 声明 + host `find_spec` 起进程前预检 + 未装 fail-closed 拒载）的**第一个真实消费者**
——github 是纯 stdlib，没走过这条路。

**依赖成本（与 github 技能的本质区别）**：没法纯 stdlib，主 venv 装 3–4 个纯 Python 包——
`pdfminer.six`（PDF；MIT、CJK 支持尚可）/ `openpyxl`（xlsx）/ `python-docx` / `python-pptx`。
全纯 Python、无编译依赖；§9 已决依赖进主 venv，机制零新增。

**范围（已议定）**：

- 一期 **xlsx + pdf**（编码场景最高频）：`xlsx_read`（工作表 → markdown 表格，限行列防爆炸）、
  `pdf_read`（按页 range 分段，回执带页码）；二期 docx + pptx。
- **只读不写**：生成 docx/pdf 体大频低，转换类任务用文本工具输出 markdown 就够。
- **扫描件 PDF 不做 OCR**：提取不到文本层就如实回执"疑似扫描件（无文本层）"，OCR 另立项。
- `need_review: false`（纯读本地）+ 路径过工作区沙箱（对齐 file_io）+ 损坏文件给可预期回执
  （read_file 2026-09-14 口径）。

**待拍**：真实频次（几个月遇不到一次的话，继续搁置就是对的）；命名 `documents`（倾向，一个
技能装四族、共享沙箱与渲染）还是按 pdf / office 拆。

## [ ] 减法审计遗留：低危清理项（2026-09-15）

**背景**：2026-09-15 对全仓做了一轮"减法审计"（逻辑冗余 / 死代码 / 逻辑错误 / 过时注释），
高危与中危已就地修掉（见下"已清"），剩下这批是**不承重的零碎**——每条都确证过行号，但单独
不值得开一轮，做别的改动顺路带上即可。**不必再全仓扫一遍**：已清的部分不会复现，下面这些
是**仅剩**的清单。

**已清（同批，供对照，避免重复排查）**：

- 高危 4 条：`@图片` 引号分支 KeyError（`Path(raw).suffix` 反查 mime，用户写 `@"x.png "` 即崩
  整轮）、`process_read` 吞掉已结束进程的输出（先 `read_new` 消耗再 `drain`）、`edit_file` 读
  GBK/二进制被误判 `internal_error`、技能体检漏捕**读阶段**的裸 `OSError`（逃出 `get_skill`）；
- 中危 7 条：`glob` 输出基准与 `read_file` 不一致、`copy_path(recursive=False)` 对目录假成功、
  `get_directory_tree` 的符号链接逃逸、`/list session` 把 UTC 的 checkpoint `ts` 当本地、
  `git_commit` 的 `Co-authored-by` 子串误判（他人的 trailer 会让 Theta 尾注被跳过）、
  `prompt.py` 漏登记 `git_push`/`git_clone`、`_register_failed` 跨会话串味（改为按
  `(会话, 名字)` 记账）；
- 文档漂移：`README.md` / `app/main.py` / `docs/EXCEPTION_DESIGN.md` / 3 个测试 docstring
  **共 5 个文件 11 处**的 `uv run`（会另建/重同步 Linux venv、破坏现有环境）、GitHub 工具
  计数统一到 **14 只读 + 3 写 = 17 条**、`SKILL.md` 的 PR 流程与示例文档冲突 + `git_pull`
  参数顺序写错、`Phase B 预留` / `尚未接入` 一类失效陈述；
- 删除：`_MAGIC_PREFIXES` 魔数表（file_io + github 两份）——它只产出显示标签、无分支依赖，
  且回执里本就回显 path（后缀在内），参见 `mcp_service/file_io.py:78` 的留痕注释；
  `tool.json` 的 `safe_tool`/`risky_tool`、`NoteEntry.topic/tags`、`auto_approve`/`auto_reject`、
  `InterruptRecord`（三个字段无人读，改为 `EvalResult.interrupt_count`）、terminal 的 6 处
  只写不读的进程状态、`graph.py` 的 `from app.agent.nodes import *`（连带 31 个无关名灌进建图
  模块的命名空间）、9 处未使用 import。

**2026-09-17 第二批已清**（同属低危，顺手做掉）：逐条改动与验证搬到了独立条目
[DONE.md](DONE.md) 的「低危清理第二批 + 评估 CLI 退出码（2026-09-17）」条，**本节只留"还剩什么没做"**（见下面「遗留」）。

**同时作废（引用的代码已删/早已修，不必再做）**：

- ~~`app/schema/__init__.py` 漏导 `ImageRef` / `SkillPreflight`~~——**早已导出**（`__init__.py:4,10`）；
- ~~`QueueNode` 空 docstring + 空 `__init__`~~——docstring 已写全（`nodes.py:1014`），且它本就没有 `__init__`；
- ~~`mcp_service/git.py:110` 的 `_get_repos()` 双扫~~——`git.py` 已于 2026-09-15 整体删除；
- ~~`file_io._resolve_path` ↔ `git._resolve_repo_path` 逐字同构~~——只剩下面那条 `file_io` /
  `mcp.py::_validate_workspace` 的两份近亲。

**遗留（均为低危）**：

- **只被测试用的生产函数**：`app/resource/skills.py::read_body`——生产路径走 `get_meta`/`body_of`，
  只有 `tests/test_skills.py` 在用；而同文件 docstring:24 把"host 侧只读 SKILL.md、不拿模型
  给的名字拼路径"这条安全不变量的落点指成了它（真正落点是 `get_meta`）。删需同步改
  `tests/test_skills.py` 里 4 条用例的调用。
- **自我标注的 YAGNI 字段**：`app/schema/agent_schema.py::MCPToolSpec.metadata`（注释自述
  "当前无消费者，保留以备将来"）。留就换成具体计划，删就顺带清 `app/platform/mcp.py` 两处透传。
- **跨文件重复**：`WORKSPACE_PATH` 的"取 env + resolve + 存在性校验 + **同文案** `ConfigError`"
  在 `mcp_service/file_io.py`（import 期校验）与 `app/platform/mcp.py::_validate_workspace`（构图期）
  各一份。抽到 `mcp_service/utils.py` 前先想清楚：file_io 那份是 **import 期**触发的，搬过去会
  把"import 即校验"也一起搬走——要么只抽纯函数（校验时机留在各调用点），要么显式拍板改时机。

**2026-09-22 追加（路径名已收束，这条是剩下的那一半）**：路径的资源清单、基准与落点**已经**全部
收进 `app/resource/paths.py`（规定见 docs/ARCHITECTURE.md §4.4；`Path(__file__)` 现在只许出现在
白名单里，`tests/test_resource.py` 末尾有用例守着）。**没做的是"工作区是从哪来的 + 校验"**——
现在有 5 个形近函数：`app/agent/graph.py::_resolve_workspace`（参量 / 默认 tmp）、
`app/platform/mcp.py::{_normalize_workspace, _validate_workspace}`（归一 / 构图期校验）、
`mcp_service/{sub_agent,dispatch}::_resolve_workspace`（env）、`app/tui/runner.py::resolve_tui_workspace`
（cwd）、`mcp_service/file_io.py`（import 期 env）。**它们语义真的各不相同**（来源分别是参量 / cwd /
env，时机分别是 import 期 / 构图期 / 调用期），所以不能一刀切合并——可共用的只有"归一化 +
存在性校验 + 同文案报错"那一段，且 file_io 那份的**时机**（import 即校验）搬走就会变。真要动就
按上面那条的原则来：**只抽纯函数，校验时机留各调用点**。
- **小冗余**：`mcp_service/file_io.py:699-700` 的 `ext_set` 两次赋值可合一
  （`(e if e.startswith(".") else f".{e}").lower()` 一次到位）。
- **远端 GBK**：`skills/github/server.py::github_file_read` 只做了二进制那半防线（NUL 探测），
  GBK 文本仍会 `errors="replace"` 成一片替换字符喂给模型——本地 `read_file` 2026-09-14 已补
  第二半，这里是漏掉的孪生。

**待拍（一条）**：`app/agent/tools.py:872` 的 `meta.name != meta.dir_name` 分支**生产不可达**
（扫盘期已保证能力型两者相等），但 `tests/test_skill_runtime.py:467` 自述是"安全网…即便拿到
构造出来的 meta，加载期也必须拒绝"。二选一：① 删掉该分支 + 同步删那条用例；② 按安全网保留
——保留的话请在注释里点明它不可达，并修掉 `app/resource/skills.py:174` 那处把校验位置指向
"get_skill 的校验"的指针（实际执行处是**扫描期**）。

## [ ] 评估框架的已知缺口（2026-09-16 记，09-17 更新）

**背景**：2026-09-16 给 `evaluation/` 补了**层 A 录制回放**（零 token，拿真实录制的模型输出当
固定输入样本，断言"除模型之外的一切"没变）与**闸门策略能力**（`deny_all` / `scripted` /
`allow_except(answers=…)`），默认命令从此不花钱。用法与两条层的分工见 README「模型行为评估」。
下面的事当时**显式没做**（2026-09-17 又核出"C 仓库里还没有 CI"一条），记在这里免得下次重新勘察：

- **任务集与断言覆盖面**：示例任务只有 3 个（读 / 写 / 计划），断言工厂只有 6 个。未覆盖：拒绝
  路径之后模型怎么办、`ask_user` 提问闸门、长期记忆写入、技能加载、`dispatch_subtasks`、compact
  触发。**策略侧已经能表达了**（`deny_all()` 测拒绝、"`allow_except(answers=[…])`" 答提问），
  缺的只是任务与断言本身。
- **量化与闸门**：`EvalResult` 只记次数与耗时，**不记 token**（要从 `AIMessage.response_metadata`
  聚合）。~~`__main__` 没有退出码，进不了 CI~~ **[已落地 2026-09-17]**：退出码三档
  （`0` 通过 / `1` 不通过 / `2` 用法错误），判据与理由记在独立条目
  [DONE.md](DONE.md) 的「低危清理第二批 + 评估 CLI 退出码（2026-09-17）」条。**仍缺两件**：① 上面这条 token 聚合；
  ② **基线快照对比（本次 vs 上次）**——退出码能拦"变红"，还拦不住"悄悄变慢、审批变多"。
- **仓库里还没有 CI**（2026-09-17 核）：**有了退出码 ≠ 接进了 CI**——全仓一个 CI 配置文件都没有
  （既无 `.github/` 也无 `.gitlab-ci.yml`）。真要把层 A 当回归闸门，缺的是那条流水线本身
  （跑 `python -m evaluation` 吃它的退出码；顺带跑 `-m pytest` 的 528 例——**注意测试那两条前提**：
  必须 `python -m pytest`、且 `WORKSPACE_PATH` 要经 `WSLENV` 传对，见 README / CLAUDE.md）。
- **按工作区复用编译图**：每任务建一次图、各拉一组 MCP 子进程（`runner.py` 模块 docstring 的 TODO）。
- **evaluation/ 自己的测试仍不完整**：同日补了 `tests/test_eval_{policy,fixtures,runner}.py`（策略
  语义 / fixture 读写与指纹 / SKIP 装配与清理顺序），2026-09-17 又补了 `tests/test_eval_report.py`
  （通过判据的三种红 + 用法错误退出码），但**真图回放**只有 `python -m evaluation` 手工跑
  ——没有人守着"回放这条路本身没坏"（这是最值得补的一环：它是唯一能守住
  "回放≠坏掉"的手段，且零成本）。

**顺带修掉的既有 bug（同日）**：`run_suite` 曾在返回前就 `rmtree` 掉临时工作区，而检查项要到报告
阶段才跑——于是**文件类断言恒失败**（`--keep-workspace` 时 8/8、默认 6/8）。清理已挪到
`cleanup_workspaces()`，由 CLI 在报告之后调。这正是"骨架没人真跑过"的直接后果，也是上面最后一条
值得补的理由。

## 相关但未立项（讨论过，待显式拍板再单列）

- 读策略太保守：`read_file` docstring 的"避免大文件整段塞满上下文"引导模型把 1280 行文件
  拆 ~200 行 × 多段读，徒增往返与重发成本；考虑改成"需要整份就整读、超大/只需探测才分段"
  + "相互独立的读取/检索尽量同回合并发"。
- ~~思考内容剥离~~：**2026-09-14 查明它在当前栈下是空转的**——`langchain-openai` 根本不提取
  `reasoning_content`（没有东西可剥），已改为由契约用例钉住库行为，见上面那条 `[x]` 条目。
- 长回合止损：run_tui 运行中响应 `q` 取消当前 turn；主 ainvoke 显式设 recursion_limit。
- 大体量广度探索的 dispatch 触发信号（docs/MULTI_AGENT §10"三模式如何被选中"）。
