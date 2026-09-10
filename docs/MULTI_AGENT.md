# Theta $\theta$ 多 Agent 编排设计（草稿）

> 状态标注：`[已落地]` = 已实现；`[待验]` = 需实验确认；未标注 = 目标态（设计方向，未实现）。
> **§9/§10 是按日期追加的落地记录**（每条自带日期与当时的"下一步"字样）——**要现状就读最新的那一条**，别按字面把早条目当现状；本页顶部与文末"模块落点"是现状口径，会随重构更新。
> 相关：[[docs/CONTEXT_ENGINEERING]]（只流结论 / notes 复用）、[[docs/EXCEPTION_DESIGN]]。

---

## 0. 一句话

**三种编排模式平级，按任务选型，没有优先级**：相互独立 → 并行分发；依赖清晰 → DAG；需要 agent 间自由对话 → @mention / A2A。三者共享同一套底座（共用 MCP、统一人工审批、只流结论）。**"先实现哪个"是实现顺序，不是架构优先级**——当前先落 DAG。

**拉起形态（已定起点）**：主 agent = orchestrator（拆解/审批/调度/归并，保持现有 graph）；worker = 执行单元（跑单子任务，才是"做成可调用的东西"的一方）。**先用同进程子图**（候选 A：worker 复用同一套 nodes 再 compile，审批天然留主侧）；**同进程出现瓶颈才升级 worker=MCP server**（候选 B，§5：换进程隔离/对外复用，但先要解跨进程审批）。当前先落 DAG，载体即 A。

> **现状修正（2026-09-06/07）**：实际的 isolated 首步落地为 **worker = spawn-per-task 的只读 sub_agent MCP 进程**（候选 B 的运输形态，见 §5/§9），候选 A 同进程子图未实现。选型现状以 §5/§6/§10 各节标注日期的最新"已定"为准。

## 1. 编排模式（三种平级）

| 模式 | 适用任务 | 机制 | 实现顺序 |
|---|---|---|---|
| **并行分发** | 相互独立、无依赖（如"逐季分析 2024–2026 财报"） | 主 agent 拆独立子任务 → 并发跑多个子 agent → 归并 | 后（P1 起） |
| **DAG** | 依赖清晰、下游吃上游（如"做含前后端+数据库的网站"） | 主 agent 产依赖图 → 拓扑排序 → 就绪节点逐个/并行调度 → 归并 | **先（P0）** |
| **@mention / A2A** | 需要 agent 间你来我往的对话澄清 | agent 间走消息/协议（A2A），非共享全量消息流 | 后（远期） |

选型依据是**任务耦合度/协作形态**，三者平权；一个任务里也可**混合**（主干 DAG，某个子任务内部再并行/对话）。

## 2. 公共底座（所有模式共享）

无论哪种模式都复用以下地基，因此多 agent 不是推倒重来：

- **共享 MCP 服务**：单套 MCP server、工具只绑一次，主 + 全部 agent 共用（§5）。
- **统一人工审批闸门**：`interrupt` 单点；审批对象可以是一层"计划/子任务表"，也可以单次敏感调用（§6）。
- **上下文只流结论**：节点间传"结论 + 文件指针"，不传全量历史（上下文工程纪律）。
- **工作区即共享真值**：文件产物跨 agent 交接，细节各自 read/search。

## 3. DAG 模式（先落地，P0）

主 agent **不亲手执行**，产出结构化依赖图，作为人工审批对象：

```json
{ "tasks": [
    {"id": "t1", "agent": "backend",  "task": "实现 X API",   "deps": [],  "produces": "services/x.py"},
    {"id": "t2", "agent": "frontend", "task": "对接 X API",   "deps": ["t1"], "consumes": "services/x.py"},
    {"id": "t3", "agent": "qa",       "task": "联调验收",     "deps": ["t2"], "produces": "验证报告.md"}
]}
```

流程：
1. 主 agent 生成依赖图（JSON，先结构化、不靠模型自由文本）→ **interrupt 让人类审批这张表**（通过才开工，可改拆法）；
2. **拓扑排序**，依赖就绪的节点才调度（多个就绪节点可并发，是 DAG 的副产品，不是独立并行模式）；
3. 每节点 = 调用一个下层 agent 执行该子任务（同底座内，见 §5）；
4. 节点完成 → **交付物验证门**（produces 存在且非空、结论非空）→ 解锁下游；
5. 主 agent 汇总（重读文件真值，只信工具回执）。

