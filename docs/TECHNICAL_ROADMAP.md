# Theta 技术路线与实现概览

> 核对日期：2026-10-10。本文综合 README、设计文档和当前代码，描述项目现状及后续演进方向。
> 状态口径以代码和测试为准；设计文档中保留的目标态、历史记录不等于已实现能力。

## 1. 项目定位

Theta 是一个基于 LangGraph 的编码助手。模型负责理解任务并提出工具调用，Theta 负责组织会话和工具执行；在文件写入、删除、终端执行、联网检索等操作执行前，通过人工审批闸门暂停并等待用户决定。

项目的核心设计不是“把所有能力塞进一个 agent 图”，而是分成两层：

- **事件基座**负责一条图运行之外的事情：会话生命周期、输入循环、审批排队与派发、运行时资源装配，以及本地与 worker 之间的事件通信。
- **LangGraph**负责单次 agent run 的状态流转、工具调用审核和执行。图中的 `interrupt` 是基座能够 park/resume 一条 run 的事件入口。

当前主路径是 Python TUI；基座与 UI 通过协议分离，未来可以接入其他前端而不重写 agent 生命周期管理。

## 2. 当前架构

```text
用户
 │
 ▼
app/tui ── UI 协议 ──► app/platform（事件基座）
                           │
                           ├── run 生命周期 / park-resume / 统一审批 broker
                           ├── 会话 runtime / SQLite checkpoint / MCP 运行体管理
                           └── 驱动 LangGraph 主图
                                   │
                                   ├── LLMNode：模型推理与上下文装配
                                   ├── ReviewNode：工具策略判定与 interrupt
                                   ├── OrchestrateNode：计划、记忆、技能等 state 工具
                                   ├── ToolNode：执行已批准的 MCP 工具
                                   └── CompactNode：轮末历史折叠
                                         │
                                         ├── mcp_service 子进程
                                         └── skills/<name>/server.py（按需加载）

resource/<ws_key>/                 工作区私有运行数据
├── sessions/<session_id>/agent.db  会话 checkpoint
└── memory/memory.md                跨会话长期记忆
工作区根下的 AGENT.md              项目画像，随工作区仓库管理
```

### 2.1 分层职责

| 层 | 主要位置 | 职责与边界 |
| --- | --- | --- |
| 入口 | `app/main.py` | 薄入口，启动 TUI 并转换顶层配置错误 |
| 前端 | `app/tui/` | 输入读取、面板渲染、实现 `UI` 协议；不负责 run 调度 |
| 事件基座 | `app/platform/` | 输入/命令循环、run 驱动、审批 broker、会话 runtime 和 MCP 宿主生命周期；不读 stdin、不直接 print |
| Agent 图 | `app/agent/` | LangGraph 状态图、提示词、模型节点、工具策略与 agent 状态工具 |
| MCP 服务 | `mcp_service/` | 文件、终端、联网、派发 worker 等进程隔离的工具服务 |
| 资源层 | `app/resource/` | 项目根与落点、画像、记忆、技能、图片等外部资源的读取与规范化 |
| 数据契约 | `app/schema/` | Agent、审批、UI、会话等跨模块纯数据结构 |
| 评估与测试 | `evaluation/`、`tests/` | 分别验证模型行为与确定性机制 |

依赖方向的关键约束：基座不依赖 TUI；`app/resource/` 不依赖 `app.agent`；图通过宿主侧 MCP 边界获取工具。`app.agent` 顶层读取配置，因此基座仅在创建图时懒加载 agent 模块，避免基础设施导入时就要求模型配置。

### 2.2 主图与一次工具调用

主图由 `app/agent/graph.py::get_main_agent_graph` 构造，调用方负责编译并挂载 checkpointer：

```text
START → llm_node
           ├─ 无工具调用 →（超预算时 compact_node）→ END
           └─ 有工具调用 → queue_node → review_node
                                   ├─ state/编排工具 → orchestrate_node ─┐
                                   └─ 普通 MCP 工具 ─────► tool_node ─────┴→ llm_node
```

