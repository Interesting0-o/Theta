# `nodes.py` 重构方案（2026-11-04）

> 状态：**方案稿；Stage 0 / 1 / 2 已落地（2026-11-04）**。Stage 3（统一 FoldGroup /
> 按组摘要）与 Stage 4（可选 `nodes/` 包）未做。
> 结论一句话：**先抽“上下文压缩引擎”，暂不把 `nodes.py` 机械拆成 `nodes/` 包。**

---

## 1. 背景

`app/agent/nodes.py` 目前同时承载两类东西：

1. **图节点适配器**：`LLMNode`、`ToolNode`、`ReviewNode`、`OrchestrateNode`、`QueueNode`，负责一拍内“读 state → 做这一拍 → 写回 state”；
2. **上下文压缩引擎**：`CompactNode`、`CompressNode`、工具块解析、批级分组、状态判定、归档策略、摘要渲染。

这两类代码的变化原因不同：

- 图节点跟着**运行协议**变：审批、工具执行、state 切片、动态工具集；
- 压缩引擎跟着**上下文生命周期**变：块解析、语义分组、批级指针、notes、缓存经济。

现在两类代码挤在同一模块里，导致压缩算法每次演进都要穿过一整个执行链路。

---

## 2. 为什么重构

本方案**不以**以下两点作为理由：

- **不拿文件行数说事**：行数只说明现象，不说明职责边界是否错了；
- **不拿现有导入路径说事**：导入路径是历史事实，不是架构判据。

真正的理由如下。

### 2.1 两套块解析已经漂移

轮末 `CompactNode._plan()` 自己扫一遍工具块；轮内 `_iter_tool_blocks()` 又扫一遍。

两套解析在语义上已经不一致：

- 轮末要求“失败 / 被拒整块保留”；
- 轮内允许同块成功兄弟被 mask，失败正文保留；
- 轮末用 `tool.json source` 推导编排工具；
- 轮内另有一份硬编码保护表；
- `CompressNode` 还反向复用 `CompactNode._tm_status` 与 `_read_range`。

这不是两个节点“恰好相似”，而是**同一套协议解析被拆成两份实现**。

### 2.2 策略散落，已经开始互相打架

当前至少有四处策略：

| 策略 | 出处 |
| --- | --- |
| 轮末不折的编排工具 | `tool.json source` 派生 |
| 轮内状态回执工具 | `_STATE_RECEIPT_TOOLS` 硬编码 |
| 轮内保护工具 | `_NO_MASK_TOOLS` 硬编码 |
| notes 归档工具 | `ARCHIVE_TOOLS` 硬编码 |

结果是同一工具在轮末与轮内的处置不一致。`read_note / read_event_log` 就是典型例子：轮末被 source 保护，轮内却可能被批级 mask。

### 2.3 目标态已经要求统一 planner

`docs/CONTEXT_ENGINEERING.md` 的目标态明确写了：

- 抽共享 `_iter_blocks`；
- `CompactNode` 与 `CompressNode` 统一到 `CycleBatchPlanner`；
- 轮末按组压缩；
- Phase 2 做整块墓碑 / args 清理。

这不是本文新造的方向，而是项目已有演进目标。当前模块形态已经跟不上这个目标。

### 2.4 图节点与压缩引擎是两种东西

项目自己的架构判据已经写明：

> 节点做“这一拍做什么”；被它调用的模块做“机制怎么实现”。

压缩引擎是纯确定性机制，不依赖审批、不依赖模型、不依赖动态工具集。它适合独立成模块，而不是继续塞在节点文件里。

### 2.5 算法缺陷需要一个可测试的落点

当前压缩算法存在明确缺口：

- 批级 mask 触发高水位后，没有真正压到低水位就停；
- 语义切点可能突破 `max_batch_chars`；
- 增量 read 去重会留下陈旧指针；
- 旧消息失败前缀回退不全；
- plan 分组按“调用意图”重建，失败 plan 也可能改变边界；
- 轮末摘要没有容量上界。

这些问题的修复需要一组纯函数与回归测试，而不是继续在 `nodes.py` 里叠条件分支。

---

## 3. 不做什么

本方案**明确不**包含以下内容：

1. **不先拆 `nodes/` 包**；
2. **不把 `LLMNode / ToolNode / ReviewNode / OrchestrateNode / QueueNode` 机械拆成一类一个文件**；
3. **不在本次重构中改变审批 / 提问 / 工具执行行为**；
4. **不把轮末压刚完成周期混进结构迁移**；
5. **不把低频 checkpoint / LLM digest 一并做掉**；
6. **不新建泛化的 `utils.py / contracts.py / format.py` 工具袋**。

