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

## [ ] Reflect 节点：模型拟动手前，先让另一个（或同一个）模型审一遍

**动机**（2026-09-10 记）：README 路线图里的 "reflect-before-act"——在模型拟调用工具、**进入人工
审批之前**，用另一个模型审一次"这个动作对不对"，被否决就把理由回灌主模型重想。README 已定
"按实测**单次反思收益已足够，先只做一次**"。

**归位**：按 `docs/ARCHITECTURE.md` §4 的判据，这是**图内的一条边/一个节点**（一条 run 内部的
迭代），不是基座事件 → 落 `app/agent/nodes.py::ReflectNode`，**不进 `app/platform`**。

**要点（实现前须钉死）**：
- **插入点**：`llm_node →(有 tool_calls)→ reflect_node → queue_node`。放在 queue **之前**，被否决
  就直接回主模型重想，**不惊动审批面板与 broker**。
- **触发条件**：① 开关（`/reflect on|off` 之类）落 **state**（会话级运行态），别为此重编译图；
  ② 更值得的一条——**只对"需审批那一类调用"反思**（写/命令/联网），免审的读操作不值得花一次调用。
- **意见怎么回灌**：反思没有 tool_call 可兑现，**别塞成 ToolMessage**（读起来像"助手自己说的"）；
  建议走 state 的 `reflect_feedback` 字段，由 `LLMNode` 下一轮拼成 `# 审计意见（本轮）`——
  与 `current_plan` 的注入口径一致。
- **次数上限**：`reflect_count` 每轮重置、**最多 1 次**，否则"反思→重想→再反思"会打转。
- **模型选择**：用另一个模型（不同视角）还是复用 `CHAT_*`？选前者要加一组**可选**配置键，
  注意 `app/config.py`"键不可缺、值可空"的约定。
- **纪律**：反思是**机器审计**，**不能替代审批闸门**（否则等于用模型替人放行）——两条正交、可叠加。
- 相关背景：README 路线图"反思节点"条、`docs/ARCHITECTURE.md` §4（图内 vs 基座的判据）、
  `ReviewNode` 的 `log` 开关（节点逐条日志的既有做法）。

## [ ] agent 提问模式：需求不明确时，给用户选项等回答

**动机**（2026-09-10 记）：agent 调用编排工具，把"不明确之处的选项"返回给用户、等用户填写反馈。
本质是**把"审批闸门"泛化成"人机闸门"**：现在图↔人的往返只运一个 bool
（`Command(resume={"approved": …})`），这里要运"一个回答"（选项 / 自由文本）。

**两条路线（动手前须选）**：
- (a) **复用 interrupt**（倾向）：给 interrupt payload 加 `type`（`tool_approval` / `ask_user`），
  `resume` 按 type 分派——等于给闸门加第二种载荷，不新造通道。
- (b) 工具把选项写进对话、**不挂起图**：agent 先收尾一轮，用户下一条消息回答。实现最省，但
  "停下来等答案"的体验没了——两条路的交互差异要先确认。
- （不推荐）把 `ask_user` 当 `need_review: true` 的普通审批：resume 只装得下 bool，仍要扩协议，
  却把"问"与"批"混成一个语义。

**要点（实现前须钉死）**：
- **UI 协议要第一次实质扩展**：`UI.decide(value) -> bool` 装不下"选了第 2 项 / 自由文本"。这是
  "为第二个前端留的缝"的压力测试——**先定它该返回什么**（一个决定对象？），否则 web 前端接上
  时还要再改一次。终端侧顺带泛化刚做的 **EOF 处理**（EOF → 视为"未回答"）。
- **worker 不参与**：worker 是只读资料收集器、不该问用户 → 工具不登记进 `worker_tools` 白名单
  source 即结构性排除；worker 的 HTTP 回传**暂不认** `ask_user`。
- **提示词纪律**：模型给选项时必须写清"为什么问" + 每项差别与默认/推荐项，否则 5 个选项比
  1 个问题更难答。
- **无人回答怎么收场**：与审批保持一致（不设超时、人一直在等），还是给"未回答"一个收尾语义？须定。
- 相关背景：`docs/MULTI_AGENT.md` §6「统一审批视图」（marker/payload 语义要同步扩）、
  `app/platform/ui.py`（UI 协议）、`app/tui/panels.py`（选项渲染）、`app/platform/approvals.py`。

## [x] TUI markdown 渲染：模型答复不再满是 `**` 与 `###`

**动机**（2026-09-10 记）：`TerminalUI.emit` 现在直接 `print(event.text)`，模型的 markdown 原样
带出来，终端里很难读。

