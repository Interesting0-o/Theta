# Theta $\theta$ 总设计（架构原则）

> 本文档是所有分设计（异常 / 上下文工程 / 多 Agent / 长期记忆）的**上层**：先讲"系统长什么样、为什么"，具体机制进各自分文档。
> 状态标注：`[已落地]` = 已实现；未标注 = 目标态。相关：[[docs/EXCEPTION_DESIGN]]、[[docs/CONTEXT_ENGINEERING]]、[[docs/MULTI_AGENT]]、[[docs/LONG_TERM_MEMORY]]。

## 0. 一句话

**主程序是一个"事件基座"；LangGraph 图只是基座上的一块可插拔执行单元——图是项目里比较大的一个组件，不是项目的全部。**

## 1. 为什么（LangGraph 的定位与边界）

LangGraph 擅长的：把"一条 agent run"描述成带状态机、可中断、可续跑（checkpoint）的图。它的边界也清楚：

- 编译后的图拓扑**封闭**，对外只有 ainvoke / astream（流式/阻塞）；
- **没有"任意时刻往运行中的图临时注入"**——图只在一个个**预先埋好的 interrupt 点**等外部决定/输入（`Command(resume=…)`），之外的事件它既看不见、也不该由它管；
- 单条 run 的执行引擎 ≠ 系统协调者。谁在跑哪条 run、谁等谁、审批/消息/进程间事件从哪来、多 run/多 agent 怎么调度——这些**不该活在图里**。

推论：**事件机制不是"图要支持的功能"，而是"图外面那层主程序的事"**。图被一个 driver 驱动：起 run / 送事件续 run / 取消 run；interrupt 是图留给这层基座的**事件输入端口**。

## 2. 架构原则（基石）

1. **主程序 = 事件基座**：负责事件的**处理与发送**、进程间通信、run 生命周期（起 / park / 续 / 取消）。`[已落地 · 雏形]`
   - 2026-09-07 起 main 已是**事件驱动壳**：interrupt **阻塞图、不阻塞进程**；本地主 agent interrupt 与 worker 审批**对齐进同一 broker**（`app/platform`：`loop` 事件循环 / `approvals` 统一队列 / `turn` 驱动原语）。
   - 2026-09-10 起**基座与前端分家**：基座 = `app/platform`（run 生命周期 + broker + runtime，**不 print、不读 stdin**）；终端前端 = `app/tui`（只实现下面 §2.7 的 `UI` 协议）。
2. **LangGraph 图 = 基座上的执行单元（actor）**，不是底座：一个图实例承载"一条 run 的引擎"，经 driver 把外部事件 ⇄ 图翻译——新事件 → 起/续一条 run（thread_id）；interrupt 挂起 → 发事件等外部决定 → `Command(resume)` 续跑。`[已落地 · 主 agent 图作模块；worker 子图作执行单元]`
3. **interrupt 当事件端口用，不拆图**：图在关键点（审核、计划审批、轮末）埋 interrupt，driver 把它们注册成事件源。别为"事件化"把图拆碎——那会丢掉 checkpoint 续跑。
4. **"临时注入"的语义定死**：向一条 **park 在 interrupt** 的 run 注入决定/消息（支持）；向未 park 的 run 硬塞输入（不做——run 应常驻在事件边界等唤醒，而不是被硬插）。
5. **跨 run / 跨进程复用同一协议**：worker→主的审批回传已是 HTTP（收件箱 + 长轮询）；将来主→worker / @mention 复用同一套"事件 + resume"语义，只是换个送达方。
6. **最小基座，先收敛命名、后引框架**：先把现有事件壳命名成清晰的"事件类型 + dispatch"，不急着上通用消息总线；仓库还小，够用再泛化。
7. **基座不与前端耦合**：基座只经 `app/platform/ui.py::UI` 协议（三个方法：`emit` 渲染事件 / `read_line` 取一行输入 / `decide` 就一条待决的**闸门请求**问人）与外界说话，事件形状在 `app/schema/ui_schema.py`。基座**不 print、不读 stdin**；前端（`app/tui` 终端 / 将来的 web）只实现协议、不碰调度。**这层缝是为第二个前端而设**：换前端不该重写 run 生命周期与 park/resume 调度。`[已落地 · 终端一个前端]`
   - **闸门有两种**（2026-09-16）：`decide` 收到 `value["type"]` 决定怎么问——`tool_approval`（工具审批，终端画面板收 y/n）或 `ask_user`（agent 提问，终端收"选项 + 补充"两段）。回答统一是 `Decision` 值对象（`app/schema/approval_schema.py`），**决定权威在人**；两类闸门的"没人回答"语义不同（审批 fail-closed 不放行 / 提问记未作答），见该协议的 EOF 契约。
   - 推论：**用户命令（`/init`…）是基座的控制面事件**（`app/platform/commands/` 包：机制与命令表在 `__init__.py`，载荷在 `prompts.py`，各命令在 `session.py` 等），在"起 turn 之前"介入 `loop`——前端只负责把输入交上来、把 `Notice`/事件渲染出去，不解析命令。