1. `LLMNode` 在每轮组装系统提示和工作区上下文，按当前可用工具调用模型。
2. `QueueNode` 将模型的 `tool_calls` 拆入待审核队列，并重置单轮审批/提问状态。
3. `ReviewNode` 逐条读取审批策略。需要人工决定的调用通过 LangGraph `interrupt()` 挂起；获批的调用根据 `source` 路由到编排节点或普通工具节点；拒绝的调用会回灌与原 `tool_call_id` 配对的拒绝回执。
4. `OrchestrateNode` 处理计划、提问回执、笔记、长期记忆和技能加载等 agent/state 操作；普通 MCP 工具由 `ToolNode` 以异步 `ainvoke` 执行。
5. 工具回执以 `ToolMessage` 写回历史，模型继续推理，直到输出最终答复。

worker 子图复用 LLM、审核和工具执行逻辑，但没有主图的计划/记忆/技能编排与 compact；其能力由实际注册的工具子集限制。

## 3. 技术栈

| 领域 | 当前采用 | 用途 / 说明 |
| --- | --- | --- |
| 语言与运行时 | Python 3.13+（`pyproject.toml` 声明 `>=3.13`） | 主程序、MCP server、worker、测试与评估 |
| Agent 编排 | LangGraph 1.x | StateGraph、interrupt/resume、checkpoint、主图与 worker 子图 |
| LLM 接入 | LangChain、`langchain-openai` | 通过 OpenAI 兼容 Chat Completions 接入模型；模型可通过配置选择 |
| 工具协议 | MCP、`langchain-mcp-adapters` | MCP server 以 stdio 子进程提供工具，宿主适配为模型可调用工具 |
| 会话持久化 | SQLite、`aiosqlite`、`langgraph-checkpoint-sqlite` | 为每个工作区会话保存 LangGraph checkpoint |
| 配置与校验 | Pydantic、pydantic-settings、python-dotenv | `.env` 配置解析、结构化数据和输入约束 |
| 联网搜索 | Tavily 的 LangChain 集成 | Web 搜索、URL 抽取、网页抓取和深度研究；无可用 Key 时跳过该 server |
| worker 审批通信 | Starlette、uvicorn、httpx | 本地主机 HTTP 收件箱；worker 提交审批请求并等待决定 |
| TUI | Python 标准库为主 | 单 reader 输入线程、异步 pump、纯渲染面板；`rich` 是可选依赖 |
| 测试与开发 | pytest、LangGraph CLI | 确定性机制测试、LangGraph 本地开发入口 |
| 依赖管理 | `pyproject.toml`；README 推荐 uv | 核心依赖和可选扩展依赖分组 |

**可选依赖与当前路线：**

- `providers`：其他模型供应商适配。
- `docs`：PDF/JSON 文档处理依赖，当前核心流程未将其作为必需项。
- `monitoring`：Prometheus 指标依赖，属于扩展方向。
- `ui`：终端 UI 增强。
- `web`：FastAPI/uvicorn，Web API/UI 规划依赖；目前主交互界面是 TUI。
- `vector`：Chroma 向量检索依赖。代码库虽预留依赖组，工作区代码仍以实时文件工具读取为主；README 将仓库内代码 RAG 列为搁置，而非已实现功能。

模型密钥等核心配置通过项目根 `.env` 读取；Tavily Key 可为空。GitHub 技能 Key 是可选项，缺失时不应影响核心启动。切勿把“项目声明了依赖”误写成“当前功能已经接通”。

## 4. 关键实现细节与设计约束

### 4.1 审批是图执行前的闸门，不是工具内部确认

- 内置工具审批策略集中在 `app/agent/tool.json`，字段包含是否需要审批、工具来源及 worker 下放规则；终端命令的只读免审判定另由 `command_policy.json` 提供规则数据。
- `ReviewNode` 是审核与提问的唯一图内落点。普通敏感工具在执行前中断，基座将请求放入统一 broker，前端决定后基座用 `Command(resume=...)` 恢复图。
- 本地 agent 和远程 worker 的审批共用 broker。HTTP 收件箱只负责排队/回填；是否批准由人决定，不由 broker 或 worker 决定。
- LangGraph 恢复会重跑被中断节点。节点中断前必须保持无外部副作用；写文件、启动进程等实际执行放在审核通过之后。该约束也决定 `ask_user` 的中断不能嵌在可能产生副作用的编排执行里。
- 拒绝不是简单丢弃工具调用：系统用对应的 `ToolMessage` 关闭原调用，避免历史中留下模型 API 无法接受的悬空 `tool_call`。

