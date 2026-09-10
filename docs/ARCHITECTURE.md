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
7. **基座不与前端耦合**：基座只经 `app/platform/ui.py::UI` 协议（三个方法：`emit` 渲染事件 / `read_line` 取一行输入 / `decide` 就一条待审请求问人）与外界说话，事件形状在 `app/schema/ui_schema.py`。基座**不 print、不读 stdin**；前端（`app/tui` 终端 / 将来的 web）只实现协议、不碰调度。**这层缝是为第二个前端而设**：换前端不该重写 run 生命周期与 park/resume 调度。`[已落地 · 终端一个前端]`
   - 推论：**用户命令（`/init`…）是基座的控制面事件**（`app/platform/commands.py`），在"起 turn 之前"介入 `loop`——前端只负责把输入交上来、把 `Notice`/事件渲染出去，不解析命令。

## 3. 分层（放在上面的东西）

```
┌─────────────────────────────────────────────────────────┐
│  前端（终端 TUI = app/tui；将来的 web 同协议）              │
│  · 只实现 UI 协议：emit 渲染 / read_line 取输入 / decide 问人 │
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
│  · 会话 checkpoint（按 workspace/session 分目录）[设计]      │
│  · AGENT.md（项目画像）/ memory（执行约束）    [设计]        │
└─────────────────────────────────────────────────────────┘
```

- 记忆/会话/画像层让事件基座 + 图能"跨会话认出这个项目"，见 [[docs/LONG_TERM_MEMORY]]。
- 多 Agent（并行分发 / DAG / @mention）就长在事件基座上：派发是事件、worker 审批是事件、归并是事件——见 [[docs/MULTI_AGENT]]。

## 4. 由此定下的纪律

- 协调逻辑（调度、审批、消息路由）**不进 LangGraph 节点**，放事件基座/其 handler。
- 图对外只通过 driver 暴露：`ainvoke/stream` 包在 driver 里，图不直接面向事件循环。
- 新增"agent 能力"先问：它是**图内的一条边/一个节点**，还是**基座上的一类事件/一条 run**？前者是小步执行、后者是整体编排——两者用错会拧巴（见 MULTI_AGENT 三种编排模式的对偶）。
- **基座与前端**（§2.7）：基座（`app/platform`）**不许** `print` / 读 stdin / `import app.tui`；前端（`app/tui`）**不许**碰调度与 run 生命周期——它只实现 `UI` 协议。新增前端能力时先问"这是渲染/取输入，还是调度？"，后者属于基座。用户命令（`/init`、`/session`）属**基座的控制面事件**，落 `app/platform`；前端只渲染它们的输出。