## 3. 分层（放在上面的东西）

```
┌─────────────────────────────────────────────────────────┐
│  前端（终端 TUI = app/tui；将来的 web 同协议）              │
│  · 只实现 UI 协议：emit 渲染 / read_line 取输入 / decide 问人 │
│    （decide 分两类闸门：工具审批 y/n、agent 提问选选项+补充） │
│  · 终端形态：stdin 线程 + 面板渲染 [已落地]；web 前端 [设计]  │
├─────────────────────────────────────────────────────────┤
│  事件基座（主程序 = app/platform）                          │
│  · 事件处理/发送（统一 broker：ApprovalInbox 雏形 [已落地]） │
│  · 进程间通信（HTTP 收件箱 [已落地 worker→主]）              │
│  · run 生命周期：起 / park / 续 / 取消（loop/turn）[已落地]   │
│  · 不 print、不读 stdin——经 UI 协议与前端说话（§2.7）        │
├─────────────────────────────────────────────────────────┤
│  LangGraph 图 = 执行单元（actor，经 driver 驱动）           │
│  · 主 agent 图 get_main_agent_graph          [已落地]      │
│  · worker 子图 get_sub_agent_graph          [已落地]       │
│  · 关键点 interrupt 作为事件输入端口          [已落地]       │
├─────────────────────────────────────────────────────────┤
│  状态 / 记忆 / 项目画像（会话、workspace）                  │
│  · 会话 checkpoint（按 workspace/session 分目录）[已落地]    │
│  · AGENT.md（项目画像）/ memory（执行约束）    [已落地]      │
│  · 会话命令（/list session、/new session、/session）[已落地] │
└─────────────────────────────────────────────────────────┘
```

- 记忆/会话/画像层让事件基座 + 图能"跨会话认出这个项目"，见 [[docs/LONG_TERM_MEMORY]]。
- 多 Agent（并行分发 / DAG / @mention）就长在事件基座上：派发是事件、worker 审批是事件、归并是事件——见 [[docs/MULTI_AGENT]]。

## 4. 由此定下的纪律