## 4. agent 间沟通（跨模式共同需要）

- **文件产物**：上游写 `produces` 文件，下游 `consumes` 该路径，需要细节自己 read/search。
- **结论注入**：调度器把上游 `result_summary`（一两行结论 + 文件指针）注入下游 prompt。
- **显式移交 / 对话澄清**：需要 agent 间交流时，由主 agent 或调度层在中间转发消息（DAG/并行内用这个）；**自由来回**则整体切到 @mention/A2A 模式（§1）。在候选 B 的常驻运行时里，@mention / 消息以"inbox 事件 → resume 目标 run"送达（§5 运行时骨架）——worker 从不阻塞故天然可被提及。

## 5. 拉起形态：主/worker 分层（关键约束；起点 = 同进程子图，升级 = worker=MCP）

先厘清两层定位，避免把镜像当方向：
- **主 agent = orchestrator**：拆解任务、定依赖、审批、调度、归并，是**调用方**；保持现有 graph（`get_main_agent_graph` 那套）不动，按需加 DAG/调度能力。
- **worker（子 agent）= 执行单元**：跑单个子任务，是**被调方**，才需要"做成可调用的东西"。N 个 worker 复用同一套 agent 代码、各实例独立上下文；"把主 agent 做成 MCP 给别人调"属被更外层系统编排的远期选项，与内部多 agent 解耦，不在本设计内。

worker 放哪个进程，直接决定"审批这道坎"长什么样。**已定：先 A、后 B**——A 是当前工作形态；B 等 A 出现瓶颈再做（判据见本节末尾）。

> **现状修正（2026-09-06/07 落地演进，覆写上面的"已定"）**：代码实际走的不是 A——isolated 落地为 worker = 候选 B 的**运输形态**（spawn-per-task 的只读 sub_agent MCP 进程，见 §9 P0），候选 A（同进程子图）未实现；本 §5 候选 B 的常驻事件循环运行时两侧皆远期。**main 侧事件化（2026-09-07 决策，§6 统一审批视图）**把"多 run 可 park"底座放进主进程、令候选 A 变便宜 → A/B 分叉重开，留后续单独决策，不在此次混入。

**候选 A · 同进程子图（起点，P0/P1 采用）**
- worker = 用同一套 nodes 再 compile 的"子任务图"：同 model + **收束的子任务 prompt** + **裁剪的工具子集**（只读检索/规划/notes），每子任务独立消息栈/上下文；主图调度节点按 DAG 就绪 **asyncio 并发 ainvoke 多个 worker 图**。
- **审批零成本**：worker 不授写/命令工具 → 主图 review→interrupt 单点原样成立，写动作执行权永远在主侧 ToolNode，**无需跨进程审批回传**。
- 底层工具复用现成：`app/agent/mcp.py` 已有按工作区键的进程级工具缓存 + 单飞锁（`_MCP_TOOLS_CACHE` / `_MCP_LOADING_LOCKS`，当初为 langgraph dev 反复 `get_main_agent_graph` 提速而加），**主 + worker 直接 bind 同一批工具对象**即可，不再各自拉起子进程。
- 代价：与主同进程，无故障隔离；worker 上下文隔离要自管（独立消息栈）。二者短期都非目标。

**候选 B · worker = MCP server（升级路径：A 出现瓶颈再做）**
- 每个子 agent 做成一个 MCP server（或其中的 `run_subtask(spec) → result_summary + produces` 工具），主 agent 经 `ToolNode` 当普通工具调；`tool.json` 登记派发动作（`need_review` 可门控"派发子任务"这个动作本身）。
- 好处：进程隔离、worker 状态封在 server 会话、主 agent 只拿 `result_summary + produces 文件指针`、可对外复用。
- 代价：worker 进程内跑 agent 循环要自绑工具；worker 内敏感操作的 interrupt 主侧不可见 → 若坚持"逐次审批"粒度，需建"审批回传通道"（§6 已有具体候选：HTTP 控制面回传）；若接受"派发即授权"的粗粒度档，则无需回传（§6 层次 2）。

**候选 B 的运行时骨架（常驻事件循环；2026-09-06 讨论收敛，取代"worker = 一次 run_subtask 跑完即结束"的工具化想象）**