理由见 §6 与 §8。

---

## 4. 目标结构

推荐先落一个独立的压缩包，`nodes.py` 继续保留图节点：

```text
app/agent/
  nodes.py                  # 仍保留五个执行节点 + Compact/Compress 适配器
  compression/
    __init__.py             # 只导出公共 API，不做隐式全局
    models.py               # ToolBlock / FoldGroup / FoldPlan / FoldDecision
    parser.py               # 协议块解析、状态判定、边界识别
    policy.py               # 编排 / 保护 / 归档 / 状态回执策略
    planner.py              # 分组与批规划，硬边界 → step → 语义 → 预算
    compact.py              # 轮末引擎，产出 FoldPlan
    compress.py             # 轮内引擎，产出消息更新
```

### 4.1 模块职责

| 模块 | 职责 | 不做什么 |
| --- | --- | --- |
| `models.py` | 定义结构原子与折叠计划 | 不做解析，不做策略 |
| `parser.py` | 只负责把消息流还原成 `ToolBlock` | 不决定压不压 |
| `policy.py` | 决定工具 / 消息的处置类别 | 不做分组，不渲染摘要 |
| `planner.py` | 把 block 切成 `FoldGroup` / batch | 不直接改 state |
| `compact.py` | 轮末生成 FoldPlan | 不碰 LangGraph state 合并 |
| `compress.py` | 轮内生成 content 更新 | 不删消息，不改 tool_call 配对 |

### 4.2 依赖方向

必须保持单向：

```text
nodes.py
  → compression
    → models / parser / policy / planner
```

禁止：

```text
compression → nodes.py
compression → app.agent.tools
compression → app.platform.mcp
```

如果 compression 需要 `ORCHESTRATE_SOURCES` 或 ToolMessage 戳，先抽窄协议模块，不允许反向 import `nodes.py`。

---

## 5. 核心抽象

### 5.1 `ToolBlock`：协议原子

只负责保证 API 合法性：

```text
AIMessage(tool_calls)
  + 与之配对的 ToolMessage
```

它回答的问题是：

- 这个块完整吗？
- 有没有稳定 id？
- 是否已被消费？
- 哪些回执属于哪个调用？

它**不**回答：

- 这个块该不该压；
- 失败调用是否连带成功兄弟；
- 摘要写在哪里。

### 5.2 `FoldGroup`：压缩分组

由连续工具块组成，是轮末真正折叠的单位。

边界优先级：

1. 不跨 HumanMessage / 最终答复 AIMessage；
2. 不跨不完整或未消费工具块；
3. `create_plan` 开新 epoch；
4. `update_plan_step(..., in_progress)` 开新 step；
5. 无 step 时按连续工具块自然成组；
6. 失败、被拒、ask_user、编排回执作为保护点；
7. 预算只做兜底，不再主导边界。

目标输出是**每组一条摘要**，而不是一次运行一条全局扁平清单。

### 5.3 `FoldPlan`：显式计划

不再返回 `removed / lines / archived / replaced` 四元组，而是返回结构化计划：

```text
FoldPlan
  groups
  removed_messages
  replaced_messages
  archived_notes
  summaries
```

这样轮末节点只做一件事：把 `FoldPlan` 翻译成 LangGraph state 更新。

### 5.4 `FoldDecision`：逐调用处置

目标是把“整块全折 / 全留”改成逐调用决策：

| 决策 | 含义 |
| --- | --- |
| `KEEP` | 保留原文 |
| `MASK` | 压成指针 / 短戳 |
| `ARCHIVE` | 正文进 notes，摘要留 ref |
| `DROP` | 直接删除，不归档 |

这允许同一块里：

- 失败调用保留；
- 成功可重取调用折叠；
- 联网正文归档；
- 编排回执保留。

---

## 6. 为什么不先拆 `nodes/` 包

五个执行节点的耦合不在文件层面，而在运行时协议层面。

| 节点 | 拆包难点 |
| --- | --- |
| `LLMNode` | 请求装配、RUNTIME_STATE、动态 bind、图片副本、写回消息属于同一拍 |
| `ReviewNode + QueueNode` | 共享 pending / approved / ask 状态，图拓扑上必须保持两个节点 |
| `ToolNode` | 与 Review / LLM 共享同一个 `SessionToolset` 实例 |
| `OrchestrateNode` | state 切片协议必须跟执行器走，拆散会把协议变成无主约定 |

