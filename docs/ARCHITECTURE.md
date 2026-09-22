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
  已判出的实例与落地清单见 [[docs/TODO]] 的「`app/agent/` 的判据（A/B/C）与资源层拆分」条。

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