worker = **常驻异步事件循环**（resident runtime），一个进程持有多条 **checkpoint 化的 run**（多 `thread_id` = 多 agent 上下文），由 inbox 事件驱动、统一调度"该 resume 哪条 run"：

| inbox 事件 | 动作 |
|---|---|
| 审批回包（主侧发回，§6） | resume 对应 thread_id 的挂起 run |
| @mention / 显式消息 | resume 目标 run（消息作新输入）或起新 run |
| 新子任务派发 | 起新 run |
| 定时 / 超时 | resume 或做失败收尾 |

关键不变量——**"图挂起" ≠ "进程阻塞"**：
- worker 遇敏感动作仍用原生 `interrupt()`，run 挂在 checkpoint（不占线程/连接），控制权回到事件循环继续 await inbox；恢复用 `Command(resume=...)` 按 thread_id 从 checkpoint 续跑。挂起可无限期、恢复干净，其它 run / 主侧照常推进。
- `run_subtask` 定位为**派发动作**：立即返回 ticket，审批与最终结果都走 §6 控制面回传；**不做**"同步等到干完再返回"的工具（会卡死调用方 ToolNode）。
- 进程级资源单独管理：按"活跃 agent 集合"定驻留池、空闲回收；挂起中的子任务现场在 checkpoint，进程可回收、按 thread_id 换进程续跑。
- @mention 因而不是新机制，只是 inbox 事件的一种——worker 从不阻塞 → 天然可被 @mention（见 §4）。
- **统一视角**：主 agent 与 worker 是同一套 resident runtime，差异只在主侧额外挂人工审批面板 → "worker 复用同一套 agent 代码"升格为"复制运行时"。

由此，DAG 的拓扑调度与并发**与形态解耦**：两种形态下都是主 agent 按依赖就绪**顺序/并行调度 worker**，差异只在 worker 是进程内子图还是跨进程 server。

**升级判据（A → B，出现才做、不预做）**：候选 B 只在 A 实际撞到下述瓶颈之一时才做——
- 故障隔离：worker 崩溃/死循环会拖垮整个运行与审批会话，需要进程级隔离；
- 结构性最小权限：同进程 worker 与主共享全部进程能力（无进程级沙箱边界），隔离要求变强时；
- worker 对外复用：单任务执行要暴露为独立 MCP 服务、被其它 orchestrator 调用；
- 并发压力：目标并行度把单进程/单事件循环推到资源极限（agent 为 IO-bound，通常不是瓶颈，须实测而非预设）。

升级即按候选 B 重做 worker 侧，并先解 §6 回传审批 `[待验]`。在此之前 A 是唯一工作形态。

## 6. 统一审批闸门

审批有两个正交轴，须分别定：

1. **谁审批（已钉）**：审批单点留在主侧。worker 只做读/检索/规划/提案，不自行放行写/命令。
2. **审批粒度/时机（A 形态下默认=主侧逐次 interrupt；计划级覆盖为后续可选放松，仍待定）**：
   - **逐次 interrupt**（现状单 agent 纪律）：每次敏感调用照常打断、y/n。
   - **计划级审批覆盖**：开工前把整张 DAG/子任务表审一次，视作对"各 worker 在其子任务范围内执行"的授权，执行期写/命令不再逐次打断、做完再汇总。

对应到层次：
- 层次 1：开工前审批"**计划/子任务表**"（DAG 的依赖图，或并行/对话的任务清单）——人类认可拆法与授权；
- 层次 2：执行中的**敏感工具调用**。当前形态 A：worker 不授写/命令 → 敏感动作执行权天然在主侧 ToolNode，走主侧逐次 interrupt（是否把主侧代执行纳入层次 1 的授权范围可另定）；将来若转 B、worker 内要直接写/命令 → 必须先建"回传通道"（见下）。