此外，拆 `nodes/` 包还有几个具体风险：

1. **monkeypatch 静默失效**：测试里有 5 处直接 patch 模块全局，包级 re-export 不会传导到子模块；
2. **import 时机改变**：`settings = get_settings()` 目前是刻意在 import 期触发；
3. **循环依赖风险**：子模块回读包门面很容易形成环；
4. **运行协议耦合不会消失**：拆文件不减少 state / 队列 / interrupt 的真实耦合；
5. **没有第二个真实消费者**：目前主图是唯一生产调用方，包边界可能超前。

所以，`nodes/` 包不是当前第一刀。

---

## 7. 方案对比

| 方案 | 收益 | 弊端 | 判断 |
| --- | --- | --- | --- |
| 保持单体 `nodes.py` | 改动最小；现有行为最容易审查 | 两套解析继续漂移；策略继续散落；压缩演进空间受限 | 不推荐作为长期形态 |
| 只抽 compression 引擎 | 职责清晰；可独立测试；符合已有架构判据；迁移风险可控 | 需要处理测试接缝与协议归属 | **推荐** |
| 直接拆 `nodes/` 包 + compression 包 | 模块最分散；导航性最好 | 行为回归难定位；monkeypatch / import / logger 都要重审；可能过度设计 | 暂缓 |
| 只修算法，不拆模块 | 短期最快 | 修复继续挤在混杂模块里；后续仍要拆 | 可作为过渡，不是终点 |

---

## 8. 收益

### 8.1 结构收益

- 压缩引擎与图节点解耦；
- `ToolBlock` 只有一个真相；
- `FoldGroup` 成为显式的压缩单位；
- `FoldPlan` 取代多返回值；
- 策略集中到 `policy.py`，不再四处硬编码。

### 8.2 算法收益

- 轮末可按组压缩，而不是一锅扁平摘要；
- 单块内可逐调用处置，失败调用不再拖住成功兄弟；
- 摘要可以按 step / 语义组组织；
- `CycleBatchPlanner` 有了明确落点。

### 8.3 测试收益

- 压缩引擎可以脱离 LLM / 审批 / settings 测试；
- 可以直接测纯函数与 FoldPlan；
- 回归用例可以按“协议、分组、策略、摘要”分层；
- worker 与主图后续可复用同一解析器。

### 8.4 演进收益

- Phase 2 的整块墓碑 / args 清理有稳定接口；
- 低频 checkpoint / digest 可以独立迭代；
- prefix cache 评估可以集中在引擎层做。

---

## 9. 弊端与风险

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 拆包不等于修算法 | 只搬家不会让压缩变好 | 先补测试，再修算法，最后搬家 |
| 测试接缝会变 | 私有方法、模块全局、logger 名、`tool.json` 定位都要跟着动 | 迁移前先加 characterization 测试 |
| 循环依赖 | compression 反向 import `nodes.py` 会成环 | 依赖方向写死，CI 或 review 检查 |
| import 时机改变 | `settings` 目前在 import 期触发 | 先保持现状，不做懒加载 |
| monkeypatch 失效 | 包级 re-export 不传导子模块全局 | 测试 patch 具体定义模块 |
| 行为回归 | 按组折叠会改变消息形态 | 结构迁移与行为变更分提交 |
| prefix cache 变差 | 多个摘要插入点可能造成多个缓存失效边界 | 用 `llm_usage` 实测前后对比 |
| checkpoint 复杂化 | mask 后正文进最新 checkpoint，原文未必可恢复 | 先补 SQLite 重启回归 |
| 过度抽象 | 拆太细会变成新的工具袋 | 只抽有真实共享面的结构，不建通用 utils |

---

## 10. 实施阶段

### Stage 0：补 characterization 测试

先不改结构，也不改算法。用例落点：`tests/test_compression_characterization.py`
（2026-11-04 已落地，9 例全绿）。

分两类，别把目标态混进 characterization：

**A. 现状快照（Stage 1 修算法时故意翻转对应断言）**

1. 批级 mask **不检查低水位**：超过高水位后把所有旧块压完，不会压到低水位就停；
2. 语义切点可把首个超预算块带进批，**突破 `max_batch_chars`**；
3. 增量 read 去重指针指向的最新读取**可能随后被 mask**，指针链失效；
4. 混合块处置不一致：轮内只压成功兄弟；轮末任一失败/被拒则整块保留。

**B. 不变量（重构前后都不许变）**

5. steering 提前收口时，未消费的最新块不压；
6. 轮末失败/被拒正文原样保留（整块不折）；
7. SQLite 重启后不重复摘要 / notes。