**安全边界必须如实理解：**`mcp_service/file_io.py` 对所有文件操作（包括读）做工作区路径解析和越界校验，覆盖符号链接解析后的路径；但 `run_command` 是通用终端能力，目前没有操作系统级工作区沙箱。命令策略只决定特定只读命令能否免审，不能限制获批命令的真实影响范围。`sudo` 在 spawn 前硬拒绝，但这不等同于完整的进程隔离。

Git 没有独立 MCP server；Git 操作经 `run_command` 执行。只读 Git 子命令可按命令策略免审，写命令仍须审批；`git -c` 等会改变安全语义的选项不进入免审路径。

### 4.2 MCP 运行体生命周期

- 核心工具服务按 stdio 子进程启动，工作目录设为目标工作区；必要时通过 `PYTHONPATH` 找到 Theta 包。
- 运行体按 **(工作区, 会话, server)** 隔离并在首次真实调用时懒启动，避免终端进程状态跨会话泄漏。
- 节点在各自 Task 中运行，而 anyio 的 cancel scope 要求 MCP 会话创建与关闭处于同一 Task。因此由 owner task 独占会话生命周期；调用方通过队列投递调用，不能在任意节点 Task 里直接关连接。
- 宿主返回的是不绑定会话的工具 shim：工具调用时再向 owner task 请求执行。这样切会话关闭旧池后，缓存的 shim 不会持有已关闭连接。
- 传输层故障会触发运行体重建并重试一次；业务工具错误由 MCP `guard` 转成结构化失败结果，不应混同为连接故障。
- TUI 切会话/退出及 evaluation 任务结束时负责关闭运行体。LangGraph 零参开发入口的运行体生命周期与 session 隔离仍有已知差异，见“待演进事项”。

### 4.3 文件、终端和工具错误

- 文件服务在 import 时读取并校验 `WORKSPACE_PATH`；相对路径以工作区为基准解析，解析真实路径后再检查是否仍位于工作区内。
- 文件编辑使用锚定文本替换；整文件覆盖、创建、删除等仍各有独立工具和审批策略。
- 终端服务负责受管命令与常驻进程的启动、读取、等待、终止；执行/启动/终止操作需要审批，命令还要求提供面向用户的解释。
- MCP 工具通过 `mcp_service/utils.py::guard` 将可预期领域异常翻成 `ToolResult`；意外异常记录 traceback 并标记为内部错误。模型可见的结果归一化集中在 `app/platform/tool_results.py`，避免各调用方对 MCP 返回形状作不同解释。

### 4.4 会话、资源与记忆

- 每次启动生成会话 ID；会话 checkpoint 懒创建于首次交互，数据库位于 `resource/<ws_key>/sessions/<session_id>/agent.db`。
- `ws_key` 由工作区绝对路径的可读 slug 和路径哈希构成，避免路径清洗导致两个不同工作区共用私有状态。
- 项目画像 `AGENT.md` 放在工作区根目录，描述“项目是什么”，在图实例中首次读取并 memo；通过 `/init` 生成或更新。
- 长期记忆 `resource/<ws_key>/memory/memory.md` 是工作区私有、跨会话持久化的执行约定和偏好；每轮实时读取注入，可通过 `write_memory` / `read_memory` 管理。
- 会话 `notes` 存放上下文折叠后仍需按需取回的联网正文；它属于 checkpoint 中的会话状态，不是长期记忆，也不是跨会话语义库。
- 图片通过用户消息中的 `@路径` 在请求构造时读取，并临时附入本次模型请求；base64 不进入消息历史或 checkpoint。worker 不附图。
- Agent、审批、UI、会话等跨边界结构集中在 `app/schema/`；进入 checkpoint 的 state 以 JSON 友好的结构为主。

### 4.5 上下文折叠

上下文折叠分两层，引擎在 `app/agent/compression/`，`app/agent/nodes/` 只留两个 state 适配器：