**回传审批通道（B 形态用；候选已具体化，取代原 `[待验]` 空位；2026-09-06）**
- 运输：worker→主 走**应用层 HTTP**（主侧常驻端点、入审批队列）；**不塞进 MCP**——MCP stdio 只承载"主→worker 工具调用"，审批是反向语义、非 MCP 范畴。
- 时序：worker driver 把 `__interrupt__` 序列化 POST 主侧 → 主侧入队（带 worker_id / sub_task_id / 动作渲染，复用 `format_tool_approval`）→ TUI 面板逐条 y/n（可"批准本子任务剩余全部"降聊天频率）→ 回 `{approved: true|false}` → worker driver 以 `Command(resume={approved})` 恢复同一条 run。
- 拒绝语义与单 agent 一致：`{approved:false}` 时回灌 `[approval_denied]` 悬空 tool_call（worker 复用主侧同一套 review/nodes 逻辑，只换 resume 值的运输方式）。
- 必须钉死：请求关联（request_id / worker_id / interrupt id ↔ resume map）、审批上下文展示、超时与会话关闭协议（主侧退出 → 广播取消，worker 优雅失败而非僵尸）、信任边界（绑定 127.0.0.1 + token，回执只发给登记过的进程）。
- **当前 A 形态可完全绕开本通道（仅升级 B 才需要）；B 若走"派发即授权"粗粒度档也不需要它（§6 层次 2）。**

**统一审批视图（2026-09-07 决策；覆写"回传仅 B 用"的二分，主/子 interrupt 对齐）**

interrupt 的契约 = **阻塞图、不阻塞进程**（LangGraph 原生）：run 停在 checkpoint、事件循环自由。现状 main.py 收到 `__interrupt__` 后内联等人 y/n，把进程占死——主 agent 等审批时 worker 的审批只能排队（两套审批处理互相阻塞）。main 侧事件化后：

- 主 agent 自身 interrupt 也改走**事件**（不再内联等待）：park run → 发 `ApprovalPending` → 决定事件到了 → `Command(resume)` 续跑。由此主 agent 与 worker 的 interrupt **语义对齐**，审核收敛为**同一 `decide()` 逻辑 + marker 路由**；
- marker 只承载**来源 + 回程路由**：`{kind: graph_run | inbox, resume_ref: thread_id | approval_id, 展示}`——`graph_run`（本地 park 的 run）→ `Command(resume)`；`inbox`（跨进程 worker）→ `queue.complete` + HTTP 长轮询取回。**不掺审批策略**（放行策略留 review 配置层 / tool.json）；
- 未来 worker 若转 in-process（候选 A），只是多一种 marker `kind`，决定与 resume 机制原样复用，无需第二套审核实现；
- 上面"层次 1/2"仍是授权粒度档的框架：逐次回传 = 层次 2 在统一视图下的形态；计划级覆盖（层次 1 一次授权）仍为可选放松、独立决策，不因本视图自动成立。

**注意（对照单 agent 现状）**：CLAUDE.md 的现状纪律是"每次敏感调用仍 interrupt、不靠 prompt 放松"。多 agent 若选计划级覆盖，是对审批粒度的**刻意放松**，须作为独立安全决策显式记录——包括如何界定 worker 越界（只在其 produces 范围内授权）、DAG 上界外的操作仍回逐次批。

## 7. 归并与交付物验证

- 节点/子 agent 完成先过**交付物验证门**（produces 存在非空 + 结论非空），否则 failed → 重试/降级；
- 依赖未就绪的 DAG 节点置 blocked 不上跑；
- 主 agent 汇总时**重读文件真值**校验自述；
- 某分支失败不拖垮整体：主 agent 决定重试 / 换法 / 告知用户。

## 8. 参考借鉴：claw-office（`E:\code\claw-office`）

读过的结论：roles 即 JSON 数据 + `orchestrator.py` asyncio 守护 + 共享消息流 + DAG/@mention 双模式；**没有实现真人工审批**（`waiting_user` 只在文档/恢复代码）。

| claw-office 做法 | 对本设计的启发 |
|---|---|
| PM 输出 JSON 任务图，运行时解析调度 | DAG 模式：依赖图 JSON 作**人工审批对象**，通过再拓扑调度（§3） |
| 任务 produces/consumes + 交付物验证 | 交付物验证门（§7）。它 `verify_delivery()` 半成品（全仓库无定义会 NameError），搬要自写 |
| worker 产出后回到 PM 裁决 | 对应"归并点人工审批/主 agent 汇总"位（§3/§6），PM 裁决换人工 y/n |
| 三层 JSON 提取（json 块→raw_decode→验证） | 主 agent 输出 JSON 任务图时复用（`orchestrator.py:_extract_json_block`） |
| @mention 群聊 + forced_actor | @mention/A2A 模式可参考其"按角色路由、谁发言谁 step"；但我们不共享全量消息流，走显式通信（§4/§1） |
| 在 OpenClaw 基座上做编排 | 佐证"agent 可由 LLM 底座暴露、被上层编排调用"——我们映射为 worker 可做成 MCP 服务（§5 候选 B，升级路径）；但 OpenClaw 侧无真审批，我们的 interrupt 闸门仍是差异化护栏 |