- 协调逻辑（调度、审批、消息路由）**不进 LangGraph 节点**，放事件基座/其 handler。
- 图对外只通过 driver 暴露：`ainvoke/stream` 包在 driver 里，图不直接面向事件循环。
- 新增"agent 能力"先问：它是**图内的一条边/一个节点**，还是**基座上的一类事件/一条 run**？前者是小步执行、后者是整体编排——两者用错会拧巴（见 MULTI_AGENT 三种编排模式的对偶）。
- **基座与前端**（§2.7）：基座（`app/platform`）**不许** `print` / 读 stdin / `import app.tui`；前端（`app/tui`）**不许**碰调度与 run 生命周期——它只实现 `UI` 协议。新增前端能力时先问"这是渲染/取输入，还是调度？"，后者属于基座。用户命令（`/init`、`/session`）属**基座的控制面事件**，落 `app/platform`；前端只渲染它们的输出。
- **工具面：新增工具先问四句**（2026-09-21 定；**取代**此前一切"按返回值形状 / 按是否需要注入"的分类说法。依次问，前一句答"否"才问下一句）：
  1. **它是对外部世界能力的调用吗**（读文件 / 跑命令 / 搜网页）→ 是 = **MCP 工具**（`mcp_service/` 或技能目录：住**自己的子进程**、工作区来自启动 `env`、返回值 `ToolResult`、走 `ToolNode`）。
  2. **不是的话——它改变当前 Agent 的运行语义吗**（plan / notes / loaded skills / pending work / subtask）→ 是 = **Agent orchestration**（编排工具，`app/agent/tools.py`）。**这就是编排工具的定义**：改的是 agent 自身，不是外部世界。而**改变的方式本就各样**（写 `current_plan` 切片的、只产一条回执的都有）——"返回 state 切片"不是定义。
  3. **它还涉及持久化 / workspace / session 等资源吗**（如 memory 读写、dispatch 的工作区）→ 那不是退回 MCP，而是 **Resource abstraction** 方向：资源访问要有单点（`app/resource/paths.py` 那类）。
  4. **它还需要独立进程 / 独立 Agent runtime / 独立能力边界吗** → **才**考虑 **MCP / worker boundary**——**multi-agent 正落在这里**（`dispatch_subtasks` 派生 worker = 独立 agent runtime）。
  ⚠️ **两条不得用作判据的东西**：① **返回值的形状**（见上第 2 条）；② **"需要工作区"**——`file_io` 需要工作区却住在子进程（工作区就是它的启动配置），`InjectedWorkspace` 只是"留在主进程"的**后果**、不是理由。两类工具的返回值**为何不做统一**，见 [[docs/EXCEPTION_DESIGN]] §5。
- **`app/agent/`：一个东西该不该进来，先问三句**（2026-09-21 定；**与上一条配对**——上一条判"是 MCP 还是编排"，本条判"进不进 `app/agent/`"，**三条都不中**才轮到上一条决定它去哪）：
  - **A · 是否参与 Agent 的决策上下文**——"**Agent 现在知道什么**"：当前对话 / plan / notes / memory / loaded skills / 工具状态 / workspace 状态的语义表示；
  - **B · 是否定义 Agent 的决策过程**——"**Agent 如何从当前状态走向下一步**"：`LLM → tool call → queue → review → orchestrate/tool → LLM`；Graph / Node / AgentState；
  - **C · 是否定义 Agent 可以采取什么行动**——Agent action：plan / load_skill / write_memory / dispatch_subtask。
  命中任一即属于 `app/agent/`。**C 有最容易做错的一处**：**Agent action ≠ action implementation**——`app/agent/tools.py` 描述"Agent 可以做什么决策性动作"（*"我要写这个文件"*），`mcp_service/file_io.py` 负责"这个动作实际怎么执行"（真正写盘）；两者**同名、同签名、同一业务**，从名字看不出谁是谁。**同一条区分在节点层还会再出现一次**：节点做"这一拍做什么"（拼 messages / 调 / 写回），被它调用的模块做"资源怎么读、怎么转"——别把它当成两条独立规则。
  ⚠️ **反向限制**：**代码只因为 Agent 恰好调用了某个外部边界，并不因此属于 Agent。** 判法——问"**如果那条外部边界不存在，这段代码还需要吗？**"：`app/platform/tool_results.py::format_tool_result` 与 `app/platform/mcp.py` 答"不需要"（它们的存在理由是 MCP 边界的形状），`app/agent/tools.py` 答"需要"。
  已判出的实例与落地清单见本文 §4.1 的落地表（原始审计记录在 [[docs/DONE]] 的「`app/agent/` 的
  判据（A/B/C）与资源层拆分」条）。

### 4.1 判据的落地结果（2026-09-21，三簇一起做）

按上面两条判据过了一遍全仓，实际搬动如下（**看代码时以这一节为准**，别照旧文档翻老路径）：

