# Theta $\theta$ 待办 / 收口清单

> 当前工作树仍在演进：文档与代码不一致时以代码为准（约定同 CLAUDE.md 与 docs/README.md）。
> 每条记录动机/现状，避免"当初为什么没做"再次翻车；勾掉前应能指到验证它的提交。

## [ ] 运行中 compact：工具循环中间的上下文收口

**问题**（2026-09-07 长回合实测）：主 agent 一轮里大量工具调用（如一次 jupyter 审计
~67 次、20 分钟）时，历史里的**读文件/检索结果作为 ToolMessage 每轮原样重发**，上下文越滚
越大、单轮越来越慢；`compact_node` 却只在**回合末**（`llm` 不再调工具 → `route_after_llm`
走 compact/END）才可能折叠，**工具循环中间从不触发** → 长跑无中间收口。

**目标态**：历史 content 超预算（复用 `needs_compact` / `CompactNode._plan` 口径）时，
在工具循环中途也折叠"更早轮次、已被消费的完整工具块"，只保留当前正在进行的轮次。

**要点（实现前须钉死）**：
- 只能折**最后一个 HumanMessage 之前的旧轮完整块**（含 AIMessage(tool_calls) + 兑现它的
  ToolMessages），**正在推进的当前工具链不能折**——判据 `CompactNode._plan` 已有；
- 折叠落点仍是 `SystemMessage(摘要, id=…)` 原位替换 + `RemoveMessage`，不尾追加；
- 触发点候选：queue/review 之前或每 N 轮检查一次；须避免与 interrupt 审批的 checkpoint
  交互互相干扰（折叠发生在哪一步、会不会打断挂起 run）；
- 折叠后的摘要系统消息会再注入每轮，注意别把"当前轮"误判成旧轮折掉。
- 相关背景：[[docs/CONTEXT_ENGINEERING]]（只流结论 / notes / 折叠目标态）、
  根目录 CLAUDE.md 上下文工程一节。

## 相关但未立项（讨论过，待显式拍板再单列）

- 读策略太保守：`read_file` docstring 的"避免大文件整段塞满上下文"引导模型把 1280 行文件
  拆 ~200 行 × 多段读，徒增往返与重发成本；考虑改成"需要整份就整读、超大/只需探测才分段"
  + "相互独立的读取/检索尽量同回合并发"。
- 思考内容剥离：`LLMNode` 写回 history 前剥掉 AIMessage 里的 thinking/reasoning
  （content 内块与 additional_kwargs 都剥），保证思考永不回发、不累积成本。
- 长回合止损：run_tui 运行中响应 `q` 取消当前 turn；主 ainvoke 显式设 recursion_limit。
- 大体量广度探索的 dispatch 触发信号（docs/MULTI_AGENT §10"三模式如何被选中"）。