## 9. 分期落地（实现顺序，非优先级）

- **P0 · DAG**（worker 形态 = 候选 A · 同进程子图，见 §5；B 为出现瓶颈后的升级，本阶段不做）：
  1. 底层工具共享——**前提已大体落地**：`mcp.py` 按工作区的工具缓存 + 单飞锁已实现（当初为 langgraph dev 提速）。P0 不是从零抽，而是验证"主 + worker **bind 同一批工具对象**、不再各自拉起子进程"；
  2. `create_dag`-类工具（带 deps/produces/consumes，仿 create_plan，tool.json 归 state/plan 通道）产出依赖图 → interrupt 审批计划表；
  3. worker 执行单元 = 编译子任务图（收束子任务 prompt + 只读工具子集，§5 候选 A）；先做"审批留主侧"的最小版：worker 只读/提案，敏感动作回主侧（§6）。
     - **isolated 首步已落地（2026-09-06）**：只读"分析师"worker MCP server——`mcp_service/sub_agent.py` 即 **worker 进程侧的家**：运输层（`run_subtask(task)`）+ worker agent 逻辑（`WORKER_SYSTEM_PROMPT` / `read_only_tools` 只读过滤 / `authorize_node` + `build_subtask_graph`：LLMNode + 免审放行 + ToolNode，无 checkpointer）同收一处；主 agent **不 import 它**，只经 spawn + `run_subtask` 跨进程调用。独立进程/独立消息栈跑只读子任务并返回结论；**未接 main、无写工具**。它铺平"写工具 + §6 审批回传"后续里程碑（届时注意：sub_agent 被主 agent 以 cwd=工作区拉起时读不到项目 .env 的 CHAT_*，需注入 env 或改 cwd）。
     - **worker 审核门 + 审批回传闭环已接（2026-09-08）**：worker 默认工具子集从"只读检索"放开到
       只读检索 ∪ 联网四件套（`read_only_tools` → `worker_tools`；web_search 系 need_review:true
       触发审批）；图删 `authorize_node`（免审），改复用主图 QueueNode/ReviewNode（ReviewNode 加
       `log` 开关——worker 是 stdio MCP server、stdout 即 JSON-RPC，不能 print）+ **InMemorySaver**
       checkpointer（worker 每次 spawn 只跑一条 run_subtask，interrupt 挂起 → `Command(resume)`
       续跑都在同进程同 thread 内完成，内存检查点足够、不落盘）；`run_subtask` 内新增 driver：
       遇 `__interrupt__` 把中断值 POST 进主侧统一 broker（ApprovalInboxServer）、长轮询等人工
       决定后 resume——**批准即执行、拒绝回灌 [approval_denied]**。主侧 run_tui 事件驱动，在
       dispatch 进行中也能并发服务 worker 审批（new_pending 唤醒 drain）。**仍不下放工作区写/命令**。
       闭环测试 tests/test_sub_agent_approval.py（批准/拒绝/只读休眠三向）；主侧整链另由
       tests/test_dispatch_worker_approval.py 覆盖（OrchestrateNode → dispatch → worker 审批 →
       resume → 汇总回主）。同日重构：worker 子图迁 app/agent/graph.py::get_sub_agent_graph、
       mcp_service/sub_agent 瘦身为 run 壳，见文末"模块落点"。
     - **HTTP 传输层已通（同日）**：`mcp_service/http_agent.py`——streamable-http transport 的最小 MCP server，`chat(task)` 一次性 LLM 回复（无工具/状态）；验收 = 起服务 → HTTP 客户端 ListTools + CallTool 拿到回复（已手动验证）。`SUBAGENT_HTTP_HOST`/`SUBAGENT_HTTP_PORT` 可覆盖监听地址。
     - **spawn-per-task 派发已落地（同日）**：主 graph **不静态绑 worker**（避免"先启动服务/最多一个子 agent"），而是加编排工具 `dispatch_subtasks(sub_tasks)`（收在 `app/agent/tools.py`，与 create_plan 等编排工具同住；source=dispatch 走 orchestrate 层）：每次调用按任务 `asyncio.gather` **现场 spawn N 个独立 sub_agent 子进程**跑 `run_subtask`，各独立上下文、干完即回收，返回汇总结论；`workspace` 以 `InjectedToolArg` 对模型隐藏、由 OrchestrateNode 注入。worker 子进程 cwd=项目根以读 .env 的 CHAT_*。测试 tests/test_dispatch.py。
     - **主侧审批控制面落地（同日，idle-only；后收窄为纯请求队列）**：主 agent 保留 TUI、不做常驻后端。`app/main.py` 输入解耦（阻塞 `input()` → 单 stdin reader 线程 + asyncio.Queue，循环不再被键盘占死）；`app/approval_inbox.py` 合一**待审请求队列 ApprovalInbox + 薄 HTTP 收件箱 ApprovalInboxServer**（starlette/uvicorn 同事件循环任务，POST /requests 入队、GET /requests/{id}?block=1 长轮询、POST /requests/{id}/decision 回填；不 import app.agent）。**语义定界**：本模块只做队列 + 送达（enqueue/complete/wait），不做审批判定——判定单点在图的审核节点（§6）；complete 只记录审核方给的决定并唤醒等待者。TUI **idle 时**能收/审/回 worker 待审请求（复用 `format_tool_approval` 面板 + y/n）；**dispatch 中途弹审批与 worker 写工具未做**（留后续里程碑，届时复用 queue 的 enqueue/wait 原语）。测试 tests/test_approval_inbox.py。（本段的输入解耦 / 收件箱 / 审批判定已随 2026-09-07 main 事件化重构迁入 `app/tui`，并升格为"主 agent 自身 interrupt 也入同一 broker park"的统一审批，见 §6 统一审批视图与 §10 已定(2026-09-07)。**2026-09-10 再分家后的当前位置**：收件箱/队列 → `app/platform/approvals.py`，判定面 → `app/tui/panels.py` + `TerminalUI.decide`，输入 → `app/tui/input.py`，事件循环 → `app/platform/loop.py`；`app/tui/{driver,approval,approval_inbox}.py` 已删除。）
     - **HTTP interrupt 回传演示已通（2026-09-07）**：`mcp_service/http_agent.py` 在 `chat` 之外
       新增 `request_approval(description, tool_name, tool_args)`——模拟子 agent 在敏感动作前
       interrupt：用 httpx 把一条 ApprovalRequest POST 进主侧薄收件箱（`ApprovalInboxServer`），
       长轮询 GET /requests/{id}?block=1 等人工决定后返回 批准/[denied]。为支撑它，`app/main.py`
       把内联的"渲染面板 + 收 y/n"抽成可复用判定单元 `decide_approval(value, out_q)`（主图
       interrupt 与收件箱 worker 请求两处共用；判定权威仍在人）。**仍无真实写/命令工具、无
       worker driver、主侧仍 idle 裁决**——本步只证明 §6 回传"入队→判定→httpx 回决定"最小回路
       能通；写权限下放与逐次回传留后续里程碑。测试 tests/test_http_agent.py
       （2026-09-08：http_agent 与其测试已删，同回传现由 tests/test_sub_agent_approval 覆盖）。
     - **main 事件化重构（2026-09-07 定；`[已落地]`）**：agent 图作为 main 的**模块而非全部**、main 改事件驱动壳（run 可 park）；主 agent 自身 interrupt 改走事件与 worker 对齐（§6 统一审批视图：ApprovalPending + marker 路由）。代码落点 `app/tui` 包（`app/main.py` 保留为入口、`python -m app.main` 不变）；**2026-09-10 进一步分家为基座 `app/platform` + 终端前端 `app/tui`**（见文末"模块落点"）。顺带消除"图内联等 y/n 占死进程、worker 审批被推迟"的互阻塞；**worker 审核门已于 2026-09-08 接上**（见本节上一条），**写权限下放**仍留后续里程碑。
  4. 拓扑调度 + 交付物验证门（自写）→ 归并。
