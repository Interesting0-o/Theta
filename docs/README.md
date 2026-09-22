# Theta $\theta$ 设计文档

本目录收录 $\theta$ 的设计文档。每份是**目标态设计 + 已落地标注**：文档与代码不一致时以代码为准，文档可作为演进方向（与根目录 `CLAUDE.md` 的约定一致）。

## 索引

| 文档 | 主题 | 一句话 |
| --- | --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 总设计 / 架构原则 | 主程序 = 事件基座，LangGraph 图只是基座上的执行单元；各分设计的上层 |
| [EXCEPTION_DESIGN.md](EXCEPTION_DESIGN.md) | 异常体系 | 异常的边界是语义过滤器——决定什么信息能进大模型的推理 |
| [CONTEXT_ENGINEERING.md](CONTEXT_ENGINEERING.md) | 上下文工程 | work_log（意图+事实双半行）、notes 笔记区（会话级 + promote 进 repo）、滚动块折叠 |
| [MULTI_AGENT.md](MULTI_AGENT.md) | 多 Agent 编排（草稿） | 拉起形态 / 审批回流 / 并行分发 vs DAG / 状态隔离 |
| [LONG_TERM_MEMORY.md](LONG_TERM_MEMORY.md) | 长期记忆 + resource 重构 | 跨会话记忆 = 按工作区落 md + SystemMessage 注入 + 记忆写读工具；resource 按 工作区/会话 分目录 |
| [SKILL_DESIGN.md](SKILL_DESIGN.md) | 技能（一期 §11 / 二期 §13 已落地，三期见 §13.10） | skill = 按需加载的领域包；差异化在"约束三分法"——软约束走 `SKILL.md` 正文（host 读取、注入系统提示）/ 硬闸门走 tool.json / 结构性拒绝写死在工具里。落地清单见 §11 |
| [TODO.md](TODO.md) | **待办 / 收口清单（未完成与进行中）** | 运行中 compact、reflect 节点、长期记忆分层化等条目的动机与现状 |
| [DONE.md](DONE.md) | **已完成条目归档** | `TODO.md` 里已勾掉的条目原文（动机 / 决策 / 落地记录 / 验证），只作追溯用 |

## 写作约定

- 文档语言用中文，与代码注释保持一致。
- 用 `[已落地]` / `[未做]` / 日期括注标注与代码现状的关系，避免读者把目标态当现状；**行为类文档若按日期追加记录（如 `MULTI_AGENT.md` §9/§10），要在顶部声明"读最新一条"**，否则后来者会把早期条目当现状。
- 根目录 `README.md`（首页）、`CLAUDE.md`（Claude Code 项目指令）固定留在根，不入本目录。