| 原位置 | 现位置 | 判据 |
| --- | --- | --- |
| `app/resource.py`（单文件） | **`app/resource/`（包）**：`paths.py` / `memory.py` / `skills.py` / `images.py` / `profile.py` | 资源层 = 读 agent 之外的东西 → 内部表示 |
| `app/agent/memory.py` / `skills.py` | `app/resource/memory.py` / `skills.py` | 同上（消费者跨 agent + platform，一个被基座 import 的模块不该住 `app/agent/`） |
| `LLMNode` 里的图片通道（139 行）/ 画像读盘（31 行） | `app/resource/images.py` / `profile.py` | 节点做"这一拍做什么"，资源访问不归它（节点只留"读一次还是每轮读"这个决定） |
| `app/agent/model.py` | `LLMNode.thinking_extra_body()` / `LLMNode.main_chat_model()` | "这一拍发给谁"属节点；**并入时保留了 import 期读 `.env` 的时机** |
| `app/agent/mcp.py` / `utils.py` | **`app/platform/mcp.py` / `tool_results.py`** | 反向限制：答"不需要"（存在理由是 MCP 边界）；两者是同一件事的两半，放一起 |
| `ReviewNode` 的免审判定 / `tools.py` 的重复读表 / `ReviewNode` 的提问校验 | **`app/agent/gates.py`** | 都是"闸门挂起前必须成立的判定材料"；策略表仍与解析器同住（纪律见 §4 上一节） |
| `app/agent/tools.py::dispatch_subtasks` | **`mcp_service/dispatch.py`** | 四问第四问（派生独立进程 / 独立 Agent runtime）；memory 则按第三问留在编排工具 |

**顺带定下的两条"单一落点"**（此前形状靠两侧注释互相指认）：`ToolMessage` 的结构化戳由生产端
（`app/agent/nodes.py` 的 ToolNode / ReviewNode，经 `_tool_stamp`）定义、CompactNode 只读常量；
编排工具返回切片的协议由执行器（`OrchestrateNode` + `_EXTRA_SLICE_KEYS`）定义、`tools.py` 按它写。
**为什么切片的权威在消费端**：生产者与执行器分居 `nodes.py` / `tools.py`，而 `nodes.py` 刻意不
import `tools.py`，"权威放生产端"就得造一条反向 import——写成"执行器定义协议、生产端照写"即可。

### 4.2 文本渲染的落点：受众 / 形状主人 / 消费者数（2026-09-22 定）

散乱的从来不是"家"，是"规矩"。全仓的渲染函数分布其实很整齐：跨模块的那几个都已经住在**形状
主人**那里（工具回执 → `app/platform/tool_results.py`；注入块 → `prompt.py` / `memory.py` /
`skills.py`；面板 → `app/tui/panels.py`），只有一个消费者的就近留着（`CompactNode._render_line`、
`_tool_call_log`、`_snapshot`、`_render_session_line` …）。所以只补一条判据，**不新建"formatters 袋"**：

1. **受众是谁** —— 给模型 / 给人 / 给磁盘。它决定验证方式（给模型的改动要跑 `--live`），也决定
   **能不能合**：`panels.truncate`（人看的 `" ...(共 N 字符，已截断)"`）与 `skills/github._clip`
   （教模型补救的 `"…（已截断，完整内容共 N 字符；可用参数缩小范围再取）"`）受众与后缀语义都不同，
   **判过、不合**。
2. **这段文本的形状由谁定义** —— 渲染跟着形状主人走：ToolResult → MCP 边界模块；state 切片 → 那个
   节点；技能目录 → `app/resource/skills.py`；记忆文件 → `app/resource/memory.py`。
3. **几个消费者** —— 1 个：留在消费者里（就近，别提前抽象）；>1：提到形状主人那一层；>1 **且跨层**
   （agent ↔ platform/tui）：放中立层（只依赖 stdlib / schema），否则**写明"为什么不合"**。

**命名**：注入给模型的块统一叫 `*_block`（`workspace_context_block` / `handoff_pointer_block` /
`memory_block` / `skills_block` / `catalog_block` / `agent_md_block`）——这是既存的事实约定，只是
从没人写下来；`format_tool_result` 是唯一例外（它也给模型，但名字已是对外契约，改了只有成本）。

**为什么不集中**：`app/format.py` 之类会变回无序工具袋（本项目删过一次同性质的
`app/agent/utils.py` 并留了警告 docstring），而且受众不同的函数放一起会持续诱导后人合并。

**已知重复、判过不合**：`app/platform/commands/session.py::_short_line` ↔ `nodes.py` 的
`CompactNode._first_line`（逐字相同，但跨层 import 的代价 > 4 行重复；2026-09-21 拍板方案 B）。

### 4.3 配置的归属（判据，2026-09-22 定）

`config.py` 收**两类**东西（2026-09-22 修订）：