- **轮末 `CompactNode`**（无条件运行）：折叠最终答复之前、已被消费的工具块（含刚完成的当前轮；无最终答复时退回最后一条 Human 之前）；按 plan epoch/step 分组，每组一条摘要。块内逐调用处置：编排 / 失败 / 被拒调用保留，联网正文归档到会话 `notes`，其余折叠；通过 `RemoveMessage` 原位替换，不拆开 AI tool call 与其对应 ToolMessage。保留回执必须排在折叠回执之前，否则整块保留。
- **轮内 `CompressNode`**：在当前轮工具循环中做 read_file 去重、批级移动窗口（高水位触发、低水位停止）、状态回执变薄，以及旧写操作的大 args 清理（`content` / `old_string` / `new_string` → 带长度指针）；不改 `tool_call_id`。整块墓碑（`tombstone_blocks=True`）可把可重取的旧块整块移除并留摘要，**默认关闭**，等真实会话基线后再决定是否开启。

两者共享 `ToolBlock` 解析与策略表；当前计划单独存在 `current_plan` 并按轮注入，不依赖计划工具回执一直留在历史里。当前机制**不等于完整的长上下文解决方案**：预算仍是字符代理，尚未按 provider 的 token 用量校准；整块墓碑默认关闭（需真实会话的 prefix cache / 行为基线）；prefix cache 与 evaluation 仍未接线。

### 4.6 技能与动态工具

技能是 `skills/<name>/` 下的领域包：`SKILL.md` 提供按需注入的知识；可选 `server.py` 提供工具，存在即为能力型技能。技能通过 `get_skill` 加载、`drop_skill` 卸载，模型上下文只保存已加载名称，正文由宿主在每轮从文件读取。

能力型技能加载时会校验名称、依赖、环境变量白名单、工具声明/审批策略和重名冲突；技能工具策略声明在其 `skill.json`，内置策略仍在 `tool.json`。会话工具集随技能加载/卸载动态变化；模型只在工具集版本改变时重新绑定工具。技能 server 复用 MCP 运行体机制。

当前首个主要技能是 GitHub 能力型技能。其服务提供 GitHub 平台 API 能力；本地克隆/分支等 Git 操作仍由终端命令承担。技能运行体当前没有空闲自动回收机制。

### 4.7 多 Agent 与运行中转向

- `dispatch_subtasks` 是 MCP 派发工具。主 agent 将彼此独立的资料收集任务并发交给独立 worker 进程，worker 完成后返回结论，由主 agent 汇总。
- worker 工具集结构性限制为工作区读取、获准下放的终端操作及联网能力；worker 不写工作区、不管理主计划、不读写主 agent 记忆，也不能向用户提问。
- 需要审批的 worker 操作通过本地 HTTP 收件箱回传到主侧，审批权仍在用户；worker 完成任务后进程回收。
- **DAG 依赖式调度、worker 写权限下放尚未实现。**当前派发适用于并发独立任务，不是通用多 agent 工作流引擎。
- 主 agent 已支持运行中转向：基座将用户输入放入进程内 mailbox，图在下一个 LLM 边界检查信号并提前收口，再把消息作为下一轮标准输入送入；内容本身不直接写入运行中的 LangGraph state。转向不会撤销正在执行的工具批次。
- 图结果目前主要按 run 终态处理；逐 superstep 流式观测/显示（例如更细粒度的过程输出）仍是演进项。

### 4.8 配置与开发入口

- `.env` 固定锚定 Theta 项目根，不随用户启动工作区的当前目录漂移；产品配置经 `get_settings()` 读取。
- 工作区是构图期显式参数：TUI 传启动目录，evaluation 传隔离临时目录；LangGraph CLI 零参入口默认使用项目根下的 `tmp/`。
- MCP 子进程使用显式环境传递。`.env` 经 pydantic-settings 解析的值不会自动写进 `os.environ`，依赖子进程环境的变量需按代码路径显式转发。
- `langgraph.json` 将 `my_agent` 注册到 `get_main_agent_graph_langgraph`。TUI 则通过 `app/main.py` → `run_tui` 启动。

## 5. 验证策略

项目将机制测试与模型行为评估分开：

1. **`tests/`：确定性机制测试**，覆盖文件沙箱、异常 guard、审批路由与端到端审批闭环、会话/checkpoint、MCP 运行体池、技能动态注册、多 Agent 审批回流、运行中转向、图片附加等。
2. **`evaluation/` 层 A：录制回放**，使用已录制的模型输出，仍执行真实图、策略和 MCP 工具，适合低成本验证非模型机制回归；fixture 会检查 prompt/setup 指纹。
3. **`evaluation/` 层 B：真实模型运行**，验证模型对当前 prompt 和工具说明的实际响应。改动 prompt 或工具 docstring 时，回放不能验证模型行为，应使用 live 评估。

