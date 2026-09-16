<div align="center">

# Theta $\theta$

一个基于 **LangGraph** 的编码助手智能体（$\theta$）

**写操作和命令执行永远需要你点头之后才动手。**

![Python](https://img.shields.io/badge/python-3.13+-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-1.x-brightgreen)
![uv](https://img.shields.io/badge/uv-依赖管理-orange)

</div>

---

## 📌 这个项目解决什么问题

大模型很擅长"想要"调用工具，但当你把**文件写入、目录删除、终端命令**这类能力交给它时，最怕的就是它自作主张。$\theta$ 在"模型想调工具"和"工具真正执行"之间插入了一道**人工审批闸门**：安全的读操作自动放行，有风险的操作必须先展示给你看，你确认后才执行。

简单说——**让你放心地把文件系统和终端交给你自己的 AI 助手。**

## ✨ 核心特性

- 🛡️ **知情审批闸门**：写文件、删目录、跑命令前，TUI 展示工具名、参数与模型的**意图解释**，等你按 `y/n`。终端命令强制模型附一句"这条命令要干什么"，命令 + 解释同屏展示，先看意图再核对命令
- ❓ **人机闸门 · agent 提问**：需求不明确时模型可以**停下来问**（`ask_user`）——摆出几个带差别的选项，你选一项、也可以**另外补一段说明**（补充是独立的一段，不是"选项之外的替代品"）。与审批共用同一个闸门骨架；没人回答就记"未作答"，模型按最佳判断继续
- 📋 **计划编排**：模型把任务拆成步骤（`create_plan`），边做边推进状态（`update_plan_step`），计划跨对话轮持久跟踪，不随对话丢失
- 🔌 **MCP 工具即插即用**：文件、终端、联网检索各自是**独立 MCP 服务器**，新增工具只改对应 server，核心 agent 代码不动（**git 没有专用 server**——它一律走 `run_command`，见下面的免审面一节）
- 📁 **文件沙箱**：文件工具（**含读操作**）全部锁在 `WORKSPACE_PATH` 内——相对路径解析 + 符号链接消解后再校验，越界即拒绝
- 🔍 **检索基建**：`read_file` 行号分段读、`glob` 按路径定位、`search_content` 内容检索（大小写 / 正则可配），大仓库不整段读
- 🌐 **联网检索四件套**：`web_search` / `extract_urls` / `crawl_website` / `deep_research`（内置 Tavily）
- 🖥️ **终端进程管理器**：一次性 `run_command`（到点不杀、转入受管句柄）+ 常驻 `start_process` + `process_*` 生命周期管理
- 💾 **会话持久化**：SQLite（`AsyncSqliteSaver`）保存对话、审批队列与计划——**每个会话一个库**（`resource/<工作区>/sessions/<id>/agent.db`），重启后可与历史会话切换续聊（`/list session`、`/session <id>`）
- 🧠 **跨会话记忆 + 项目画像**：工作区私有长期记忆（`write_memory`/`read_memory`，每轮注入）+ 工作区根的项目画像 `AGENT.md`（`/init` 生成，会话首启注入）
- 🧩 **斜杠命令**：`/help` 看全集；`/list session` 列会话、`/new session` 新建、`/session <id>` 切换；`/init` 让 agent 通读工作区生成项目画像
- ☁️ **部署友好**：直接对接 LangGraph CLI / Platform

## 🚀 快速开始（60 秒上手）

### 1. 环境要求

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)（推荐）

### 2. 安装与配置

```bash
# 安装依赖
uv sync

# 生成环境变量模板并填写 API Key
cp .env.example .env
```

`.env` 必须包含 `app/config.py` 声明的全部键（键不可缺、值可为空）。**聊天模型必填**；嵌入模型当前未启用可留空；`TAVILY_API_KEY` 留空则联网检索不可用（对应 MCP server 自动跳过，不阻塞主流程）：

```bash
CHAT_MODEL_API_KEY=sk-...            # 聊天模型 Key（默认走 DeepSeek）
CHAT_MODEL_URL=https://api.deepseek.com
CHAT_MODEL_NAME=deepseek-v4-flash
EMBEDDING_MODEL_API_KEY=             # 词嵌入：当前未启用，留空即可
TAVILY_API_KEY=tvly-...              # 网页搜索 Key；留空则联网工具不可用
```

### 3. 启动并体验

```bash
.venv/Scripts/python.exe -m app.main     # 本机是 WSL + Windows venv，用仓库里的解释器
```

> ⚠️ **不要用 `uv run`**：WSL 里的 `uv` 是系统 uv，跑它会另建/重同步一个 Linux venv、破坏现有环境（见 CLAUDE.md「常用命令」）。

> 💡 工作区沙箱默认指向**启动目录**（运行 `python -m app.main` 所在的项目，agent 直接对它动手）；想操作别的项目就 `cd` 过去再启动。`langgraph dev` 与无参调用 `get_main_agent_graph()` 则默认 `<项目根>/tmp`（不指向仓库自身）。

输入任意问题即可开始对话。当模型请求调用 `write_file`、`run_command` 等敏感工具时，会看到类似这样的审批提示——终端命令会强制模型附带一句 `解释`（它在干什么、预期什么），与命令本身同屏展示，让你先看意图再核对命令：

```
+---------+
| Command |
+---------+
[run_command]   步骤 1/1
  解释: 删除临时演示目录 /tmp/demo
  参数:
    - command: rm -rf /tmp/demo
  调用ID: call_xxx
是否批准该操作？(y/n):
```

> 敏感工具按 `app/agent/tool.json` 逐个 `interrupt()` 挂起——一个回合里模型同时调用多个敏感工具会**连续多次**向你确认；读操作与计划编排自动放行、不打扰你。

#### 斜杠命令

以 `/` 开头的输入是命令，**不会当成提问发给模型**（未识别的 `/xxx` 也只给一行提示）：

| 命令 | 作用 |
| --- | --- |
| `/help` | 列出全部可用命令与说明 |
| `/init` | 让 agent 通读工作区，在工作区根**生成或更新** `AGENT.md`（项目画像）——写文件照常走审批，你先过目内容 |
| `/fast readme [语言]` | 快速生成一个开源项目的 `README.md`（按标识/简介/特性/快速开始/贡献/许可的通行结构写）；可跟语言（`zh` / `cn` / `de` / `jp` / `fr`…），不给则默认英语 |
| `/list session` | 列出本工作区的所有会话（短 id · 最后活动时间 · 消息条数 · 末条回复摘要） |
| `/new session` | 新建一个会话并切过去 |
| `/session <短 id>` | 切换到指定会话，接着它原来的消息栈继续聊 |

`/init` 属"提示词型"命令：它把一段预设提示词**当作你的输入**投给模型，由模型自己用现有只读工具读代码、再写画像——不加新工具、也不另起一条 run。

> **数据落在哪**：`resource/<工作区键>/`——每个会话一个 checkpoint 库（`sessions/<id>/agent.db`），长期记忆在 `memory/memory.md`（见[跨会话记忆](#跨会话记忆让模型记住你这个项目和你的偏好)）。换个工作区（`cd` 过去再启动）就是另一套会话与记忆。

### 最小可运行示例（代码方式）

```python
import asyncio
from langchain_core.messages import HumanMessage
from app.agent.graph import get_main_agent_graph

async def main():
    # 工作区是构造期参量：不传则默认 <项目根>/tmp（TUI 传的是启动目录）
    graph = await get_main_agent_graph()
    app = graph.compile()  # 传 checkpointer 才持久化会话；TUI/基座用的是
                           # AsyncSqliteSaver + resource/<ws>/sessions/<sid>/agent.db

    result = await app.ainvoke({
        "session_id": "demo",
        "messages": [HumanMessage(content="帮我创建一个 hello.txt")],
        "pending_tool_calls": [],
        "approved_tool_calls": [],
    })
    print(result["messages"][-1].content)

asyncio.run(main())
```

> 💡 遇到 `__interrupt__` 挂起时，先看 `payload["type"]` 再决定回什么（`resume` 的值随闸门种类变，见 `app/platform/turn.py::decision_to_resume`）：
> - `tool_approval`（工具审批）→ `Command(resume={"approved": True/False})`；
> - `ask_user`（agent 提问）→ `Command(resume={"option_index": 1, "supplement": "另外……"})`，
>   两段都可为 `None`（选项序号 0 起始）。
>
> 详见 [工具审批流程](#-进阶内容)。

## 🧩 进阶内容

### 架构：主程序是事件基座，图只是它的一个模块

`app/platform`（基座）管"一条 run 之外的事"：起/续/取消 run、把 `interrupt` 当事件端口、统一审批队列、按 (工作区, 会话) 装配 db 与图；`app/tui`（终端前端）只做两件事——**取输入、渲染事件**，它实现的正是基座要求的 `UI` 协议（`emit` 渲染 / `read_line` 取输入 / `decide` 就待审请求问人），**不碰调度**。将来的 web UI 就是同一协议的另一个实现，复用整个基座（详见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)）。

这带来一个可感知的行为差异：**审批会 park 图而不是占死进程**——主 agent 等你按 `y/n` 时，子 agent（worker）的审批照样能被服务。

### 一次请求怎么走完

```
START ─► llm_node ──(有 tool_calls)──► queue_node ─► review_node ──(逐条审批)──┬─► orchestrate_node ─┐
            ▲                                                                 └─► tool_node ────────┤
            └─────────────────────────────────────────────────────────────────────────────────────┘

  收尾：llm_node ──(无 tool_calls)──► 历史超预算 ? compact_node ─► END : END
```

| 节点 | 职责 |
| --- | --- |
| `llm_node` | 聊天模型判断本轮要不要调工具（可调 = 编排/笔记/派发/记忆工具 + 四个 MCP server 的全部工具）；拼系统消息时注入 行为契约 + 工作区上下文 + 当前计划 + 项目画像 + 长期记忆 |
| `queue_node` | 把模型输出的 `tool_calls` 移入待审队列，清空上一轮的放行队列与提问回答 |
| `review_node` | **人机闸门的落点**，按 `tool.json` 逐个判定：`need_review: true` 则 `interrupt()` 挂起等 `y/n`；`source: "ask"`（`ask_user`）则 `interrupt()` **提问**并把回答写进 state；通过的调用按 `source` 分流（state 工具 → orchestrate_node，其余 → tool_node）；**被拒**的调用回灌一条带 `[approval_denied]` 的回执（不让模型以为它执行了） |
| `orchestrate_node` | 执行 agent 侧 state 工具：计划编排（`create_plan` / `update_plan_step` / `clear_plan`）、`read_note`、`ask_user`（把闸门收来的两段回答渲染成回执）、`dispatch_subtasks`、`write_memory` / `read_memory`——只动会话内 state，不产生真实副作用 |
| `tool_node` | `ainvoke` 执行已批准的工具（MCP 工具是 async-only），结果作为 `ToolMessage` 回传 |
| `compact_node` | **轮末折叠**：历史超预算（默认 60000 字符）时，把更早轮次**已消费的完整工具块**折成一条摘要系统消息（联网正文归档进 `notes`，需要时用 `read_note` 取回）；只在收尾跑，不碰当前轮 |

几个关键点：

- **闸门用 interrupt 而非条件分支**：每个需审的 `tool_call` 各自 `interrupt()` 一次，所以一次请求可能挂起多次；基座（`app/platform`）把中断入统一 broker 并 park 住 run，前端（终端 TUI）只负责画面板、收人的决定（审批 `y/n`、提问选选项 + 补充），基座再以 `Command(resume=…)` 续跑——**阻塞图、不阻塞进程**，因此主 agent 等审批时 worker 的审批照样能被服务。**interrupt 必须待在"挂起之前没有副作用"的节点里**：resume 会把节点整体重跑，而外部副作用不会回滚（`review_node` 满足，`orchestrate_node` 不满足——所以 `ask_user` 的挂起在闸门处、执行在编排处）。
- **为什么这几类免审**：计划编排只动会话内、跨轮持久化的 `current_plan`；`read_note` 只读会话内的笔记；`write_memory` 写的是 agent 自己的私有记忆目录（`resource/` 下，不在你的仓库里，可随时改）；`dispatch_subtasks` 派出的 worker 只有 file_io 读 + **下放的终端** + 联网（后两者要审批时才来问你，**只读 git 子命令免审**）。真正有副作用的（写文件 / 删目录 / 跑命令 / 联网检索）一律要审批。
- **审批策略与工具归属集中在 `app/agent/tool.json`**（`need_review` + `source`）：

| 类别 | 工具 | 审批 |
| --- | --- | --- |
| 计划编排 | `create_plan` / `update_plan_step` / `clear_plan` | 免审（无真实副作用） |
| 会话笔记 | `read_note`（读被折叠归档的联网正文） | 免审 |
| 向用户提问 | `ask_user`（摆选项问用户、挂起等回答；回答 = 选中项 + 一句补充） | 免审（不产生副作用，也不替代审批） |
| 并发资料收集 | `dispatch_subtasks`（派发只读 worker 并行调研） | 免审（worker 自己发起的联网调用另需审批） |
| 长期记忆 | `write_memory` / `read_memory` | 免审（写在 resource 私有目录，可随时改） |
| 读 / 查询 | file_io：`read_file` / `list_dir` / `get_directory_tree` / `search_content` / `glob`；terminal 只读：`process_wait` / `process_read` / `process_list`；**git 只读子命令**（`git status` / `log` / `diff` / `show` / `branch` 列表 / `fetch`，经 `run_command`） | 免审 |
| 文件写 | `create_file` / `create_dir` / `write_file` / `edit_file` / `delete_file` / `delete_dir` / `copy_path` | 需审批 |
| git 写 | `git add` / `commit` / `switch` / `pull` / `push` / `clone` …（同样是 `run_command`，但不在免审白名单里） | 需审批 |
| 终端执行 | `run_command` / `start_process` / `process_kill` | 需审批 + 必填「解释」 |
| 联网检索 | `web_search` / `extract_urls` / `crawl_website` / `deep_research` | 需审批 |

### 跨会话记忆：让模型记住你这个项目和你的偏好

两套东西分工明确（设计见 [`docs/LONG_TERM_MEMORY.md`](docs/LONG_TERM_MEMORY.md)）：

| | 长期记忆 `memory.md` | 项目画像 `AGENT.md` |
| --- | --- | --- |
| 位置 | `resource/<工作区键>/memory/`（agent 私有，不进仓库） | **工作区根**（随仓库走，建议提交） |
| 内容 | 你的偏好与约束、敲定的决策、这个项目的既有约定 | 这个项目是什么、目录与技术栈、主流程、怎么构建与跑测试 |
| 谁写 | agent 自己（`write_memory` 免审；省略 key 追加、给 key 覆写） | 你用 `/init` 让 agent 通读工作区生成，或自己维护 |
| 何时被读到 | **每轮**注入系统提示 | **会话启动时**读入一次（改了它要重开会话才生效） |
| 给 worker 吗 | 不给（worker 是只读资料收集器，两样都不注入） | 不给 |

两边都**只放跨会话仍然成立的东西**：临时过程、随时能从工作区重取的内容不记——记忆里存的是"下次做这个项目时还成立"的偏好、决策与约定。

### 并发资料收集：一次派发多个只读 worker

需要"先并行查一堆互不相关的资料"时，模型可以调 `dispatch_subtasks`：每个独立问题派给一个**独立 worker 进程**（`mcp_service/sub_agent.py`，stdio spawn、干完即回收）并发执行，拿回各自的结论正文，由主 agent 汇总核对后再落地改动。

worker 的边界是硬的：工作区**只读**（文件检索）**＋ 下放的终端 ＋ 联网检索**，但**不改文件、推进计划或做决策**——它返回的结论只是素材。终端的下放靠 `tool.json` 的 `worker_allow` **逐条授权**（`run_command` 与四个进程管理工具），其中**只读 git 子命令免审**，写命令与联网会以"子任务审批"的形式出现在同一个审批面板里——worker 进程经 HTTP 把待审请求回传给主侧队列，**审核权始终在主侧**（面板上标 `worker:<id>` 并附上那条子任务原文，便于人判断来由）。

### 目录结构

```
theta/
├── app/                        # agent 本体 + 主程序壳（命名空间包）
│   ├── main.py                 # 程序入口（薄壳）：调 app.tui.run_tui()
│   ├── config.py               # 环境配置（pydantic-settings，读 .env）
│   ├── exception.py            # AgentError 体系（ConfigError / WorkspaceViolationError / InvalidArgumentError）
│   ├── resource.py             # 落盘路径单点：workspace_key / session_db_path / memory_root
│   ├── platform/               # 事件基座（"图外面那层"；不 print、不读 stdin）
│   │   ├── loop.py             # AgentPlatform：主事件循环（drain 待批 → 等 turn → 等输入）
│   │   ├── approvals.py        # 统一审批 broker：队列 + 薄 HTTP 收件箱 + 排空/回填
│   │   ├── turn.py             # 一次 run 的驱动原语：drive_turn（park/resume）+ _race
│   │   ├── runtime.py          # 按 (工作区, 会话) 装配 db / checkpointer / 图
│   │   ├── ui.py               # UI 协议（emit / read_line / decide）
│   │   └── commands/           # 控制面命令：/init /help /list session /new session /session
│   ├── tui/                    # 终端前端（只取输入 + 渲染事件，不碰调度）
│   │   ├── input.py            # stdin 单 reader 线程 + pump（一问一答）
│   │   ├── panels.py           # 审批面板 / 标题框 / 截断 / markdown 渲染（纯渲染）
│   │   ├── ui.py               # TerminalUI：实现 UI 协议
│   │   └── runner.py           # run_tui()：解析启动工作区 → 组 UI + 基座 → 跑
│   ├── agent/                  # 图本体
│   │   ├── graph.py            # 状态图构建与条件路由（langgraph.json 的注册入口在此文件）
│   │   ├── state.py            # AgentState：消息 / 审批队列 / current_plan / notes
│   │   ├── nodes.py            # LLM / Queue / Review / Orchestrate / Tool / Compact 节点
│   │   ├── prompt.py           # 系统提示词：行为契约 + 工作区上下文
│   │   ├── model.py            # 聊天模型初始化（OpenAI 兼容）
│   │   ├── mcp.py              # 以 stdio 子进程拉起四个 MCP server 并收集工具
│   │   ├── tools.py            # agent 侧工具：编排 / read_note / dispatch / 记忆读写 / 技能加载
│   │   ├── memory.py           # 长期记忆 memory.md 的读写与注入渲染
│   │   ├── skills.py           # 技能（skill）扫描 / 解析 / 注入渲染（只读单点）
│   │   ├── utils.py            # format_tool_result / coerce_tool_result（工具结果归一化）
│   │   ├── tool.json           # 审批策略集中登记（need_review / source / worker_allow）
│   │   └── command_policy.json # 终端命令的免审白名单（只读 git 子命令等）
│   └── schema/                 # 数据形状（agent / approval / ui / session 四个域）
├── mcp_service/                # MCP 服务器（各自独立子进程）
│   ├── file_io.py              # 文件操作 + WORKSPACE_PATH 沙箱（import 时校验该 env）
│   ├── terminal.py             # 终端进程管理器（任意命令 + 进程组托管 + sudo 硬拒绝）
│   ├── web_search.py           # Tavily 联网检索四件套 + 上游失败翻译
│   ├── sub_agent.py            # worker（子 agent）MCP server：run_subtask（spawn 即走，只读）
│   └── utils.py                # guard 异常收口装饰器（只 return 不 raise）
├── skills/                     # 技能库（可插拔的领域包；命名空间包）。一个技能一个目录：
│   └── github/                 #   SKILL.md（必备）+ 可选 server.py（有=能力型，无=知识型）
├── evaluation/                 # 模型行为评估框架（真实 LLM + 自动审批策略）
├── tests/                      # 机制层 pytest 单测（沙箱 / guard / 审批路由 / 计划 / 会话命令）
├── docs/                       # 设计文档（七篇 + 索引，见 docs/README.md）
├── resource/                   # 运行时数据（gitignore）：<ws_key>/{sessions/<sid>/agent.db, memory/}
├── langgraph.json              # LangGraph CLI/Platform 注册（my_agent → get_main_agent_graph_langgraph）
└── pyproject.toml              # 项目与依赖定义
```

### 运行测试

两条前提缺一不可：用 `python -m pytest`（裸 `pytest` 因包未安装会报 ModuleNotFoundError）；导出 `WORKSPACE_PATH` 指向一个已存在的目录（`mcp_service/file_io.py` 在 import 时即校验；测试夹具建在 pytest 的 tmp 下，该目录必须**包住**它，所以用系统的临时目录）。

```bash
# 本机是 WSL + Windows venv：用仓库里的解释器，且 WSL → Windows 传 env 必须靠 WSLENV
export WSLENV="WORKSPACE_PATH"
export WORKSPACE_PATH='C:\Users\<你>\AppData\Local\Temp'   # 见 python -c "import tempfile;print(tempfile.gettempdir())"

.venv/Scripts/python.exe -m pytest                                   # 全部测试
.venv/Scripts/python.exe -m pytest tests/test_file_io.py             # 单文件
.venv/Scripts/python.exe -m pytest tests/test_guard.py               # 异常/guard 层
.venv/Scripts/python.exe -m pytest tests/test_terminal.py            # 终端进程管理器
```

> 不设 `WSLENV` 变量根本进不去子进程（会看到 "WORKSPACE_PATH 未设置"），且值要写 **Windows 路径**（别用 `/tmp`，那会被当成 `E:\tmp`）。纯 Linux/macOS 下把解释器换成 `python`、`WORKSPACE_PATH` 指向该系统的临时目录并跳过 `WSLENV` 即可。
> Windows（PowerShell）：`$env:WORKSPACE_PATH="$env:TEMP"` 后再跑 `python -m pytest`。

> `app/config.py` 在 import 阶段就读 `.env`，跑测试/启动前 `.env` 必须存在（键全、值可空）。`tests/test_file_io_sandbox.py` 的符号链接逃逸用例在 Windows（无符号链接权限）会 skip。

### 模型行为评估（evaluation/）

`tests/` 守**机制**（沙箱 / guard / 审批路由等确定性单测）；`evaluation/` 守**模型行为**——用可编程审批策略（自动批准 / 自动拒绝 / 黑名单）替代 TUI 里的人工 `y/n`，跑真实 LLM 完成任务，再用确定性断言检查终态（文件 / 审批次数 / plan 状态），用于 prompt / 工具 docstring / tool.json 迭代时抓 LLM 涌现行为的静默回归。

```bash
python -m evaluation                          # 跑默认示例任务集（耗真实 token）
python -m evaluation --task write_and_verify  # 跑单个任务
```

### 本地部署 LangGraph

```bash
langgraph dev          # 启动本地开发服务器
langgraph build        # 构建可部署镜像
```

### 已知限制（上下文工程 / 消息压缩）

消息压缩与笔记区是**进行中的工程**，以下缺口当前已知、未闭环（`docs/CONTEXT_ENGINEERING.md` 是目标态设计，实现是它的简化收敛，改动时以代码现状为准）：

- **单轮不折（P1 滚动折叠未做）**：折叠只在"轮末且存在更早轮次"时触发；单个超长请求（一次任务里连续大量读取/输出）内部不会滚动折叠——本轮逼近或超过模型上下文时是**唯一的真实溢出风险**。
- **notes 只读不搜**：联网正文折叠归档进 `notes`，模型用 `read_note(note_id)` 按 ref 取回（免审）；尚无 `search_notes` / 按主题召回 / 懒分类，研究结论的 **promote 进 repo** 收尾也尚未在 prompt 引导。
- **旧问答与历史摘要逐轮留存**：折叠只删旧工具块；更早的 Human / 答复 / 历史摘要不合并，超长会话会线性累积（需很多轮才明显）。
- **预算未经实测标定**：默认 60000 字符、构造参数可调（`CompactNode(content_budget_chars=…)`），但未接 evaluation 做"达成率 / 轮数 / token"的量化校准。
- **老消息无结构化戳**：ToolMessage 的 `error_type` / `denied` 结构化戳是新路径，戳落地前的历史 checkpoint 消息靠解析 content 前缀回退判定。
- **read_note 的语义让步**：它需读 `AgentState.notes`，当前复用"编排（state 工具）"执行器（source=`notes`、免审）；若这类"读 AgentState 的 agent 工具"增多，将抽专门的节点/列表，不再堆进编排执行器。

#### 待办（上下文工程收口，按依赖序）

- [ ] **P1 · 单轮滚动折叠**：单次超长请求内部滚动折叠（`tool/orchestrate → llm` 回边预算触发；保留最新一批待模型反应的块）——闭环上面"单轮不折"缺口
- [ ] **notes 补全**：`search_notes` / 按主题召回 / 懒分类；prompt 引导 `read_note` 用法，与"研究结论 promote 进 repo"的收尾习惯
- [ ] **P2 · 量化标定**：evaluation 增加折叠前后"达成率 / 轮数 / token"断言，标定预算阈值；届时按需引入 token 精门（字符粗门保留）
- [ ] **历史摘要合并**：超长会话把更早轮的 Human / 答复 / 旧摘要再收敛，避免逐轮线性累积
- [ ] **read_note 收敛**：读 `AgentState` 的 agent 工具增多时，抽专门节点/列表、退出"编排"执行器

### 路线图

单 agent 闭环（执行 / 审批 / 计划）已通，以下为纵深方向（2026-09 规划）：

- [x] **多 Agent 编排（并行分发已落地）**：通过 MCP 服务拉起多个子 agent——`dispatch_subtasks` 一次派发多个**相互独立**的调研子任务，各自 spawn 独立 worker 进程并发执行；worker 只读工作区 + 可联网检索，敏感动作（联网四件套）经 **HTTP 回传进主侧统一审批闸门**；结论回主 agent 归并。**未做的是 DAG 拓扑编排与依赖调度**（下游消费上游产出 + 就绪调度）、以及子 agent 写权限下放。**设计草稿见 [`docs/MULTI_AGENT.md`](docs/MULTI_AGENT.md)**
- [x] **人机闸门 · agent 提问**：审批闸门泛化成"人机闸门"——`ask_user` 把选项摆给用户、挂起图等回答（回答 = 选中项 + 一句补充，两段都可只给一半）。与审批共用 `ReviewNode` 的 interrupt、统一 broker 与 `Command(resume)` 回程；worker 侧结构性不参与
- [ ] **反思节点（reflect-before-act）**：在 llm_node 拟调用 tool_calls 之后、进入人工审核之前，用**另一模型**审计一次拟执行操作（不同模型常有不同视角，容易揪出主模型盲点）；被否决则把**理由回灌主模型重想**。反思次数必须有**上限**——按实测，**单次反思收益已足够大，可先只做一次**，再做"多次 vs 收益"的评估对比
- [ ] **Web UI**：FastAPI（web 依赖组已预留）暴露 SSE + 审批端点，把人工审批与计划可视化搬到浏览器
- [ ] Web API 服务层（FastAPI）暴露 REST / SSE 接口
- ~~词嵌入模型接入与向量检索（chroma）~~ —— **搁置**：RAG/向量索引对可实时读取的工作区代码无作用面（索引无法像 git 一样随真值收敛、必然陈旧），仓库内检索走实时文件工具即可；仅在"工作区外 + 慢变只读 + pin 版本"的语料上才值得重新评估
- [ ] 更多 MCP 服务器接入
- [ ] 审批策略的运行时动态配置

## 🤝 贡献

欢迎提交 Issue 与 Pull Request！

1. Fork 并创建功能分支
2. 提交改动，确保测试全部通过（跑法见上文「运行测试」；**不要用 `uv run`**）
3. 发起 Pull Request，描述改动目的

详细流程见 `CONTRIBUTING.md`（待补充）。

## 📄 License

本项目目前**尚未指定 License**。如需开源发布，请先添加 `LICENSE` 文件并注明协议。

---

*如果你觉得 $\theta$ 对你有帮助，欢迎 ⭐ Star 支持！*
