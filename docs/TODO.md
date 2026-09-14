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

## [ ] 工具面缺口：一次真实运行的实测（2026-09-13）

**样本**：一个会话、3 轮用户请求（克隆 Luna-Agent → uv 补环境 → 查/拉分支），共 **55 次**工具调用。

| 工具 | 次数 |
| --- | --- |
| `run_command` | **30（55%）** |
| `read_file` | 7 |
| `update_plan_step` | 6 |
| `list_dir` / `write_memory` | 各 2 |
| `get_directory_tree` / `create_plan` / `clear_plan` / `create_file` | 各 1 |
| `git_clone` / `git_branches` / `git_status` / `git_switch` | 各 1（**clone 与 switch 都失败过**） |
| `github_*`（整个技能） | **0** |

**三条结构性原因**（不是"模型不听话"）：

1. **第一步就脱轨**：本机 github 被 Watt Toolkit 劫持到 127.0.0.1，git 的 schannel 证书吊销检查
   失败 → `git_clone` 失败；绕过要 `-c http.sslVerify=false`，而**那个开关只能敲在 shell 里**（工具
   不收 git 配置）→ 此后所有 git 操作都走 shell（路径依赖）。
2. **工具参数面太窄**：`--depth` / `--unshallow` / `--track` / `-B` / `ls-remote` / `for-each-ref` /
   `reflog` / `diff --cached` / `config` —— 一个都不支持。该会话第三轮的 12 次 `run_command`
   **全部**是这类。
3. **github skill 结构性地插不上手**：它只有 7 个**只读平台**工具，没有克隆/分支/拉取——那些在核心
   `git_*` 里，而 `git_*` 又不支持上面那些参数。**模型没加载它是对的**（"克隆+补环境"本来就不是
   GitHub 平台操作）。

**收敛清单（按日志频次排序）**：

- `git_clone` 加 `depth`（浅克隆；日志里它直接用了 `--depth 1`）；
- `git_switch` 支持"从远端建跟踪分支"（`--track` / `-B` / `-u`；日志里试了三次才绕过去）；
- **远端分支清单**（`ls-remote` 语义）独立成只读工具、或给 `git_branches` 一个开关 —— 浅克隆下
  本地只有默认分支的跟踪引用，日志里连着两轮都在找它；
- `git_fetch` 支持 refspec / `--unshallow`。

**不要做**：给 `git_clone` 透传 `-c`——那等于给模型一个正式"关掉证书校验"的开关（MITM 面）。

**审批摩擦（同一份实测）**：全程 **33 次人工 y**，其中 **30 次是 shell 命令**。"一次路由调用了多个
工具"的实际开销是**人的**：图里 `review_node` 逐条 drain（`review_node → review_node`，排空才去
执行），**模型每轮只调一次**，所以 N 个 tool_call ≠ N 次模型调用，但 = N 次审批（需审的那些）+ N 条
ToolMessage 进 history（token，由 compact 折叠兜底）。实测里还有**模型自己的重复劳动**：一条复合
命令（`uv --version; python --version; git --version`）批准执行之后，它又把三条**分别重跑了一遍**
——4 次审批换一份信息。

## [ ] 工作区语义：克隆下来的项目落在工作区**子目录**里（2026-09-13 实测）

**现象**：用户新建一个空目录当工作区、让 agent 克隆 + 补环境，落点是 `工作区/Luna-Agent/`——于是
**工作区 ≠ 项目根**。后果全在"以工作区为界"的东西上：项目画像 `AGENT.md` 写在外层；长期记忆按外层
分（同一外层下克隆的第二个项目会**共享一份记忆**）；终端每条命令都得自己 `cd`（实测 30 次
`run_command` 里 **19 次带 `cd Luna-Agent && …`**，其中还有用 `&` 而非 `&&` 的——cmd 里 `&` 是
**无条件**分隔符，`cd` 失败也照跑，`checkout -B`/`reset` 这类写操作会落到错目录）。

**待拍板（两条，二选一或都要）**：

- **① 克隆落点**：`git_clone` 的 `dest` 改成**必填**（审批面板必须看得见落点——现在 `dest` 可空时
  人只看到 `url`），docstring 写清"**工作区是空的 → 传 `.`**（工作区即项目根：之后每条命令都不必
  `cd`，画像/记忆/沙箱也都在对的地方）；**工作区已有内容 → 给子目录名**"，回执再点明"这是不是工作区
  根"。`dest="."` 已验证可用（工作区非空时会被"目标非空"拒掉，回执教它改用子目录名）。
