<div align="center">

# CodingAgent

一个基于 **LangGraph** 的编码助手智能体（Coding Agent）

**写操作和命令执行永远需要你点头之后才动手。**

![Python](https://img.shields.io/badge/python-3.13+-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-1.x-brightgreen)
![uv](https://img.shields.io/badge/uv-依赖管理-orange)

</div>

---

## 📌 这个项目解决什么问题

大模型很擅长"想要"调用工具，但当你把**文件写入、目录删除、终端命令**这类能力交给它时，最怕的就是它自作主张。CodingAgent 在"模型想调工具"和"工具真正执行"之间插入了一道**人工审批闸门**：安全的读操作自动放行，有风险的操作必须先展示给你看，你确认后才执行。

简单说——**让你放心地把文件系统和终端交给你自己的 AI 助手。**

## ✨ 核心特性

- 🛡️ **知情审批闸门**：写文件、删目录、跑命令前，TUI 展示工具名、参数与模型的**意图解释**，等你按 `y/n`。终端命令强制模型附一句"这条命令要干什么"，命令 + 解释同屏展示，先看意图再核对命令
- 📋 **计划编排**：模型把任务拆成步骤（`create_plan`），边做边推进状态（`update_plan_step`），计划跨对话轮持久跟踪，不随对话丢失
- 🔌 **MCP 工具即插即用**：文件、终端、git、联网检索各自是**独立 MCP 服务器**，新增工具只改对应 server，核心 agent 代码不动
- 📁 **文件沙箱**：全部文件 / git 读写锁在 `WORKSPACE_PATH` 内——相对路径解析 + 符号链接消解后再校验，越界即拒绝
- 🔍 **检索基建**：`read_file` 行号分段读、`glob` 按路径定位、`search_content` 内容检索（大小写 / 正则可配），大仓库不整段读
- 🌐 **联网检索四件套**：`web_search` / `extract_urls` / `crawl_website` / `deep_research`（内置 Tavily）
- 🖥️ **终端进程管理器**：一次性 `run_command`（到点不杀、转入受管句柄）+ 常驻 `start_process` + `process_*` 生命周期管理
- 💾 **会话持久化**：SQLite（`SqliteSaver`）保存对话、审批队列与计划，重启后接着聊
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
uv run python -m app.main
```

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

### 最小可运行示例（代码方式）

```python
import asyncio
from langchain_core.messages import HumanMessage
from app.agent.graph import get_graph

async def main():
    graph = await get_graph()
    app = graph.compile()  # 传入 SqliteSaver 可持久化会话

    result = await app.ainvoke({
        "session_id": "demo",
        "messages": [HumanMessage(content="帮我创建一个 hello.txt")],
        "pending_tool_calls": [],
        "approved_tool_calls": [],
    })
    print(result["messages"][-1].content)

asyncio.run(main())
```

> 💡 遇到 `__interrupt__` 挂起时，用 `Command(resume={"approved": True/False})` 继续执行——详见 [工具审批流程](#-进阶内容)。

## 🧩 进阶内容

### 架构：一次请求怎么走完

```
START ─► llm_node ──(有 tool_calls)──► queue_node ─► review_node ──► tool_node ──► llm_node ...
              ▲  (无 tool_calls → END)                   │  ▲
              │                       (编排调用) ─► orchestrate_node ─┘
              └────────────────────────────────────────────────────────────────┘
```

| 节点 | 职责 |
| --- | --- |
| `llm_node` | 聊天模型判断本轮要不要调工具（可调 = 编排工具 + 四个 MCP server 的全部工具） |
| `queue_node` | 把模型输出的 `tool_calls` 移入待审队列，清空上一轮审批结果 |
| `review_node` | 按 `tool.json` 逐个判定：`need_review: true` 则 `interrupt()` 挂起等 `y/n`；通过的调用按 `source` 分流（编排类 → orchestrate_node，其余 → tool_node） |
| `orchestrate_node` | 执行编排工具（`create_plan` / `update_plan_step` / `clear_plan`）——只读写会话内跨轮持久化的 `current_plan`，不碰普通工具队列 |
| `tool_node` | `ainvoke` 执行已批准的工具（MCP 工具是 async-only），结果作为 `ToolMessage` 回传 |

几个关键点：

- **审批用 interrupt 而非条件分支**：每个需审的 `tool_call` 各自 `interrupt()` 一次，所以一次请求可能挂起多次；TUI 捕获 `__interrupt__` 后格式化成面板，收到 `y/n` 后用 `Command(resume={"approved": ...})` 恢复执行。
- **编排工具无真实副作用**：只更新会话内、跨轮持久化的 `current_plan`（create_plan 拆步 → 按步 update_plan_step 推进 → clear_plan），因此天然免审。
- **审批策略与工具归属集中在 `app/agent/tool.json`**（`need_review` + `source`）：

| 类别 | 工具 | 审批 |
| --- | --- | --- |
| 计划编排 | `create_plan` / `update_plan_step` / `clear_plan` | 免审（无真实副作用） |
| 读 / 查询 | file_io：`read_file` / `list_dir` / `get_directory_tree` / `search_content` / `glob`；git 只读：`list_repos` / `git_status` / `git_branches` / `git_diff` / `git_log` / `git_fetch`；terminal 只读：`process_wait` / `process_read` / `process_list` | 免审 |
| 文件写 | `create_file` / `create_dir` / `write_file` / `edit_file` / `delete_file` / `delete_dir` / `copy_path` | 需审批 |
| git 写 | `git_add` / `git_commit` / `git_switch` / `git_pull` | 需审批 |
| 终端执行 | `run_command` / `start_process` / `process_kill` | 需审批 + 必填「解释」 |
| 联网检索 | `web_search` / `extract_urls` / `crawl_website` / `deep_research` | 需审批 |

### 目录结构

```
CodingAgent/
├── app/                        # agent 本体（LangGraph 状态机；命名空间包）
│   ├── main.py                 # 交互式 TUI：捕获 interrupt → 审批面板 → Command(resume)
│   ├── config.py               # 环境配置（pydantic-settings，读 .env）
│   ├── exception.py            # AgentError 异常体系（Config / WorkspaceViolation / InvalidArgument）
│   ├── agent/
│   │   ├── graph.py            # 状态图构建与条件路由（langgraph.json 注册为 my_agent）
│   │   ├── state.py            # AgentState：消息 / 审批队列 / current_plan
│   │   ├── nodes.py            # LLM / Queue / Review / Orchestrate / Tool 节点
│   │   ├── prompt.py           # 系统提示词：行为契约 + 工作区上下文
│   │   ├── model.py            # 聊天模型初始化（OpenAI 兼容）
│   │   ├── mcp.py              # 以 stdio 子进程拉起四个 MCP server 并收集工具
│   │   ├── tools.py            # 编排工具 create_plan / update_plan_step / clear_plan
│   │   └── tool.json           # 审批策略集中登记（need_review / source）
│   └── schema/agent_schema.py  # ToolResult / PlanStep 数据模型
├── mcp_service/                # MCP 服务器（各自独立子进程，import 时校验 WORKSPACE_PATH）
│   ├── file_io.py              # 文件读写 + WORKSPACE_PATH 沙箱（相对路径解析 / 符号链接消解）
│   ├── terminal.py             # 终端进程管理器（任意命令 + 进程组托管 + sudo 硬拒绝）
│   ├── git.py                  # git 仓库操作（向下查找仓库；读免审、写需审）
│   ├── web_search.py           # Tavily 联网检索四件套 + 上游失败翻译
│   └── utils.py                # guard 异常收口装饰器（只 return 不 raise）
├── evaluation/                 # 模型行为评估框架（真实 LLM + 自动审批策略）
├── tests/                      # 机制层 pytest 单测（沙箱 / guard / 审批路由 / 计划）
├── docs/                       # 设计文档（索引见 docs/README.md）
│   ├── EXCEPTION_DESIGN.md     # 异常体系设计
│   └── CONTEXT_ENGINEERING.md  # 上下文工程（消息留存 / 折叠）
├── resource/                   # 运行时数据（SQLite agent.db，gitignore）
├── langgraph.json              # LangGraph CLI/Platform 注册（my_agent → app/agent/graph.py）
└── pyproject.toml              # 项目与依赖定义
```

### 运行测试

两条前提缺一不可：用 `python -m pytest`（裸 `pytest` 因包未安装会报 ModuleNotFoundError）；导出 `WORKSPACE_PATH` 指向一个已存在目录（`mcp_service/file_io.py` 在 import 时即校验；测试夹具建在 pytest 的 tmp 下，用 `/tmp` 即可）。

```bash
WORKSPACE_PATH=/tmp uv run python -m pytest                       # 全部测试
WORKSPACE_PATH=/tmp uv run python -m pytest tests/test_file_io.py # 单文件
WORKSPACE_PATH=/tmp uv run python -m pytest tests/test_guard.py   # 异常/guard 层
WORKSPACE_PATH=/tmp uv run python -m pytest tests/test_terminal.py  # 终端进程管理器
```

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

### 路线图

单 agent 闭环（执行 / 审批 / 计划）已通，以下为纵深方向（2026-09 规划）：

- [ ] **多 Agent 编排**：通过 MCP 服务拉起多个子 agent。**子 agent 的敏感操作仍统一走人工审批闸门**；编排模式按**子任务耦合度**选择——相互独立（如"逐季分析 2024–2026 财报"）→ 各干各的并行分发；依赖清晰（如"做一个含前后端 + 数据库的购物网站"）→ **DAG 拓扑**编排（下游消费上游产出，把 create_plan 的线性步骤推广为带依赖的图 + 就绪调度）。待定：子 agent 审批回流机制、DAG 调度器、结果归并
- [ ] **反思节点（reflect-before-act）**：在 llm_node 拟调用 tool_calls 之后、进入人工审核之前，用**另一模型**审计一次拟执行操作（不同模型常有不同视角，容易揪出主模型盲点）；被否决则把**理由回灌主模型重想**。反思次数必须有**上限**——按实测，**单次反思收益已足够大，可先只做一次**，再做"多次 vs 收益"的评估对比
- [ ] **Web UI**：FastAPI（web 依赖组已预留）暴露 SSE + 审批端点，把人工审批与计划可视化搬到浏览器
- [ ] Web API 服务层（FastAPI）暴露 REST / SSE 接口
- ~~词嵌入模型接入与向量检索（chroma）~~ —— **搁置**：RAG/向量索引对可实时读取的工作区代码无作用面（索引无法像 git 一样随真值收敛、必然陈旧），仓库内检索走实时文件工具即可；仅在"工作区外 + 慢变只读 + pin 版本"的语料上才值得重新评估
- [ ] 更多 MCP 服务器接入
- [ ] 审批策略的运行时动态配置

## 🤝 贡献

欢迎提交 Issue 与 Pull Request！

1. Fork 并创建功能分支
2. 提交改动，确保 `WORKSPACE_PATH=/tmp uv run python -m pytest` 全部通过
3. 发起 Pull Request，描述改动目的

详细流程见 `CONTRIBUTING.md`（待补充）。

## 📄 License

本项目目前**尚未指定 License**。如需开源发布，请先添加 `LICENSE` 文件并注明协议。

---

*如果你觉得 CodingAgent 对你有帮助，欢迎 ⭐ Star 支持！*
