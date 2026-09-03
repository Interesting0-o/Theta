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

- 🛡️ **工具审批流**：写文件、删目录、跑命令前，TUI 会展示工具名和参数，等你按 `y/n` 确认
- 🔌 **MCP 工具即插即用**：文件操作与终端执行是独立的 MCP 服务器，新增工具不改核心代码
- 🌐 **联网搜索**：内置 Tavily 网页搜索，模型能查到最新信息
- 💾 **会话持久化**：SQLite 保存对话与审批状态，重启后接着聊
- 🖥️ **终端即用**：`python -m app.main` 一条命令进入交互式 TUI
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

`.env` 至少要填这几项：

```bash
CHAT_MODEL_API_KEY=sk-...            # 聊天模型 Key（默认走 DeepSeek）
CHAT_MODEL_URL=https://api.deepseek.com
CHAT_MODEL_NAME=deepseek-v4-flash
TAVILY_API_KEY=tvly-...              # 网页搜索 Key
```

### 3. 启动并体验

```bash
uv run python -m app.main
```

输入任意问题即可开始对话。当模型请求调用 `write_file`、`run_command` 等敏感工具时，会看到类似这样的审批提示：

```
+---------+
| Command |
+---------+
[run_command]   步骤 1/1
  参数:
    - command: rm -rf /tmp/demo
  调用ID: call_xxx
是否批准该操作？(y/n):
```

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
用户 ──► llm_node  ──(有 tool_calls)──► queue_node ──► review_node ──(批准)──► tool_node
           ▲                                                                       │
           └───────────────────────────────────────────────────────────────────────┘
                                                                 回到 llm_node
```

| 节点 | 职责 |
| --- | --- |
| `llm_node` | 大模型判断是否调用工具 |
| `queue_node` | 把模型输出的工具调用放入待审队列 |
| `review_node` | 按 `tool.json` 判定是否需审批；需要则 `interrupt()` 挂起等人工确认 |
| `tool_node` | 执行已批准的工具，结果回传给大模型 |

审批策略集中在 `app/agent/tool.json`，`need_review: false` 的读操作自动放行，敏感操作需人工确认：

| 工具 | 来源 | 审批 |
| --- | --- | --- |
| `web_search` | Tavily | 免审 |
| `read_file` / `list_dir` / `get_directory_tree` | mcp_service/file_io | 免审 |
| `create_file` / `create_dir` / `delete_file` / `delete_dir` / `copy_path` / `write_file` | mcp_service/file_io | 需审批 |
| `run_command` | mcp_service/terminal | 需审批 |

### 目录结构

```
CodingAgent/
├── app/
│   ├── main.py               # TUI 交互入口
│   ├── config.py             # 环境配置 (pydantic-settings)
│   ├── agent/
│   │   ├── graph.py          # 状态图构建与条件路由
│   │   ├── state.py          # AgentState 状态定义
│   │   ├── nodes.py          # LLM / Review / Tool / Queue 节点
│   │   ├── model.py          # 聊天模型初始化 (OpenAI 兼容)
│   │   ├── mcp.py            # MCP 服务器连接与工具加载
│   │   ├── tools.py          # 内置核心工具 (Tavily 搜索)
│   │   └── tool.json         # 工具审批策略配置
│   └── schema/               # ToolResult / ReviewRequest 数据模型
├── mcp_service/              # MCP 服务器（file_io / terminal / weather 示例）
├── tests/                    # pytest 单元测试
├── resource/                 # 运行时数据 (SQLite agent.db)
├── langgraph.json            # LangGraph CLI/Platform 配置
└── pyproject.toml            # 项目与依赖定义
```

### 运行测试

```bash
uv run pytest
```

### 本地部署 LangGraph

```bash
langgraph dev          # 启动本地开发服务器
langgraph build        # 构建可部署镜像
```

### 路线图

- [ ] Web API 服务层（FastAPI）暴露 REST / SSE 接口
- [ ] 词嵌入模型接入与向量检索（chroma）
- [ ] 更多 MCP 服务器接入
- [ ] 审批策略的运行时动态配置

## 🤝 贡献

欢迎提交 Issue 与 Pull Request！

1. Fork 并创建功能分支
2. 提交改动，确保 `uv run pytest` 全部通过
3. 发起 Pull Request，描述改动目的

详细流程见 `CONTRIBUTING.md`（待补充）。

## 📄 License

本项目目前**尚未指定 License**。如需开源发布，请先添加 `LICENSE` 文件并注明协议。

---

*如果你觉得 CodingAgent 对你有帮助，欢迎 ⭐ Star 支持！*
