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
python -m evaluation                          # 层 A 回放（默认，零成本）
python -m evaluation --task write_and_verify  # 只跑单个任务
python -m evaluation --live                   # 层 B 真跑（花 token）
python -m evaluation --record                 # 真跑并把模型输出录成 fixture（改动任务后重录）
python -m evaluation --keep-workspace         # 保留临时工作区，人工复查失败现场
```

任务的 prompt / setup 变了，旧 fixture 会被判定为陈旧并**跳过**（不计入通过率），跑 `--record` 重录即可。

**退出码**（层 A 零成本，所以它能当回归闸门直接进 CI）：`0` 通过 / `1` 不通过 / `2` 用法错误
（`--task` 名不存在）。「通过」= 检查全过 **且** 没有任务报错 **且** 至少有一个任务真跑了——
全部 SKIP 也算不通过：一行都没验证过报绿，等于把 fixture 陈旧藏起来。

### 本地部署 LangGraph

```bash
langgraph dev          # 启动本地开发服务器（非 Windows）
langgraph build        # 构建可部署镜像
```

Windows 下请使用 `.\scripts\langgraph-dev.cmd` 启动开发服务器。该脚本会启用 UTF-8、允许 LangGraph Studio 的本地网络预检，并使用 LangGraph dev 的 `--allow-blocking` 选项，避免 Windows 的 MCP 可执行文件探测被同步 I/O 检查拦截。

### 已知限制（上下文工程 / 消息压缩）

消息压缩与笔记区是**进行中的工程**，以下缺口当前已知、未闭环（`docs/CONTEXT_ENGINEERING.md` 是目标态设计，实现是它的简化收敛，改动时以代码现状为准）：

- [架构原则](docs/ARCHITECTURE.md)
- [技术路线与当前实现](docs/TECHNICAL_ROADMAP.md)
- [上下文工程](docs/CONTEXT_ENGINEERING.md)
- [长期记忆](docs/LONG_TERM_MEMORY.md)
- [多 Agent](docs/MULTI_AGENT.md)
- [技能设计](docs/SKILL_DESIGN.md)
- [待办事项](docs/TODO.md)

## 贡献与许可

欢迎提交 Issue 和 Pull Request。项目目前尚未指定 License。