- **P1 · 并行分发**：独立子任务并发跑（复用同底座/同 checkpoint 分工或轻量并发段）。
- **远期 · @mention / A2A**：agent 间消息/协议；跨 runtime 时再评估远程调用。
- **P2 · 评估接线**：evaluation 加"子任务数 / 并行度 / 审批次数 / 成本"断言，量化各模式是否真带来接力收益。

## 10. 待定 / 待验

**已定（2026-09-07 · 统一审批语义 + main 事件化）**：
- 跨进程审批的**决定往返最小回路已闭环并测试**（worker 侧审核点 → HTTP POST 收件箱 → `decide_approval` → `queue.complete` → 长轮询取回；当时 test_http_agent 批准/拒绝两向，2026-09-08 该模块已删，同回传现由 test_sub_agent_approval 覆盖）。但那是**运输层**——worker 图仍未接审核门（worker 只读、`authorize_node` 免审放行，无 `need_review` 工具），见 §9 P0。（2026-09-08 已把 worker 审核门接上、并放开联网工具触发审批，
   本条为当时旧状态留档，见 §9 同节 bullet。）
- **main 侧改事件驱动壳、agent 图作为其一个模块**：run 可 park——interrupt **阻塞图、不阻塞进程**；主 agent 自身 interrupt 也改走事件（不再内联等 y/n）。主/子 interrupt 语义对齐，审核收敛为**同一 `decide()` + marker 路由**（§6 统一审批视图）。
- 落地：`app/tui/` 包骨架 = main 重构的家；`app/main.py` **保留为程序入口**（`python -m app.main` 不变）。（2026-09-10 更新：**家已分家**——基座 = `app/platform`（调度/park-resume/broker/runtime/命令），`app/tui` 只留"取输入 + 渲染"；见文末"模块落点"。）
- **worker 形态本次不动**（维持 spawn-per-task 形态；2026-09-08 起 worker 已可联网调查并触发
  审批，工作区写/命令仍未下放）；事件化 main 令候选 A（进程内 worker 子图）变便宜 → §5 A/B 分叉重开，留单独决策。