1. **环境配置**（`Settings`，`.env` 驱动）：随环境 / 部署 / 人变，且改它不该动代码——凭证、端点、
   模型名、开关、技能白名单。
2. **跨模块共用的量级常量**（模块级常量，**不是 .env 键**）：一处值、多处调用；放这里是为了避免
   同一量级在各模块各写一遍、再靠注释互相指认"同量级"（`TEXT_BUDGET_CHARS` 就是这么合出来的）。
   ⚠️ 若某个量真成了用户旋钮（随部署变、改它不该动代码），再从第 2 类升级成第 1 类（`Settings` 字段）。

**配置键的取值域跟键同处声明**（`THINKING_MODES` 挨着 `CHAT_THINKING`）——取值域属于键，不归消费方。凡"改了要重审行为、要跟测试与文档"的，都是**口径**（预算 / 上限 /
超时 / 词法 / 协议标记），留在代码里、并尽量给构造参量缝（`CompactNode(content_budget_chars=…)`、
`LLMNode(cap=…)`）——搬进 `.env` 只会多出一个"没人评审就能改行为"的静默面。
⚠️ 子进程侧读 `os.environ` 是**运输**不是配置：真值在 `get_settings()`，由 host 经
`stdio_connection(extra_env)` / `skill_env` 注入。审计与两处缺口见 [[docs/TODO]]「配置的归属」条。

### 4.4 路径的取得方式（规定，2026-09-22 定；落地见 `app/resource/paths.py` 的 docstring）

路径是唯一"**便宜到没有约束**"的资源：可拼、可上溯、`..` 随便接，所以"能不能拿到"从不构成限制，
约束只剩规定。四条（**规矩本身写在 `app/resource/paths.py` 顶部**，那里同时是全仓的资源清单）：

1. **落点只能调用、不许拼** —— 约定资源（名字与位置由我们定死，如 `AGENT.md`）一律经具名函数取：
   `agent_md_path(ws)` / `handoff_md_path(ws)` / `memory_path(ws)` / `session_db_path(ws, sid)`。判据是
   **约定 vs 发现**：`<ws>/**/*.py` 是发现（由内容与模式决定）→ 拼接 + glob 正当。
2. **拼接的权利只属于"这段布局的主人"，且只在自己根以下** —— `skills.py` 拼
   `SKILLS_DIR/<dir>/skill.json` 正当（目录里摆什么是它的知识）；**别人不许替它拼**，多一个 `..` 也不行。
3. **基准不许自算** —— 不许用 `Path(__file__).parents[N]` 推断项目根（文件一挪，它不报错、只是静默
   指向别处）；基准只由 `paths.PROJECT_ROOT` 给出，其余（`RESOURCE_ROOT` / `SKILLS_DIR` /
   `DEFAULT_WORKSPACE` / `.env` 定位）全部派生。
4. **`..` 在生产代码里基本是禁令** —— 唯一合法例外是**解析别人给的路径**（`file_io` 的沙箱解析、
   `images` 的 `@路径`、`config` 的 `.env` 定位）：那是输入处理，`resolve()` 后必须过校验。

**刻意不归 paths 的两处**：`tool.json` / `command_policy.json`（闸门判定材料必须与解析器同住，
`app/agent/gates.py` 用 `Path(__file__).with_name(...)` 自拼）与 `evaluation/fixtures.py`（评估框架
自己的样本目录，app 不该知道评估存在）。**规定可检查**：`tests/test_resource.py` 末尾两条用例钉住
"基准只算一处"与"落点函数存在"——谁在别处就近算一下项目根，测试立刻变红。

### 4.5 函数的返回值形态（判据，2026-09-22 定）

**只有两种合法形态**：

1. **单一类型**（含 `X | None`）。`| None` 是"**有没有**"（存在性），不是"多类型"——Python 里它
   仍是单一语义，不必为它造包装。
2. **数据结构体**（`frozen dataclass` / `TypedDict`）——"两个以上字段一起返回"时用它。

**禁止两样**：① 把**互斥的结果**塞进元组（`(载荷, 错误)`、`(命中, 候选清单)`）；② 三元组以上的位置
返回。理由：位置返回**没有名字**——调用方只能靠顺序记"哪一半是错误"，类型上也区分不出来；而值对象
自带字段名，还能带上 tag。