**落地**（2026-09-11）：`app/tui/panels.py::render_markdown`（rich 渲染，返回 bool = "渲染过了吗"）
+ `app/tui/ui.py::TerminalUI.emit` 的 `TurnFinished` 分支（`if not render_markdown(...): print(...)`
回退）。要点里"须定"的两条定成：

- **依赖策略 = 有就渲染、没就纯文本**（rich 属 pyproject 的 `ui` 可选组，实测常被 langchain /
  langsmith 传递装上，所以当前环境开箱即用）——与 TAVILY 留空则跳过联网同一取舍，不阻塞主流程；
- **只在 `TurnFinished` 渲染**，审批面板与 `Notice` 保持等宽纯文本（那里要精确、不能重排）。

渲染异常也只降级、不冒泡：展示层的问题不该吞掉一整轮答复。验证：`tests/test_main_tui.py`
（6 例，覆盖渲染 / rich 缺失 / 渲染抛错 / 空正文 / emit 两条分支）。

**要点（原设计记录）**：
- **只在 `app/tui`**（渲染属前端，分层干净）：`panels.py` 加渲染函数，`TerminalUI.emit` 对
  `TurnFinished` 走它；**审批面板保持等宽纯文本**（那里要精确、不能重排）。
- **依赖策略须定**：rich 进正式依赖，还是"**有就渲染、没就纯文本**"（后者更贴本项目"可选能力
  不阻塞主流程"的既有取舍——如 TAVILY 留空就跳过 web_search）；`pyproject.toml` 已有可选依赖组先例。
- 现在 turn 是**整段一次性返回**（无流式），所以是"一次渲染整段"；**若同时上流式输出**，rich 的
  `Live` + markdown 重排是另一个工程量——别混在一步里做。
- 可测性：用 `Console(file=StringIO())` 捕获输出断言；Windows 老终端（conhost）会降级，需实测。
- 相关背景：`app/tui/ui.py`（`emit`）、`app/tui/panels.py`、`docs/ARCHITECTURE.md` §3（前端职责）。

## [~] 技能（skill）：按需加载的"领域包"——**一期已落地，能力型待做**

> **设计已独立成文** → [[docs/SKILL_DESIGN]]。本条只留指针与当前状态，**别再往这里堆内容**。

一句话：把某个领域要用的**工具**和这个领域的**纪律**打成一包、按需加载。典型 = **GitHub**（推送 /
PR / issue / 搜索 / 不克隆就读远端代码，外加"先开分支""master 不能随便提"等约束）。差异化在
**约束三分法**——软约束走 `SKILL.md` 正文（host 读取、注入系统提示）、硬闸门走 `tool.json`、
结构性拒绝写死在工具里。

**当前状态（2026-09-12）**：**两型共用的那条通道已落地**——`app/agent/skills.py` +
`get_skill`/`drop_skill` + state 的 `loaded_skills` + `LLMNode` 注入（落地清单见
SKILL_DESIGN §11）。`skills/github/` 没有 `server.py`，故**当前自动是知识型**、可加载。
**未做**：能力型（`server.py` 生命周期、会话级注册表 + 动态 `bind_tools`、idle 回收、
env 声明与白名单转发）——那是 §3.3/§3.4 那套，也是最重的一块。

**动手做能力型前先读 SKILL_DESIGN.md §9「待定 / 待验」**——绑定时机 (b) 的注册表落地姿势、
env 展开白名单、多源目录的沙箱边界这几条没钉死之前，不要开工。

**与本文其它条目的关系**：SKILL_DESIGN §6 与上面的「反思节点」打通——skill fork 到隔离子 agent 执行，
本身即一次结构性反思，且 `mcp_service/sub_agent.py` 那套骨架已经在了；但它卡在
`docs/MULTI_AGENT.md` §6「子 agent 写权限下放」这个里程碑上。

## 相关但未立项（讨论过，待显式拍板再单列）

- 读策略太保守：`read_file` docstring 的"避免大文件整段塞满上下文"引导模型把 1280 行文件
  拆 ~200 行 × 多段读，徒增往返与重发成本；考虑改成"需要整份就整读、超大/只需探测才分段"
  + "相互独立的读取/检索尽量同回合并发"。
- 思考内容剥离：`LLMNode` 写回 history 前剥掉 AIMessage 里的 thinking/reasoning
  （content 内块与 additional_kwargs 都剥），保证思考永不回发、不累积成本。
- 长回合止损：run_tui 运行中响应 `q` 取消当前 turn；主 ainvoke 显式设 recursion_limit。
- 大体量广度探索的 dispatch 触发信号（docs/MULTI_AGENT §10"三模式如何被选中"）。