涉及沙箱或 MCP server 的测试需按 README 设置 `WORKSPACE_PATH`；Windows/WSL 运行时还需注意变量传递方式。测试前 `.env` 必须满足当前配置模型要求。

## 6. 后续技术路线

以下是结合当前代码、README 路线图和 `docs/TODO.md` 得出的**未完成方向**，并不表示已排期或已做完。

### P1：补齐单轮长任务的上下文收口

- 为工具循环增加运行中 compact，在保留当前未消费工具调用块的前提下，及时折叠更早的完整工具块。
- 扩展 `notes` 的检索与主题召回，并改善“研究结论何时写入工作区/如何 promote”的提示和收尾流程。
- 用 evaluation 量化上下文折叠对任务达成率、轮数、耗时和 token 的影响，再校准字符预算。
- 评估跨轮历史摘要合并，避免用户问答与最终答复长期线性增长。

### P1：明确工作区与命令执行的安全/语义边界

- 评估是否需要真正的终端沙箱或更细粒度的命令执行约束；目前审批是知情授权，并不提供 OS 级隔离。
- 明确“工作区是项目根还是项目容器”的交互与状态归属，避免克隆在工作区子目录时，项目画像、记忆和命令工作目录与实际仓库根不一致。
- 保持策略解析器与 JSON 策略表的职责边界清晰；命令免审的规则应经测试验证，不能把解析器词法约束降格为可随意配置的数据。

### P2：Agent 推理与编排增强

- 在工具调用进入人工审核之前增加有限次数的 reflect-before-act 检查；被否决时将原因交还主模型重想，并用 evaluation 实测收益与额外成本。
- 只有在出现真实依赖任务需求后，再设计 DAG 调度、worker 结果依赖与更细的权限委派；当前独立任务并发模型应继续作为简单默认能力。
- 按实际使用需求评估长期记忆的设备/用户/工作区分层；目前只有工作区私有记忆和随仓库维护的 `AGENT.md`，分层尚未落地。

### P2：扩展交互与运行时可观测性

- 基于现有 `UI` 协议增加 Web UI/API（计划方向包含 FastAPI、SSE 和审批界面），保持调度与呈现职责分离。
- 将 run 驱动逐步改为可观测的逐 superstep 流，支持过程输出、长回合观察与更及时的 UI 更新。
- 根据运行需求扩展 MCP server 和审批策略管理；先明确权限模型与策略校验，再增加运行时可配置能力。

### 尚待评估或暂缓

- 工具返回图片目前受 `ToolResult.content: str` 和文本归一化路径限制，工具读取到的图像还不能作为多模态内容回传模型。
- 向量检索依赖虽有预留，但工作区代码是实时变化的本地真值；README 当前将其搁置，只在工作区外、慢变且可固定版本的语料场景下重新评估。
- 整理 MCP 宿主边界、配置/枚举/数值口径及函数返回值类型，是 `docs/TODO.md` 中记录的工程收敛项；应逐项以实际调用关系为依据，不宜一次性重构。

## 7. 代码与文档导航

- 项目概览、安装运行和命令：[`README.md`](../README.md)
- 设计文档目录及状态标注约定：[`README.md`](README.md)
- 架构原则：[`ARCHITECTURE.md`](ARCHITECTURE.md)
- 上下文工程：[`CONTEXT_ENGINEERING.md`](CONTEXT_ENGINEERING.md)
- 长期记忆与资源布局：[`LONG_TERM_MEMORY.md`](LONG_TERM_MEMORY.md)
- 多 Agent：[`MULTI_AGENT.md`](MULTI_AGENT.md)
- 技能机制：[`SKILL_DESIGN.md`](SKILL_DESIGN.md)
- 当前未完成事项：[`TODO.md`](TODO.md)
- 已完成事项与决策背景：[`DONE.md`](DONE.md)

若本文与实现发生偏差，以当前代码和针对该行为的测试为准；更新技术实现后，应同步修订对应设计文档与本路线图中的状态。