**正例（照抄它们）**：`ToolResult(success, content, error_type)`（app/schema —— "成败 + 载荷/原因"）；
`parse_command → PromptCommand | ActionCommand`（app/platform/commands —— "用数据结构体表达互斥"）。

**配套约定**（让这条判据不必去打所有 `X | None`）：**"只有一条载荷、且那是一句可行动的错误文案"的
函数统一返回 `str | None`，`None` = 通过**——`_register_skill_runtime` / `_check_skill_tools` /
`_skill_preflight_line` 已经是这样。它与第①条禁令不冲突：那条禁的是"两种**载荷**互斥"，这里只有
一种载荷 + 存在性。

**刻意例外（写明，别顺手改）**：

- **编排工具返回的 state 切片**（`dict`）—— LangGraph 合并 state 的规定，必须 dict；
- **UI 协议载荷**（`approvals._record_to_value` 的 dict）—— 形状由 `app/schema/approval_schema.py`
  的常量与测试钉住，是**跨边界形状**；
- **`turn._race` 的 `(idx, value)`** —— select 原语，`idx` = 哪一路胜出，"编号 + 值"就是它的语义；
- **`mcp.py::_make_shim` 的 `(content, None)`** —— langchain `content_and_artifact` 契约要求。

**落点**（沿用既有惯例）：跨边界/进 state 的值对象 → `app/schema`（`TypedDict`）；只服务生产模块的
值对象 → 留生产者模块（`frozen dataclass`）。**已量出的 12 处清单与分批计划**见 [[docs/TODO]]
「函数返回值：多类型 → 值对象」条。

### 4.6 枚举 / 标签的真相（判据，2026-09-22 定）

"枚举"在这里指**一切封闭词表**：`Literal`、状态字符串、`source` 标签、工具名清单、`error_type` 取值、
模态名……它们散落时不会编译错，只会**静默走岔**（词写错 → 落到别的分支或落盘成垃圾）。所以规矩只有
一句：**一个词表只有一份真相，且它落在"谁定义这个词"的那一侧**。

| 词表在哪一侧 | 真相该住哪 | 消费方怎么用 |
| --- | --- | --- |
| **跨边界**（模型可见 / 进 state / 过 checkpoint / 过进程） | `app/schema`（`Literal` 或常量） | 直接 import；能派生的**一定派生**（`PLAN_STATUSES = get_args(PlanStatus)` 是先例） |
| **停在单个模块** | 留那个模块，但**要有类型**（`Literal`），不许裸 `str` | 模块内直接读；跨模块消费就该升级成上一行 |
| **住在数据文件里**（`tool.json` 的 `source` / `worker_allow` / `free_when`） | **数据是登记处**，但**取值清单要在代码里有一处声明** | 代码侧用它做分流与校验；另加一条契约测试把数据与清单对齐 |

**三条补充（2026-09-22 用户拍板）**：

- **悬空判据**：枚举的意义是"用规范的字段传递消息"——**生产了却没有任何地方按它拆解**的词表/字段
  就是悬空的，**直接删**（实例如 `NoteEntry.kind`：只写不读，连带它的映射表一起删）；只有生产侧、
  消费侧却硬编码字面量的叫"半悬空"，处置是**给一份可引用的常量**（如 `DECISION_ANSWER`）。
- **字典的键不值得用变量承接**：`stamp[STAMP_ERROR_TYPE]` 与 `stamp["error_type"]` 没有区别——
  键名写字面量，常量留给"要按值分派或校验"的地方（`GATE_*` / `ASK_SELECT_*`）。
- **多处使用 → `app/schema`**：一个词表被三处以上引用（校验 / 给模型的 schema / 渲染分支）时，
  真相进 `app/schema`，消费方一律 import（`AskSelectMode` / `ASK_SELECT_*` 即如此）。

**两条配套**：① **同义不许两套拼写**——同一件事有两个词表（如闸门 `Decision.kind ∈ {approval, answer}`
↔ 载荷 `type ∈ {GATE_*}`）时，必须在**一处**写明映射，不能只靠某个 `if` 分支隐含；② **名字清单不许手抄**
——"哪几个工具是联网四件套"这类，若能从既有分组/登记处派生，就不许再列一遍（手抄的那份会随数据改动
漂）。**已量出的四类分布与分批计划**见 [[docs/TODO]]「枚举 / 标签的真相在哪」条。