> 2026-11-04：A 类 1–3 已随 Stage 1 翻转为目标行为（用例名不再带 `current`）；
> A 类 4 的两条不一致是**有意的**（轮内可部分压、轮末整块保守），保持现状。

### Stage 1：修算法，不搬家（2026-11-04 落地 5/6）

先在现有模块里修：

- [x] 低水位真正控制批级 mask：逐批压，回到 `low_watermark_chars` 即停；
- [x] 语义切点不越界：切点只落在预算内，`max_batch_chars` 成为硬上界
  （单个块本身超预算时只能独立成批）；
- [x] read 指针可更新：去重目标受 mask 保护，已压缩消息不再被选为指针目标；
- [x] 状态判定统一：复核确认两节点均已走 `_tm_status`，本次无新增分叉；
- [x] plan 分组只认成功调用：失败/被拒/未兑现的 plan 调用不改变 epoch/step；
- [ ] 摘要有容量上界（未做：截断会丢 notes ref，容量与截断策略待定）。

这一步不改模块结构，只改行为。回归网：`tests/test_compression_characterization.py`
（10 例）+ 原有压缩测试 67 例；全量 **705 passed / 7 skipped**。

### Stage 2：抽 compression 引擎（2026-11-04 已落地）

- [x] 建 `app/agent/compression/`：
  `models.py` / `parser.py` / `policy.py` / `planner.py` / `compact.py` / `compress.py`；
- [x] `nodes.py` 只保留 `CompactNode / CompressNode` 两个 state 适配器
  （行数 2310 → 1251）；
- [x] `ORCHESTRATE_SOURCES` 下移到 `app/agent/gates.py`；compression 不反向
  import `nodes.py` / `app.agent.tools` / `app.platform.mcp`；
- [x] `FoldPlan` 替代轮末多返回值；`CompactNode` 只做 `SystemMessage` /
  `RemoveMessage` / `notes` 翻译；
- [x] 测试改为 import 具体子模块（`compression.parser.tm_status` /
  `compression.policy.orchestrate_tools`），不再依赖节点类上的静态方法；
- [x] 全量 **705 passed / 7 skipped**；新包不 import 图/审批模块，压缩测试可直驱引擎
  （注：`import app.agent.compression` 仍会先执行 `app/agent/__init__.py` 的 re-export，
  所以测试进程仍需 `.env`——这是父包现状，不在本阶段范围内）；
- [ ] prefix cache 前后对比：本次以搬家为主（Stage 1 的预算/指针修复已轻微改变消息形态），
  尚无真实会话基线，留待 P2 评估接线。

### Stage 3：统一 FoldGroup / FoldPlan（未做）

Stage 2 已完成其中两项：共享 `ToolBlock` 解析（`parser.py`）与统一策略表（`policy.py`）。
剩下：

- `FoldPlan` 增加 `groups` / 按组摘要（当前只有 `removed` / `summary_lines` /
  `archived_notes` / `replaced`）；
- 允许块内逐调用处置（`FoldDecision`：KEEP / MASK / ARCHIVE / DROP），把轮末的
  “整块保留/整块折叠”细化到调用级。

### Stage 4：可选的 `nodes/` 包

只有满足以下条件才做：

- compression API 已稳定；
- 出现第二个真实消费者；
- 测试已经 patch 具体子模块；
- 有明确的行为回归网。

---

## 11. 验收标准

结构迁移完成的验收：

1. 全量测试通过；
2. 无 `compression → nodes` 反向 import；
3. monkeypatch 全部落到具体子模块；
4. `CompactNode / CompressNode` 只做 state 适配；
5. `FoldPlan` 替代多返回值；
6. 真图跑通审批 / 提问 / 工具执行 / worker；
7. SQLite 重启后不重复折叠；
8. prefix cache 指标不劣于现状；
9. 文档与代码一致。

算法重构完成的验收：

1. 低水位停止可测；
2. 批大小硬上界可测；
3. 混合块逐调用处置可测；
4. 失败 / 被拒正文不丢；
5. `read_note / read_event_log` 策略一致；
6. 摘要容量有界；
7. 轮末按组摘要稳定。

---

## 12. 结论

**重构有必要，但第一刀不是拆 `nodes/` 包，而是抽 compression 引擎。**

- 先补测试；
- 先修算法；
- 再抽结构；
- 最后做按组轮末压缩；
- `nodes/` 包暂缓。

这样才能保证每一步都可回滚、可定位、可验证。