**已定（2026-09-06）**：worker 形态**先 A 后 B**——起点同进程子图（P0/P1 载体）；同进程出现瓶颈（故障隔离 / 最小权限 / 对外复用 / 并发压力，判据见 §5 末尾）才升级 worker=MCP server。B 落地所需的"回传审批"已非空位：通道有具体候选（§6 HTTP 控制面），且 B 若要绕开它可走"派发即授权"粗粒度档。代码现状不动（`mcp.py` 工具缓存已就绪，仅待 P0 验证 bind 同一批对象）。

**方向观察（同日讨论）**：worker 若定位为"自主写/改/验"（持有写/命令工具），与 A 的"worker 只读、执行权留主侧"结构性不兼容，讨论持续向 B（常驻运行时 + 回传审批，§5/§6）收敛。是否重排 A/B 顺序、B 的审批走"逐次回传"还是"派发即授权"，待 checkpoint 持久化决策（sqlite vs 内存）后再定。

- [ ] **审批粒度**：逐次 interrupt vs 计划级覆盖（DAG 表一次授权）；若选覆盖，定 worker 越界判据与 DAG 上界外操作的回退逐次批（§6）。
- [ ] **三模式如何被选中**：主 agent 依任务耦合度选型（一次请求是纯独立 / 有依赖 / 需对话），输出"模式 + 任务清单"给人类审批。
- [ ] **子任务图 schema 落地**：`create_dag(tasks:[...])`？审批对象是它。tool.json source 归 state/plan 通道；注意 `create_dag` 应 `need_review: true`（整表作审批对象），与现有 `create_plan` 系（均 false）方向相反。
- [ ] **节点 = "worker 子任务节点"形态**：一个 DAG 节点是一次 agent 回合还是允许内部多轮工具循环；各节点上下文怎么隔离/注入（形态 A 下 = 独立消息栈 + 收束 prompt + 只读工具子集；指令 / notes / 产物指针注入）。
- [ ] **交付物验证判据**：只查文件存在/非空，还是要"改动已生效"更强验证（警惕 `echo ok` 蒙混，见验证门既有讨论）。
- [ ] **worker 提示词**：复用现 SYSTEM_PROMPT 还是给"子任务专用"更收束系统提示（形态 A 的裁剪工具子集须与提示词配套）。
- [ ] **工具共享验证（前提已大体落地）**：`load_mcp_tool` 缓存已在 `mcp.py`（为 langgraph dev 提速所加）；P0 验证主 + worker bind 同一批对象、不再各自拉起子进程。
- [ ] **@mention/A2A 通信层**：消息/协议形态、是否跨 runtime（远期再定）。