### 4.7 MCP 这条线的职责：四件事、三个层（2026-09-22 记）

**背景**：`MCP` 这条线的职责**已经漂移**——它起家是"给 agent 提供工具（后来加上技能）"，现在
额外承担了三件事：**管 MCP 进程的生命周期**、**当 `get_skill`/`drop_skill` 的底层**、**管子 agent
的进程**。名字（`mcp.py`）只覆盖第一件，所以看代码的人会误判"新东西该放哪"。

**现状（按代码实测，不是设想）**：`app/platform/mcp.py`（669 行）里混着四类东西——

| 内容 | 性质 |
| --- | --- |
| `stdio_connection` / `_build_servers` / `_inbox_env` / `load_mcp_tool` / workspace 校验 | **真 MCP**：协议与连接配置 |
| `_TOOL_SPECS` / `_SERVER_SPECS` / `_tool_specs` / `_server_specs` / `_make_shim` | **工具面**：schema 与不绑会话的 shim |
| `_ServerWorker`（owner task：懒起 / 逐条执行 / 自愈重建 / 在自己的 Task 里关）+ `get_worker` + `close_session_pool` / `close_all_pools` | **运行体机制——不是 MCP 专属**：它是"一条长活子进程会话 + 严格生命周期约束（anyio cancel scope 与 Task 绑定）"的通用实现，MCP 只是目前的唯一载体 |
| `register_server` / `unregister_server` / `session_tools` / `session_tool_names` / `registered_servers` / `tools_version` | **会话级工具视图**（技能二期加的动态增删） |

而三件新职责**分散在三个层**：

| 职责 | 机制 | 胶水 | 生命周期触发 |
| --- | --- | --- | --- |
| MCP 工具供应（core + 技能） | `app/platform/mcp.py` | — | — |
| MCP 进程生命周期 | 同上（`_ServerWorker` + 池） | — | `platform/loop.py`（切会话/退出）、`evaluation/runner.py`（每任务）、worker 进程自身退出 |
| 技能运行体（`get_skill`/`drop_skill` 的底层） | 复用同一个 `_ServerWorker` | **`app/agent/tools.py` 里的 466 行**（`_register_skill_runtime` / `_check_skill_tools` / `_probe_skill_startup` / 体检 / 依赖预检）+ `SessionToolset` 124 行 | `get_skill` / `drop_skill` / 每轮对账 |
| 子 agent 进程（dispatch） | `mcp_service/dispatch.py`（主侧 spawn + 批次） | `mcp_service/sub_agent.py`（被拉起的 server） | 派发时；审批回流走基座 broker |

**判据化的归属**（沿用 §4 的四问与 A/B/C）：

| 东西 | 判据 | 该住哪 | 现状 |
| --- | --- | --- | --- |
| MCP 连接 + 工具 schema / shim | 四问第一问（对外部能力的调用）→ 工具面 | `app/platform/mcp.py` | ✓ |
| **运行体机制**（owner task / 池 / 自愈 / 关闭） | 进程生命周期**基础设施**；A/B/C 三条都不中 → 不进 `app/agent/` | platform —— **但它不叫 MCP 也成立** | 名字与职责不匹配 |
| **技能运行体的胶水** | 同上（起进程的基础设施），**不是**"Agent 可以做什么动作" | 该与运行体**同侧** | **错位在 `app/agent/tools.py`**（TODO「记账 2」点名过的那一族） |
| 子 agent 进程 | 四问第四问（独立进程 / 独立 Agent runtime）→ multi-agent | `mcp_service/dispatch.py` + `sub_agent.py` | ✓（已成对） |
| 跨进程审批回流 | 基座的一类事件 | `app/platform/approvals.py` | ✓ |

**结论（供后续动手时照此）**：这条线的**机制**（进程/会话生命周期）与**工具面**（模型看得见什么）
是两件东西，只是今天装在一个文件、共用一个名字；技能运行体的胶水则与机制同性质、却住在工具定义
的模块里。**待办与分批计划**见 [[docs/TODO]]「MCP 这条线的职责漂移」条。
