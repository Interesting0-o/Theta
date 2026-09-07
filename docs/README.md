# CodingAgent 设计文档

本目录收录 CodingAgent 的设计文档。每份是**目标态设计 + 已落地标注**：文档与代码不一致时以代码为准，文档可作为演进方向（与根目录 `CLAUDE.md` 的约定一致）。

## 索引

| 文档 | 主题 | 一句话 |
| --- | --- | --- |
| [EXCEPTION_DESIGN.md](EXCEPTION_DESIGN.md) | 异常体系 | 异常的边界是语义过滤器——决定什么信息能进大模型的推理 |
| [CONTEXT_ENGINEERING.md](CONTEXT_ENGINEERING.md) | 上下文工程 | work_log（意图+事实双半行）、notes 笔记区（会话级 + promote 进 repo）、滚动块折叠 |
| [MULTI_AGENT.md](MULTI_AGENT.md) | 多 Agent 编排（草稿） | 拉起形态 / 审批回流 / 并行分发 vs DAG / 状态隔离 |
| [TODO.md](TODO.md) | 待办 / 收口清单 | 运行中 compact（工具循环中间上下文收口）等未立项项的动机与现状 |

## 写作约定

- 文档语言用中文，与代码注释保持一致。
- 用 `[已落地]` / `[前置]` 标注与代码现状的关系，避免读者把目标态当现状。
- 根目录 `README.md`（首页）、`CLAUDE.md`（Claude Code 项目指令）固定留在根，不入本目录。