**传输形态（2026-09-08 定）**：worker 当前**继续走 stdio spawn-per-task**，HTTP 不在此刻做。worker 子图
（`app/agent/graph.py::get_sub_agent_graph`）作**可复用基座**保持传输无关；待 DAG/@mention 落地时，worker
再切 **streamable-http MCP**（届时新建 http 入口 import 同一子图，只换 transport + host/port + 常驻
生命周期）。HTTP 是既定未来方向，但**不与当前 stdio 形态并行做**——在 DAG/@mention 阶段统一切。

**模块落点（2026-09-08 定稿；2026-09-10 补基座与命令层）**：worker 子图归 **app/agent**，mcp_service 只留 server/run 壳；**主程序侧已分家**为基座 `app/platform` + 终端前端 `app/tui`：

- **基座 `app/platform/`**（图外那层，不 print/不读 stdin）：`loop.py::AgentPlatform`（主事件循环：drain 待批 → 等 turn → 等输入）、`approvals.py`（统一审批 broker + 薄 HTTP 收件箱）、`turn.py`（drive_turn park/resume + `_race`）、`runtime.py`（按 (工作区,会话) 装配 db/checkpointer/图）、`ui.py`（UI 协议）、`commands/`（控制面命令：`/init`、`/help`、`/list session`、`/new session`、`/session <id>`）；
- **终端前端 `app/tui/`**（只取输入 + 渲染）：`input.py`（stdin 线程+pump）、`panels.py`（终端渲染素材）、`ui.py::TerminalUI`（实现 UI 协议）、`runner.py::run_tui`（`app/main.py` 只调它）。将来的 web 前端 = UI 协议的另一个实现，复用同一基座；
- worker 子图（本节以下各条）：
- 子图构建 → `app/agent/graph.py::get_sub_agent_graph(read_tools, workspace_path, model=None,
  review_log=False)`：复用主图 QueueNode/ReviewNode(log)/ToolNode/LLMNode + InMemorySaver compile；
- worker 人设 → `app/agent/prompt.py::WORKER_SYSTEM_PROMPT`；可用工具子集 →
  `app/agent/tools.py::worker_tools`（工作区只读检索 ∪ 联网四件套）；
- `mcp_service/sub_agent.py` 瘦身为 **server / run 壳**：FastMCP(stdio) 入口 + `run_subtask_impl`
  （建 state / 跑图 / 提结论）+ 审批回传 driver（interrupt → HTTP 收件箱 → resume）；
- .env 耦合如何不扩散：`app/agent/__init__` 顶层 import graph → model（get_settings）→ **import
  app.agent.* 强制要 .env**，故 mcp_service/sub_agent 对 app.agent **全懒加载**（run_subtask 内才
  import get_sub_agent_graph / worker_tools / load_mcp_tool），server 模块 import 阶段仍无需 .env；
- `mcp_service/http_agent.py` **已删（2026-09-08）**：演示使命完成，HTTP 审批回传已由 worker 内嵌
  客户端承担（tests/test_sub_agent_approval、tests/test_dispatch_worker_approval 覆盖）；streamable-http
  模板待到 DAG/@mention 阶段按需重建。
- 主 agent 仍不 import sub_agent——进程边界靠 spawn，不靠包位置。

## 与现有模块的关系（改动面提示）

- `app/agent/mcp.py::load_mcp_tool` / `_build_servers`：按工作区缓存 + 单飞锁**已落地**；P0 是验证主 + worker **bind 同一批工具对象**，不再各自拉起子进程（§5 候选 A / §10）。
- `app/agent/graph.py`：主 agent 侧新增"DAG/并行任务编排 + 归并"节点；节点执行复用现有 LLM/Review/Tool 回路。
- `app/agent/tools.py` + `tool.json`：`create_dag` 类工具登记（source 进 state/plan 通道）。
- `app/main.py` TUI：审批面板需展示"计划/子任务表"这一层（不只单工具）。
- `docs/`：与 CONTEXT_ENGINEERING（只流结论）配套；EXCEPTION_DESIGN 沿用。
