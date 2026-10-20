<div align="center">

# Theta $\theta$

基于 **LangGraph** 的编码助手

**文件写入、删除和终端操作前，先由你审批。**

![Python](https://img.shields.io/badge/python-3.13+-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-1.x-brightgreen)

</div>

Theta 让大模型能够读取项目、调用工具并协助完成编码任务，同时在可能产生影响的操作执行前保留人工决策权。

## 核心能力

- **人工审批**：写入或删除文件、执行终端命令等操作会先展示给你；批准后才执行。只读文件操作可自动进行。
- **联网调研**：可搜索和抓取网页；联网工具执行前也会请求审批。
- **项目协作**：读取和检索工作区文件、制定任务计划、按需提问，并在需要时并发派发独立的只读调研任务。
- **会话与记忆**：会话可持久化并切换；项目画像和工作区记忆帮助后续任务沿用项目背景与约定。
- **可扩展工具**：通过 MCP 服务和按需加载的技能扩展能力，避免把所有领域工具和说明常驻在提示词中。
- **终端界面**：提供交互式 TUI；Agent 基座与界面分离，为后续其他交互界面留出扩展空间。

## 快速开始

### 环境要求

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- 一个兼容 OpenAI API 的聊天模型及其 API Key
- Tavily API Key（可选；不使用联网搜索时可留空）

### 安装与配置

```bash
uv sync
```

复制 `.env.example` 为 `.env`，填写聊天模型配置：

```dotenv
CHAT_MODEL_API_KEY=你的模型API_KEY
CHAT_MODEL_URL=https://api.deepseek.com
CHAT_MODEL_NAME=deepseek-v4-flash
TAVILY_API_KEY=你的Tavily_API_KEY
```

`TAVILY_API_KEY` 这一项需要保留在 `.env` 中，但值可留空；留空时联网搜索不可用。GitHub 技能凭证等可选配置见 `.env.example`。

### 启动

在希望 Theta 操作的项目目录中启动，当前目录即工作区。

Windows：

```powershell
.venv\Scripts\python.exe -m app.main
```

macOS / Linux：

```bash
.venv/bin/python -m app.main
```

启动后直接输入任务。需要写入、删除或执行命令时，检查审批面板中的操作和说明，再决定是否批准。

> **安全边界：** 文件工具限制在工作区内；终端命令不是操作系统级沙箱。审批能让你知情并决定是否执行，但获批命令仍可能影响工作区以外的资源。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `/help` | 查看可用命令 |
| `/init` | 分析工作区并生成或更新项目画像 `AGENT.md` |
| `/fast readme [语言]` | 生成 README；默认英语，可指定 `zh`、`de` 等语言 |
| `/list session` | 列出当前工作区的会话 |
| `/new session` | 创建并切换到新会话 |
| `/session <id>` | 切换到已有会话 |

## 架构概览

```text
TUI（输入与展示）
        │
        ▼
事件基座（会话、审批、运行生命周期）
        │
        ▼
LangGraph Agent（推理、审核、工具执行）
        │
        ├── MCP 服务：文件、终端、联网、worker 派发
        └── 按需加载的技能
```

LangGraph 图负责一轮 Agent 执行；外围基座负责会话管理和审批事件。需要人工决定时，图暂停等待，基座把请求交给界面，再恢复执行。详细设计、技术栈及实现约束见[技术路线文档](docs/TECHNICAL_ROADMAP.md)。

## 开发与文档

运行测试：

```bash
python -m pytest
```

运行 LangGraph 本地开发服务器：

```bash
langgraph dev
```

测试前需要准备 `.env` 和工作区环境变量；Windows/WSL 等环境的具体要求见[技术路线与实现说明](docs/TECHNICAL_ROADMAP.md#5-验证策略)。

更多项目设计：

- [架构原则](docs/ARCHITECTURE.md)
- [技术路线与当前实现](docs/TECHNICAL_ROADMAP.md)
- [上下文工程](docs/CONTEXT_ENGINEERING.md)
- [长期记忆](docs/LONG_TERM_MEMORY.md)
- [多 Agent](docs/MULTI_AGENT.md)
- [技能设计](docs/SKILL_DESIGN.md)
- [待办事项](docs/TODO.md)

## 贡献与许可

欢迎提交 Issue 和 Pull Request。项目目前尚未指定 License。