- **② 更根本**：空工作区**默认**落根（零操作，但面板看不到落点）；或加 `/workspace <path>` **切换
  工作区**（工作区是"项目容器"时用它；要动基座：关旧运行体 → 换 `ws_key` → 重建图与库），后者顺带
  解决"TUI 起在了错的目录"。

**副作用（记在案）**：工作区=项目根时，`/init` 写的 `AGENT.md` 会落进**克隆下来的那个仓库**，成为
它的未跟踪文件。这跟"在一个项目里起 agent"的常态一样（想不提交就 gitignore），只是值得知道。

## [x] 终端命令继承了 MCP 的 stdin（挂死；2026-09-13 修）

**现象**（真实会话实测）：`run_command("uv --version && git --version && python --version")` 永不返回
——`git --version` 挂满 `run_command` 的 300 秒超时；同一条链里的 `uv --version` 却 0.1 秒返回，
表象成了"只有 git 命令执行不了"。用户侧的感受是"**克隆下来项目、补环境老是补不全**"。

**根因**：`mcp_service/terminal.py::_build_spawn_kwargs` 只设了 stdout/stderr，**`stdin` 没设 → 继承**；
而 terminal server 自己的 stdin 是主程序连过来的 **JSON-RPC 管道**（没人写、也永不 EOF）。凡启动时
读一下 stdin 的程序就永远等下去。定位过程（都可复核）：`git --version < NUL` 秒过；同一解释器跑
`git --version` 单独正常；换绝对路径调同一个 `git.exe` 照样挂——**只差 stdin**。顺手排除了环境
（PATH/COMSPEC/SYSTEMROOT 都在，MCP SDK 会补默认环境）与 git 本身两个假设。
GitPython 起 git 时显式设了 `stdin=(istream or DEVNULL)`，所以**只有 terminal 这一处漏**。

**修法**：`stdin=subprocess.DEVNULL`；回归测试**直接断言 spawn 参数**（跑一条"读 stdin 的命令"
测不出来——pytest 进程自己的 stdin 通常已是 EOF）。**顺带挡住一种更坏的形态**：命令读到协议字节，
把 MCP 会话搞乱。

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

## [~] 技能（skill）：按需加载的"领域包"——**两期均已落地**（见 docs/SKILL_DESIGN.md）

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

**前置里程碑已落地（2026-09-12）**：SKILL_DESIGN **§12「MCP 运行体常驻化」**——MCP server 从
"每次调用重起的临时脚本"变成"会话期常驻的后台服务"（每 (工作区, 会话, server) 一个 owner task）。
顺带修好了 `mcp_service/terminal.py` 的跨调用进程管理（`start_process` 起的进程以前下次调用就认不到）。
承重约束（anyio 的 cancel scope 与 Task 绑定 × langgraph 每节点开新 Task）写在 §12.2，**改那套机制前必读**。

**二期已落地（只读子集，2026-09-12）**：SKILL_DESIGN **§13 能力型**——技能可以带 `server.py`，
`get_skill` 把它的工具拉进本会话、`drop_skill` 一并关掉（机制 = "技能就是按需加入的 server"，
复用 §12 的运行体）。首个能力型技能 `skills/github/` 落了 **7 个只读工具**（stdlib HTTP，不引 PyGithub）+ 一次**加载时凭证体检**（`skill.json` 的 `preflight`，替代了原 `github_auth_status` 工具）。

**三期已落地（2026-09-14，见 SKILL_DESIGN §13.10）**：`skills/github/` 从 7 个只读工具长到
**12 只读 + 3 写**（`github_pr_create` / `github_pr_review` / `github_issue_comment`，都要人批）。
越权动作在工具内**结构性拒绝**：`github_pr_review` 的 `event` 白名单不含 `APPROVE`；**"合并"连工具
都没有**（"合并权留给人"最彻底的落点是这个动作压根不存在，与"APPROVE 被拒"同构）。同批修掉了
`server.py` 的 8 处审计问题与 `skills.py` 两处真 bug（坏编码的 `SKILL.md` 会拖垮整次扫描；目录名是
Python 关键字时被误标 `[带工具]`）。

**未做**：技能运行体的空闲回收（已定不做，只靠 `drop_skill`）；`/skills` 控制面命令。口径见 §13.9。

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
