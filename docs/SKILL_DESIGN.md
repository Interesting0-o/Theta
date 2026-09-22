# Theta $\theta$ 技能（Skill）设计（草稿）

> ⚠️ **2026-09-15 作废通告**：本文多处（尤以 §8.3 与 §13 的工具清单）以 `mcp_service/git.py` 的
> `git_push` / `git_clone` / `git_*` 为参照来论证"核心能力 vs 技能"的边界。**那批工具已整体删除**
> （git 一律走 `run_command`，只读子命令免审见 `app/agent/command_policy.json`）。
> **下述记录保留原样**——它们是当时的决策依据，不是当前事实；读到时以代码为准。判据本身
> （"动的是本地仓库还是远端平台"）仍然成立。

> 状态标注：`[已落地]` = 已实现（可指到代码/测试）；`[未做]` = 目标态（设计方向，尚未实现）。
> **一期已落地（2026-09-12）**：`get_skill` / `drop_skill` 通道（两型共用那半）——见 §11「一期落地」。
> **前置里程碑已落地（2026-09-12）**：§12 = 「MCP 运行体常驻化」（**通用基建，非 skill 专属**）——含它的承重约束（anyio × langgraph 的 Task 绑定），改那套机制前必读。
> **二期已落地（只读子集，2026-09-12）**：§13 = 能力型——技能可以带 `server.py`，工具随加载出现、随卸载消失。
> **未做**：技能运行体的空闲回收、`/skills` 命令、§6 的 fork 执行、§10 的命令收敛。
> 文中标 `[已落地]` 的另一些是**既有基建**（当时为"将来复用"而标），不是 skill 能力本身。
> 相关：[[docs/ARCHITECTURE]] §4（"图内一条边" vs "基座上一类事件"的判据）、[[docs/MULTI_AGENT]] §6（统一审批视图 / 子 agent 写权限下放）、[[docs/EXCEPTION_DESIGN]]（`guard` 与错误语义）、[[docs/CONTEXT_ENGINEERING]]（notes 的"轻注入"同构）、[[docs/LONG_TERM_MEMORY]]（工作区内容 → 注入的既有通道）。

---

## 0. 一句话

**Skill = 按需加载的"领域包"**：把某个领域要用的**工具**和这个领域的**纪律**打成一包，平时既不占上下文、也不起进程，需要时才整个拉进来。

**本设计的差异化只有一处**：**约束三分法**——软约束走 `SKILL.md` 正文（host 读取、加载期注入系统提示）、硬闸门走审批声明（**2026-09-19 修订：技能自带工具的 `need_review` 声明在技能目录自己的 `skill.json`，见 §13.11；`tool.json` 只管内置 MCP 工具与编排工具**）、结构性拒绝**写死在工具里**。这一条写不清楚，skill 就塌缩成"`opencode-lazy-loader` 的重新实现"（§7）。

---

## 1. 要解决什么

现在的扩展面**全是能力**——MCP server 加工具、编排工具改 state。教模型"**怎么做某类事**"的地方只有两处：

| 现有通道 | 局限 |
| --- | --- |
| `SYSTEM_PROMPT`（`app/agent/prompt.py`） | 每轮**全量注入**；再往里塞领域纪律就挤爆上下文，且与无关任务一起付费 |
| `PROMPT_COMMANDS` 载荷（`app/platform/commands/prompts.py`） | 只能**用户手动敲**；模型无法自主触发；正文没法按需取 |

缺的是中间一层：给模型一份**目录**（只有名字 + 一句话），它判断相关时再取回正文或拉起能力照做。即"渐进披露"的领域包。

**典型例子 = GitHub**：推送 / 开 PR / 审 PR（**合并权留给人**）/ 不克隆就读远端代码 / 看 issue 与包 / 搜项目，外加一串约束（自主提交、先开分支、master 不能随便提）。这些能力**当前都没有**——`mcp_service/git.py` 只管**本地** git（status/diff/log/commit…，推送由 `git_push` 补上、仍是核心服务，见 §8.3）。

---

## 2. 两种载荷（别混为一谈）

### 2.1 严格子集：两类只差一个 `server.py`（2026-09-11 定）

|  | `SKILL.md` | `server.py` | `get_skill` 做什么 |
| --- | --- | --- | --- |
| **知识型** | 有 | **无** | 读正文 → 注入 |
| **能力型** | 有 | **有** | 读正文 → 注入 **+ 起 server + 注册工具** |

- **`SKILL.md` 对两型都是必备**——它是"这个领域是什么 / 该守什么"的**唯一来源**。
- **`server.py` 是唯一的 server 侧产物**；有它就是能力型。
- 能力型 = 知识型 **+ 一个进程**。这不是类比，是字面上的子集——所以**发现 / 扫描 / 注入的代码只有一套**，能力型多的那一步是"起进程 + 注册工具"。

**能力型优先做**：只有它解决"能力当前根本没有"的问题（§5）；知识型的机制随后再补。

### 2.2 宿主 / 服务端的分工（**不变量**）

**`SKILL.md` 永远由 host 侧读（主进程的工具实现），server 从不读它。**

- **`get_skill` 永远是 host 侧动作**：读 `SKILL.md` 正文 →（能力型）再拉起 `server.py`。正文只进 state（**不回给模型**，见 §3.5 的铁律）。
- 这是**不变量**，不是当前实现的偶然。它换来两件事：
  1. `skills/` 成为**可插拔、不侵入主业务逻辑的插件包**——新增一个技能 = 加一个目录，主流程一行不改；
  2. 知识型（连进程都没有）才能和能力型走**同一条**加载 / 注入通道（§3.5）。
- **推论**：`server.py` 只需管好自己的工具（读 GitHub、开 PR…），**不必知道 `SKILL.md` 存在**，也不负责往外吐约束文本。

两者不冲突、按轴分工：**领域能力**（GitHub）→ 能力型；**项目本地约定** → 知识型。

> **落地时两者共用一套目录布局**（`skills/<name>/`，差别只在有没有 `server.py`）——见 §8.1。

---

## 3. 能力型的形状

### 3.1 一个技能目录同时给两样东西（**仅能力型**）

`server.py`（工具：推送 / PR / issue / 搜索…）+ `SKILL.md`（约束正文）。两样**同目录、同加载时机**，但**由 host 侧分别读取**（§2.2）——server 不读 `SKILL.md`，也不负责提供约束文本。

跟着能力一起到；不用时既不进上下文、也不起进程。

> **知识型只给一样**（正文，没有工具）。别把本节读成对两型都成立——本节在"能力型的形状"标题下。

### 3.2 `get_skill(name)` 是**两型统一**的入口，且是编排工具

- **统一入口**：知识型**也**走 `get_skill`——否则它就没有"加载事件"（§3.5）。区别只在**能力型多一步**：起 `server.py` + 注册工具；**知识型只读正文，连进程都不起**。
- `ReviewNode.ORCHESTRATE_SOURCES` 加一项 `skill`，走 `OrchestrateNode`——归位同 `dispatch_subtasks`（都是"拉起子资源"的编排动作）。
- **免审**：它自己不动手（动手的是它带来的工具，那些照常撞闸门）；知识型更是只读工作区文件。**两型都免审。**
- **技能目录**（name + 什么时候用）放 `get_skill` 的 docstring——**构图期扫一次烤进去**（与 `load_mcp_tool` 同一节奏）。工具 schema 反正每轮都在，**这一段零新增注入机制**。技能**安装**是低频事件，所以"扫一次、重开会话生效"够用——`AGENT.md` 的实例内 memo 同理（不是硬约束，(b) 既然能动态改绑，重建 docstring 也做得到；只是不值得）。
  > ⚠️ 但**正文注入是另一回事**：它要落 state、动 `LLMNode`（§3.5）。别把"目录零注入机制"误读成"整条链路零机制"。
  > ⚠️ 也**别把目录和正文的开销混成一个数**——两者都每轮付费，但增长律与满后策略完全不同（§3.6）。

### 3.3 不常驻 = 懒起 + 连接池 + 空闲回收

**不是"懒起后常留"**：首次用到才起，起后按 `(会话, skill, server)` 进池复用（别每次调用重 spawn + 重认证）；**空闲 N 分钟自动断开**——否则 skill 一多，进程数只增不减。`opencode-lazy-loader` 就是这个做法（池化 + 5 分钟 idle），比"常留"可运维得多。

两个坑：

- **idle 计时不能把"park 等审批"算进去。** 一次 `github_pr_merge` 挂起等人按 y/n，人可能十分钟后才回；若按 wall-clock 计 idle，server 会在 park 中途被拆掉。计时以"最近一次工具调用**完成**"为基准，挂起期间冻结。
- **拆 server 必须同时注销它的工具**（否则模型会去调已经不存在的工具）——这条与 §3.4 的 (b) 注册表强耦合。

> **本节的两个坑已落地 → §12**（"懒起 + idle 回收"改成了**常驻到宿主关闭**，理由见 §12.3 决定 4；另有一处实测结论：改造前项目里**根本没有池**，连核心四件套都是每次调用重起 server 子进程，已造成 `terminal` 的跨调用缺陷）。本节留作**为什么需要常驻**的论证，机制细节不复述。

### 3.4 ⚠️ 鸡生蛋：工具什么时候 bind 到模型上 → **已定：走 (b) 真·动态绑定**（2026-09-11）

现状是**构图期**一次 `load_mcp_tool` → `bind_tools(all_tools)`、`ToolNode` 持固定 `tools_map`——工具集**构图期定死**。而 skill 的工具在 `get_skill` 之前不存在。两条路：

| | 做法 | 结论 |
| --- | --- | --- |
| (a) schema 常驻、进程懒起 | 工具 schema 一直在，子进程 / 认证到第一次真调用才发生 | **否决** |
| **(b) 真·动态绑定** | 工具随 `get_skill` **出现**、随卸载**消失** | **取此** |

**(a) 被否决的两条理由：**

1. **"动态注入"本身就是这个设计要的能力。** 工具**按需出现**是能力型 skill 的卖点（§2/§3.1）；(a) 把它换成"永远可见"，等于开局就放弃了这一条。
2. **只有 (b) 能让 §3.3 的 idle 拆除语义自洽。** 拆 server 必须同时注销它的工具——在 (b) 下这是自然的（工具本来就在注册表里，删一条即可）；在 (a) 下只能靠"下次调用自动重启"兜底：工具永不失效，但要吞一次冷启动延迟，且**模型看不到"这个技能已经睡了"**。(a) 不是更省，是换了个更难解释的模型。

**设计要点：**

- **注册表 = 会话级对象**，不是散在各节点上的属性。`LLMNode`（bind）、`ToolNode`（`tools_map` 查表）、`ReviewNode`（tool.json 查表路由）**三者共持同一份**——这正是 (b) 的主要复杂度所在。
- **不必每轮 bind**：注册表带一个 `version` 计数，`LLMNode` **只在版本变化时**才重新 `bind_tools`。加载 / 卸载才动版本，平时零开销。
- **卸载有两条触发路径，走同一个卸载函数**：① **模型显式关**（`drop_skill`，主动、便宜）；② **idle 超时**（§3.3，被动、保证进程不堆积）。**两条都要**——只给模型权力，它可能忘记关；只靠超时，则白占一段时间的上下文与进程。
- **`tool.json` 仍是静态的**（skill 的工具预先登记，§8.3）——所以 `ReviewNode` 的查表不受动态绑定影响，只是"注册表里有这个工具吗"要问注册表。
- **这是本项目第一个"运行中改变工具集"的机制**，值得单独立项、单独测。

> **已落地（2026-09-12）→ §13.2/§13.4/§13.6**。三处与本节原稿不同：① "注册表 = 会话级对象"简化成
> **技能 = 按需加入的 server**（运行时状态按 (工作区, 会话) 落在 `app/platform/mcp.py`，与 §12 同源，
> 不另造对象）；② 卸载**只剩一条路径** `drop_skill`（不做 idle 回收，与 §12 决定 4 一致）；
> ③ `version` 计数与"只在版本变化时 bind"照原样落地（`LLMNode` 持未绑定 model + 按版本缓存 binding）。

### 3.5 注入机制：正文怎么进系统提示（2026-09-11 定）

`get_skill` 返回的是工具结果，**改不了 system prompt**。所以注入只能绕 state：

> ⚠️ **铁律：正文只有一个家。**
> `get_skill` 的 ToolResult **只回执、不回正文**（如"已加载 github 技能，正文已进系统提示"）。
>
> **为什么这条是铁的**：工具结果是会进 **message history** 的，此后每轮随历史重发。若正文当作
> ToolResult 返回、同时又注入 system prompt，就是**两份拷贝**——而淘汰只能抹掉注入那份，history
> 里那份仍在（直到 compact 把它折掉）。**淘汰就成了谎**：模型手里始终有正文，预算回收的是个影子。

1. **`get_skill` 是两型的统一入口**（§3.2）——知识型同样走它，否则知识型根本没有"加载事件"，正文也就永远进不来。
2. **正文的落点是 state → 系统提示**：`get_skill` 往 state 写新字段（如 `loaded_skills`），`LLMNode` 下一轮读它、拼进 system prompt——**与 `current_plan` 的注入口径一致**（[[docs/TODO]]「反思节点」那条已立此先例：反馈走 state 字段、由 `LLMNode` 拼成系统消息）。动 `app/agent/state.py` + `app/agent/nodes.py::LLMNode`，**不是**在 `get_skill` 里改提示词。
   - **没有时间差**：图恒为 `tool_node → llm_node`，所以"加载"的下一次模型调用**就带着正文**——回执写"正文已进系统提示"是**真的**，不会让模型以为加载失败。
   - **归位代价**：`OrchestrateNode` 目前把 `current_plan` **硬编码**成唯一的 state 切片（`if "current_plan" in result: …`），要支持 `loaded_skills` 的增删得**把它泛化成通用的 state 切片合并**。
3. **留多久**：**会话内常留，直到卸载**——① 显式 `drop_skill`；②（能力型）idle 超时（§3.3）。**不建议"只注入 N 轮"**：软约束"只生效 N 轮"在语义上讲不通（纪律要么在、要么不在），且要为此另造一个计时器。常驻与 `AGENT.md` / 长期记忆同一口径（都是"本会话已知的背景"）。
   > ⚠️ **常驻的代价是每轮都在付费**（系统提示每轮全量重发）——所以注入**要有预算**，口径同 `MEMORY_INJECT_CAP` / `AGENT_MD_INJECT_CAP`。
   >
   > **但"满了淘汰谁"不能自动决定**（2026-09-11 定）：**预算满时 `get_skill` 直接拒绝**，并返回**当前已加载的技能清单**，要求模型先 `drop_skill` 一个。**不静默淘汰任何一个。**
   >
   > **淘汰必须让模型看得见**：注入块的抬头**列出当前已加载的技能**——这是模型决定卸哪个的依据，也让"某技能已不在列表里"成为卸载信号。否则 history 里那句"已加载 github 技能"的回执还在，模型会以为纪律仍在生效——**又是一种"影子"**。
   >
   > 这也正是**知识型需要的下界**：它**没有 idle 卸载的理由**（无进程可省），若不设预算，加载过的知识型技能会永久占着每一轮的 token。
   >
   > **淘汰要成立，前提就是上面那条铁律**——正文只有一个家，抹掉就是真抹掉。

   **为什么不做自动淘汰："最久未用"在知识型上没有定义。** LRU 需要一个**"使用事件"**当信号，而**两类不对称**：

   | | use 信号 | 自动淘汰 |
   | --- | --- | --- |
   | **能力型** | **有**：最近一次该技能工具的调用 | **不需要**——idle 超时（§3.3）已覆盖，且信号干净（没调用就拆） |
   | **知识型** | **无**：正文被动注入，模型每轮都"看到"，没有可观测的"用" | **无法定义** |

   给知识型硬凑一个策略都是坏结果：**FIFO**（最久已加载先出）虽然良定义，但"最早加载"往往恰是**当前正在依赖**的那个——你就是在做这个领域时加载它的，等于专挑要用的扔。所以**把淘汰权显式交给模型**：只有它知道自己用没用完。代价是多一个 `drop_skill` 工具、模型可能卡在"预算满又不肯卸"——但那时给的是**响亮且可行动**的反馈（拒绝 + 给清单 + 要求先卸一个），不是静默猜错。
4. **一个已知的漏（诚实记下）**：淘汰管得住"注入那份"，**管不住模型自己 `read_file` 去捞一份**（技能目录就在工作区 / 项目根里，file_io 读得到）。那会绕过整套机制、把正文塞进 history。但那是**模型主动花的成本**，且属明知故犯——不为此加"禁止读 skills 目录"这类硬拦（新增特例规则的复杂度不抵收益）。记录在案。

### 3.6 两个预算，口径不同（**别混用一个数**）（2026-09-11 记）

目录与正文**都每轮付费**，但**增长律和"满了之后能做什么"完全不同**：

| 预算 | 内容 | 随什么增长 | 满了怎么办 |
| --- | --- | --- | --- |
| **目录** | 所有**已安装**技能的 `name` + `description` | **技能生态规模**（装了多少） | **不能淘汰**——淘汰 = 不可发现 = 技能等于不存在。**只能截断**（有技能变不可达，是坏结果） |
| **正文** | 所有**已加载**技能的 `SKILL.md` | **使用**（加载了多少） | **由模型显式卸载**（`drop_skill`）；预算满时**拒绝新加载**并要它先卸一个（§3.5） |

- **目录是硬上限**：它决定"这个会话最多能装多少技能"。因为不可淘汰，溢出时**只能截断**，而截断意味着某些技能**装了却不可发现**——最坏的失败形态是**静默失效**。所以：① cap 设得够宽；② 溢出时必须**告警**（日志 + 让用户看得见），**绝不静默丢**。
- **正文是软上限**：显式卸载即回收，模型需要时重新 `get_skill` 拿回（§3.5）。**不做自动淘汰**——"最久未用"只在能力型有定义（能力型另有 idle 兜底，所以也不缺自动路径）。
- **口径不同 → 两个常量**：`SKILL_CATALOG_CAP`（**以条数为主**，每条就一行）与 `SKILL_BODY_BUDGET`（**以字符为主**，口径同 `MEMORY_INJECT_CAP` / `AGENT_MD_INJECT_CAP`）。一个是"装了多少"，一个是"用了多少"，**别用同一个数**。
- 目录在**构图期**定死（§3.2）→ 没有运行时淘汰问题；正文在**运行时**增删（§3.5）。

---

## 4. 约束分三类（本文核心；**下表是能力型语境**）

> **知识型没有工具**——既无硬闸门、也无结构性拒绝，**只剩软约束一种**。下表别读成"所有 skill 都有三类"。

| 种类 | 例子 | 落点 | 强度 |
| --- | --- | --- | --- |
| **方法 / 行动指导** | "推送前先开分支""PR 描述写清动机与验证" | **`SKILL.md` 正文**（host 读取，加载期经 state 注入系统提示，§3.5） | 软：模型读了照做，可被忽略 |
| **审批闸门** | "合并 PR 必须人批""推送要人点头" | **技能自带工具：`skill.json` `tools` 段声明 `need_review: true` → interrupt；内置工具：`tool.json`** | 硬：结构性挂起 |
| **结构性拒绝** | "master / protected 分支不许直接提交" | **工具内部直接拒绝**（`InvalidArgumentError`） | 最硬：压根不问人 |

- 第二、三类**绝不能只写进 `SKILL.md` 正文**——那等于把闸门降级成提示词纪律，与本项目"写操作要点头是**结构性前提**、不是提示词纪律"的立身之本相悖（同 [[docs/TODO]]「验证门」那条的结论）。
- 第三类有**现成先例可抄**：`mcp_service/terminal.py::_deny_sudo` `[已落地]` 在 spawn 前直接 raise——"不做提权"是结构性的，不是问人。`github_push` 判定目标是 protected branch 就该照此直接拒。
- **所以：skill 的硬约束 = 它的 `skill.json` 声明行（§13.11）+ 工具内的拒绝逻辑；`SKILL.md` 正文只装软的那半。**
- **skill 不得自己造审批**：skill 只是"把工具与指导一起送到"，**不得**声明"用了本 skill 就免审"——决定权仍在审批声明与闸门（同 [[docs/TODO]]「反思节点」那条纪律）。**自报政策的信任前提是技能源 first-party（同仓库）**：若将来开放外部技能源，外部技能的政策声明 host 不认、一律按 `need_review: true` 兜底。

---

## 5. 备选方案与否决理由（为什么不走"文档 + 编排工具 + 脚本"）

**一条编排工具只能返回文本、或改 state——它给不了模型新的可执行能力。** 所以"skill 只给文档/指导"的版本有个硬伤：GitHub 这类能力当前根本没有（§1），只有"间接实现"——让模型自己拼 shell 去调 `gh` / `git push`。这条路三点不可接受：

1. **环境是即兴的。** 脚本跑在哪、依赖装没装、凭据从哪来，全靠模型现场判断（`pip install`？建 venv？token 塞进 URL？）。结果不可预计、不可复现，每次还是新花样。**MCP server 把这三样在*编写期*就钉死**——进程怎么起、env 注入哪些键、cwd 在哪（`app/platform/mcp.py::_build_servers` `[已落地]` 干的就是这个），不留给模型即兴。
   > 这也正面回答"有脚本的 skill 怎么让 agent 执行"：**脚本不该由模型去跑，该被封进 server**。
2. **审批失去意义。** `run_command` 把整个 shell 字符串当成一个审批单元——面板上是长长一条命令，人批的是"这条命令"，不是"把 PR #123 合进 main"。**知情审批要求审批对象是语义化的动作**：`github_pr_merge(pr=123)` 一眼可审，`bash -c "…"` 不可审计。
3. **错误语义丢失。** shell 的失败是 stderr 文本，要模型自己读懂；MCP 工具走 `guard` `[已落地]` → 结构化 `ToolResult(error_type)`——正是 [[docs/EXCEPTION_DESIGN]] 的立论。

**一条中间路线的诚实评估**：也可以做个 `run_skill_script(name)` 工具，跑一个**已审阅、env 已知**的脚本——它确实解掉了"环境即兴"。但那要声明 env、要管进程、要定错误语义，**基本是在手搓一个 MCP server**；而 MCP 已经是本项目现成的能力封装单位（launcher / env / `guard` / `tool.json` / ToolNode / adapters 全套现成），重造没道理。

### 5.1 唯一没被 MCP 解决的一半：依赖仍是共享的

现在所有 server 都用 `sys.executable`（**同一个 venv**）。所以 env 变量、cwd、`PYTHONPATH` 这些 MCP 确实钉死了，但 **Python 三方依赖仍然共享**：skill 要用 `PyGithub`，要么进项目 `pyproject.toml`、要么另做"独立 venv / `uvx` 启动"的机制。

**这是真问题**（它决定 skill 能不能自带依赖）。**已决（2026-09-12）：不做隔离**——共用主 venv，skill 的新库进主 `pyproject.toml`（见 §9）。这也说明"环境"担忧有一半掉在 server 边界**外面**。

---

## 6. 执行：fork 到隔离子 agent（顺带是天然的 reflect 载体）

Claude Code 比"知识型"再往前一步：**skill 可 fork 到一个隔离子 agent**，给独立 token 预算与系统提示。本项目**已有这套基建的骨架** `[已落地]`：`mcp_service/sub_agent.py` + `get_sub_agent_graph` + `_spawn_subagent_worker`——独立上下文、独立 system prompt（`WORKER_SYSTEM_PROMPT`）、独立工具子集（`worker_tools`）、`recursion_limit`，以及**跨进程审批回流**（`_await_main_decision`，已测）。

"skill 在 fork 里跑"一次解掉三件事：

1. **中间步骤不污染主对话**（`dispatch_subtasks` 已在享受这条）；
2. **约束文本的落地问题自动消失**——领域纪律进的是 **fork 的 system prompt**，主 agent 根本不必装它，也就不必回答"主 agent 怎么知道该读纪律"；
3. **fork 本身就是隔离边界 = 天然的 reflect 载体**。[[docs/TODO]]「反思节点」那条想做"动手前先审一遍"，而 fork 已有"另一个上下文、另一个系统提示、独立预算"的壳——**在隔离子 agent 里跑 skill 的中间步骤，本身就是一次结构性反思**，不必再叠一层模型调用。**两条设计在这里打通。**

**但它卡在一个已有里程碑上**：worker 现在**结构性地只读**（`worker_tools` 白名单排除了写与命令）。skill 要能改东西，就得下放写权限——那正是 [[docs/MULTI_AGENT]] §6 记的"子 agent 写权限下放"。**所以这条取决于那个里程碑，不是 skill 一期能落的。**

---

## 7. 与外部实现的对照

记下来，免得以后重新论证一遍：

| 维度 | 社区做法 | 本项目取哪条 |
| --- | --- | --- |
| **懒加载** | Claude Code / Codex：**元数据常驻、正文懒加载**；`opencode-lazy-loader`：**server 进程懒起** | 两条都要：`get_skill` docstring 常驻当目录，server 按需起（§3.3/§3.4） |
| **目录发现** | **标配，且都按工具 / 中立命名空间**。Claude Code `.claude/skills/` + frontmatter；Codex `~/.codex/skills/` 自动扫描；OpenCode 三源扫（`.opencode/skills` / `.claude/skills` / `.agents/skills`） | **部分跟**：本项目目前**单源** `<项目根>/skills/`（2026-09-12，§8.2）；多源（工作区/用户全局/兼容别家路径）推迟，届时还要过沙箱这关（§9） |
| **Skill 与 Tool 是否分离** | **有争议**。Claude Code 最重要的决策是**彻底分开**（Skill 不是 MCP 的包装，另用 `allowedTools` / hooks 做**调用控制**）；Codex 的 skill ≈ **知识型**（目录 + SKILL.md，无能力型内嵌）；OpenCode 有一手 skill 支持，但"skill = 按需 MCP server"是**社区插件**（lazy-loader）做出来的 | **取社区插件这条**（= 能力型）。理由见下 |

### 7.1 为什么本项目敢跟 Claude Code 反着走

Claude Code 能把 Skill 与 Tool 彻底分开，是因为**它的权限系统在外部且全局**——审批不归 skill 作者管，skill 就只有"知识"这一半可管。

而本项目的**审批策略是逐工具的作者产物**（`tool.json` 每个工具一行），所以"这个领域的纪律"与"这个领域的能力"**本来就属于同一个编写单位**；硬拆开反而别扭——纪律得另找地方安放，且没有任何机制保证模型在使用 github 工具时读过它。

> **这是在下注，不是定论。** 证伪条件很明确：**若模型在使用某领域工具前确实会可靠地去读那份独立的 skill 文档，绑定就不必要。** 本项目恰好能测——`evaluation/` `[已落地]` 就是干这个的。等 GitHub 工具有了，跑一个"工具与纪律分开放"的任务，看模型守不守纪律，就能结掉这个争论，而不是靠推理。

### 7.2 不塌缩成"lazy-loader 重新实现"的只剩一样东西

**约束三分法（§4）。** 社区里没有等价物——Claude Code 的 `allowedTools` / hooks 是**调用控制**，不是"领域纪律随能力一起到达"；lazy-loader 的 skill 内嵌 MCP 定义能带 **env 声明**，却不清楚怎么承载**约束文本**。所以本设计的差异化**全押在那张三分表上**：写不清，就真的只剩重新实现一遍 lazy-loader。

---

## 8. 归位（代码落点）

### 8.1 统一布局：一个技能 = 一个目录（2026-09-11 定）

```
skills/<name>/
├── SKILL.md      # 说明 + 约束（软约束正文、硬约束的声明）——**这个领域的单一来源**
├── server.py     # 可选：MCP server（工具实现）。**有它就是能力型，没有就是知识型**
└── ...           # 可选：env / 启动配置
```

**两种载荷共用一套布局**，差别只在有没有 `server.py`——这比 §2 里"两套东西"的讲法更实用：
目录形态一致，扫描/发现逻辑就只有一套，**"能力型 / 知识型"退化成"带不带 server"这一个判断**。

- **不入 `mcp_service/`**：那个包是 Theta 的**内置核心能力**（file / terminal / git / web，随 agent 出厂、永远在）；skill 的定义恰恰相反——**可插拔、按需拉起**。混在一起就没有"哪些是核心、哪些是可加载的领域包"的边界了。更实际的是 `SKILL.md` 会**无处安放**，技能被劈成两半。
- **模块名固定 `server.py`** 而不叫 `<name>_skill.py`：启动器才能**通用化**——`python -m skills.<name>.server`，不为每个技能记模块名（技能清单只声明 `name: github`）。
- **顶层 `skills/` 用命名空间包**（无 `__init__.py`，同 `app`），保持轻。

### 8.2 源：**只有一个**（2026-09-12 改；此前规划过两个）

| 源 | 位置 | 内容 |
| --- | --- | --- |
| **技能源** | `<项目根>/skills/` | 随 Theta 出厂；首个 = `skills/github/` |

**§8.2 此前规划过第二个"工作区源" `<workspace>/.theta/skills/`（用户为自己项目写的技能），
2026-09-12 决定先不做。** 理由与代价都记下：

- **少掉两处复杂度**：①"同名时谁覆盖谁"的优先级规则；②"工作区**外**的目录要不要因此变得能被文件
  工具读到"这层沙箱边界问题（§9 那条）。
- **代价说清**：用户**不能**只在自己项目里写技能就用上——加技能要动 Theta 仓库。等真出现这个需求再
  加回来，那时重新论证比留一段没人跑的代码便宜。
- **`python -m skills.<name>.server` 因此对唯一那个源成立**（§8.1 的启动方式保住了）。此前双源版有个
  隐藏矛盾：`.` 开头的目录名不是合法 Python 模块名（`python -m .theta.skills.x.server` 报
  "Relative module names not supported"），而 §8.1 又假定按模块名启动——单源后此矛盾消失。

> 当初给工作区源定的 `.theta/` 命名空间（跟生态惯例、不占用用户项目里的通用名、与内置源不重名）
> 三条理由本身没问题，只是现在没有那个源，这些论证一并留到加回来时再启用。

### 8.3 其余落点

- **别和 `mcp_service/git.py` 重复**：那个管**本地** git（status/diff/log/add/commit/switch/列分支）；GitHub skill 只管**平台侧**（PR / issue / review / 推送 / 不克隆读远端代码 / 搜索）。"本地提交"永远走 `git_commit`。
  **判据（2026-09-13 去重时钉下）**：① 动的是本地仓库还是远端？② 要不要本地仓库状态（当前分支 / 未推送提交 / remote 配置）？③ 语义对象是"提交·分支·工作树"还是"PR·issue·release"？按这三条，技能侧砍掉了 `github_branch_list`（`git_fetch` + `git_branches` 已能回答这件事）与"只建远端分支"（`git_switch` 建本地 + push 首推即建远端）；`github_pr_diff` / `github_pr_files` **保留**——它们管的是**区间** diff，`git_diff` 看不到区间、更看不到别人的 PR。
  **远端交互族（`git_fetch` / `git_pull` / `git_push` / `git_clone`）都在核心 git 服务**（2026-09-13 定，含一次改口）：`git_clone` 补的是"要不要一份完整工作树"这件事——只想看一眼代码该用平台侧的远端读工具（不克隆），真要改代码、跑测试才克隆。它是 git.py 里**第一个创建新目录**的工具，所以**沙箱校验（目标必须落工作区内）必须它自己查**（别的工具都靠 `_ensure_known_repo` 兜着），克隆成功后还要刷新"已知仓库"缓存，否则 `list_repos` 认不出它。
  - **怎么做的**：参数是**远程仓库的完整地址**（`git push <url> <refspec>` 本就收 URL，不必有 remote 配置），凭证由**设备**给（credential helper / ssh-agent），带凭据的 http(s) 地址被**直接拒绝**；refspec 写 `refs/heads/<name>:refs/heads/<name>`（推的必须是你**本地的那个分支**，不是 HEAD），**不带 `-u`**——所以它不写 `.git/config`、不设上游、不动工作树（"本地状态只读"这条是结构性成立的）。
  - **改口的理由（前一个决定是"push 归平台技能，因为凭证是平台的"）**：那个推理把两件事捆在了一起。push 确实要凭证，但**凭证不需要由 git 服务持有**——地址是调用方给的、认证在设备上，`git_push` 自己一个 token 都不认识。于是"凭证中立"与"能推送"并不冲突：**它对 github / gitee / 自建一视同仁**，将来加 gitee 技能时它一行都不用改。反过来若留在技能里，技能就得在子进程里自己拼 URL 与认证头，那才是把凭证处理散到每个平台技能里。
  - ⚠️ **代价（如实记）**：**受保护分支的"工具内直接拒绝"没有落点了**——`git_push` 是平台无关的，它不认识 GitHub 的 protected branches 配置。**已定（2026-09-13）：这一条不做**，理由见 §13.9（保护分支是平台侧规则，服务端自己会拒；在平台无关的工具里再判一次既重复又判不准）。剩下的是"软约束 + 审批面板上可见 `url`/`branch`"。
  - 另一条副作用：不带 `-u` 就不设上游，之后 `git_pull` 不知道该跟踪谁——这一条**如实写进了回执**（让模型知道要显式给 remote/branch），配置归人。
  - **测试**：`tests/test_git_push.py`（8 例，真推到本地裸仓库、不联网）——成功推 + 不写 `.git/config`；带凭据的地址被拒；空地址 / 本地无此分支 / 游离 HEAD / 一行提交都没有的仓库各一例；**非快进被 git 拒绝且 stderr 回显**。
- **`get_skill`**：`app/agent/tools.py`，作为新的编排工具组（或并入现有编排工具组），`source="skill"` 登记进 `tool.json` 并加进 `ReviewNode.ORCHESTRATE_SOURCES`。
  > **两种 source 是**有意的**、跟现有约定一致**：编排类用**单词类别名**（`plan` / `notes` / `dispatch` / `memory` / 新的 `skill`），普通工具用**路径式名**（`mcp_service/file_io`、新的 `skills/<name>`）。所以 `get_skill` 走 `skill`（→ 分流进 `OrchestrateNode`），而 GitHub 的各工具走 `skills/github`（**不命中** `ORCHESTRATE_SOURCES` → 进普通队列 → `ToolNode`）。这正是想要的：`get_skill` 是编排动作，它带来的工具不是。
- **审批策略**：skill 的工具**预先登记进 `app/agent/tool.json`**（即使 server 没起）——保住"审批策略集中一处、可审计"。于是 **skill = 工具 + 它的 `tool.json` 登记行 + 指导文本**，三件套；加一个 skill 要动 `tool.json` 是**特性不是负担**。**登记表只在 `app/agent/tool.json` 一处**（2026-09-13 起）：技能正文不再抄一份——`SKILL.md` 只写**领域**内容（能力 + 纪律 + 凭证），项目侧的映射（哪个工具 `need_review`、`source` 是什么）不属它管，抄一份只会两处走散。
- **知识型的注入**：与能力型**同一条通道**（§3.5）——目录（name + description）进 `get_skill` 的 docstring；正文由 `get_skill` 写 state、`LLMNode` 拼进系统提示。**不另走 `read_file`**：让模型自己去捞，正文会落在**对话历史**里（会被 compact 折掉、随轮次稀释）；进**系统提示**才每轮都在、模型无法忽略。扫描 / 解析落在新模块 `app/resource/skills.py`，与 `memory.py` 并列（同属"工作区内容 → 注入"的通道）。**独立成模块的判据**：消费者跨文件 >1 且自身逻辑成规模——`skills.py` 的消费者是 `tools.py`（`get_skill`/`drop_skill` 取目录与正文）、`nodes.py::LLMNode`（注入块）、构图期（目录烤进 docstring），跨三个文件，故独立；反例见下表 `profile.py` 那行。

---

## 9. 待定 / 待验（实现前须钉死）

**绑定与生命周期**

- ~~**(a) vs (b)**（§3.4）：先做 (a) 验证价值，还是直接上 (b)？~~ → **已决（2026-09-12）：(a) 不做，直接上 (b)。** 与 §3.4 的口径就此统一（此前 §3.4 已定 (b)、§9 却仍留"先做 (a)"，是两处没对齐）。
  > ⚠️ **代价说清**：能力型 skill 的"工具按需出现"因此**一期就得有 §3.4 的会话级注册表**，没有"先拿常驻 schema 探路"这条退路。若想一期避开注册表，唯一的路是**改做知识型优先**（知识型无工具，只动 state + `LLMNode`）；但 §2.1 定的优先序是能力型（只有它解决"能力当前根本没有"）。**两条不能都要**——先做哪个，见 §9 末「一期切法」。
- ~~**idle timeout 的取值与触发点**，以及"拆 server 同时注销工具"在 (b) 下怎么与注册表对齐。~~ → **已决（2026-09-12）**：核心四件套**不做空闲回收**（常驻到宿主关闭，§12.3 决定 4），所以没有"取值与触发点"这回事；"拆 server 必须同时注销工具"在 **§13.6 的 `unload`** 里落（技能运行体的关闭是 §13 自己的事——见 §13.6 括注）。

**约束承载**

- ~~`resources` 管道~~ → **已决（2026-09-11）：一期不做 `get_resources`，软约束改由 host 读 `SKILL.md`。**
  原先把它列成"待定、且有廉价替代（`github_guide()`）"是**自相矛盾**的——§4 的整张三分表把软约束押在 `resources` 上、§0 又说"差异化全押在那张表上"，那它的承载就**不是可选项**。而两条分支都不可接受：走 `github_guide()` → 约束混进工具列表、要模型主动去调、**到达不可靠**；一期都不做 → 三分法只剩两分，§0 的"差异化"在一期不成立。
  **解法**：把"承重墙"从 `resources` 这个**传输机制**，挪到"**同目录 + 同加载时机 + 进系统提示**"（§2.2 / §3.5）。理由：① **知识型根本没有 server**，永远不可能暴露 MCP resource——若软约束被定义为"必须走 resources"，知识型就没有交付通道，**这个反例本身就说明 resources 不是承重的那个东西**；② host 直读 `SKILL.md` 与走 resources **效果等价**（都是"取到文本 → 注入系统提示"），差别只在文本过不过一遍协议；③ 关键是文本落在**系统提示**里——模型无法忽略；`github_guide()` 死在"**到达不可靠**"，不是死在"不同包"。
  **什么时候真的需要 resources**：将来出现**远程技能**（HTTP MCP，host 读不到文件）时——那时它才是必需的。

**环境与凭证**

- **env 声明 / 凭证转发**：GitHub 要 token，而 `_build_servers` / `_worker_child_env` 都是**逐 server 硬编码 env、整体替换**。通用化就得有"这个 skill 需要哪些 env 键"的声明，由 launcher 从 `get_settings()` 取明文转发（同 TAVILY_API_KEY 的做法）。**不钉这条，skill server 拿不到凭证。** → **形态已定于 §13.5**（`skill.json` + host 侧白名单 + 缺键拒绝加载）；下面两条语法讨论作为 §13.5 的依据保留。
  - **语法可直接抄 `opencode-lazy-loader`**：skill 的 MCP 配置里写 `${VAR}` / `${VAR:-default}` 展开——简洁，且天然吻合本项目"键不可缺、值可空"的 `.env` 约定（空值 = 当未设置）。
  - **但展开必须白名单**：`${VAR}` 若能被 skill 任意点名，等于 skill 配置可读**进程里任何一个环境变量**（含 `CHAT_MODEL_API_KEY`）。只转发 skill **显式声明、且落在已知配置表**里的键，**不做通用 env 透传**。
- ~~**依赖隔离**（§5.1）：skill 的三方库进主 `pyproject.toml`，还是做独立 venv / `uvx` 启动？~~ → **已决（2026-09-12）：同一个 venv**（所有 server 共用 `sys.executable`），skill 的三方库进主 `pyproject.toml`；**不做独立 venv / `uvx`**。
  于是"加一个需要新库的 skill" = 动主 `pyproject.toml` + `uv sync`——与"加 skill 要动 `tool.json`"（§8.3）**同性质**：都收在单一清单里、都可审计。代价是 skill 在**依赖这一维**上不可插拔（可插拔性只保住"加目录不改主流程"这半边），记在案。

**目录与加载**

- ~~**多源目录扫描的沙箱边界**~~ → **已推迟（2026-09-12）**：技能源改成单源（项目根 `skills/`，§8.2）后，暂时不存在"工作区外的源"。将来加回工作区源/用户全局源（`~/.theta/skills`）或"兼容别家路径"（`.agents/skills` / `.claude/skills`）时，这条重新成立：`file_io` 沙箱**只认 `WORKSPACE_PATH`**，工作区**外**的源**不能**因此变得能被文件工具读写——**扫描它可以，读取得走专用通道，别并进 file_io**。
- ~~**两源查找**（内置 vs 工作区）~~ → **已决（2026-09-12）：单源**，只扫 `<项目根>/skills/`。
- **知识型的 `description` 预算与校验**：`description` 决定模型会不会、该不该用——要写"什么时候用 + 解决什么"而非"是什么"；清单注入要设上限（条数与总字符，口径同 `MEMORY_INJECT_CAP` / `AGENT_MD_INJECT_CAP`）；坏 skill（缺 description / 无 SKILL.md）跳过并告警。
- ~~**frontmatter 解析**：本项目代码里还没有 YAML 依赖（`yaml` 目前只是被别的包传递装上）。用最小实现还是显式声明 PyYAML，须定——**别默认蹭传递依赖**。~~ → **已决（2026-09-12）：自写最小实现**（`skills.py::_split_frontmatter`），只认 `---` 之间的单行 `key: value`。不引 PyYAML、不动 `pyproject.toml`——技能的 frontmatter 只有 `name` / `description` 两个键，用不上完整 YAML。**没有 frontmatter 也不报错**：`name` 退化用目录名（它本来就可以不解析），`description` 缺失才跳过并告警（那是给模型看的、不能缺）。

**范围**

- ~~**一期切法：能力型优先，还是知识型优先？**（2026-09-12 记，(a) 被否决后新浮出来的）~~ → **已决（2026-09-12）：先做 `get_skill`（+ `drop_skill`）这套编排工具本身**，即两型**共用的那条通道**（§3.2 / §3.5），**不在两型之间二选一**。
  理由：`get_skill` 对两型是同一入口、同一套机制，差别只在能力型多一步"起 server + 注册工具"（§2.1）。先把工具与通道做出来，两型之差就退化成**一个可后加的分支**，而不是一期押注；**一期因此不带 §3.4 的会话级注册表**（设计里最重的一块）。
  两个随之被**提前**的"须定"，都已解：① **frontmatter** → **自写最小解析**（见下条）；② **靶子** → **不需要另造**——`skills/github/` 没有 `server.py`，按 §2.1 的定义它**当前自动就是知识型**，扫描一接上就出现在目录里，可直接端到端验证（正文声明的那些工具当时不存在，是二期的缺口，不是一期的；清单 2026-09-13 与 `git.py` 去重后为 18 条，其中 8 条已落地）。
- **worker 要不要能用**：worker 是只读资料收集器，让它调 GitHub 只读工具（看 issue / 搜项目）有价值；但给它就多一条加载路径、且它拿不到主 agent 的技能目录。**先不给**——而且这条**免费**：`worker_tools` 是按 `source` 白名单过滤的（`_WORKSPACE_SOURCES` / `_WEB_SOURCES`），skill 工具的 source 取 `skills/<name>`，**天然不命中 → 结构性地进不了 worker**，一行代码都不用写。（将来要开，也是"往白名单加一条"这种显式动作。）

---

## 10. 成熟后的一条归位

`PROMPT_COMMANDS` 里 `/init`、`/fast readme` 的载荷（`app/platform/commands/prompts.py` `[已落地]`）本质上就是"**用户手动触发的一次性技能**"。skill 机制跑通后应把它们**收敛成 skill**，`/` 命令退化成"按名触发某 skill"的薄壳——与项目一贯的"清单只有一个来源"（命令表唯一、`tool.json` 唯一）一致，也免得两处提示词各改各的、慢慢走散。

---

## 11. 一期落地（2026-09-12）

**范围**：只做两型**共用**的那条通道。**不含**能力型（`server.py` 生命周期 / 会话级注册表 / idle 回收 / env 转发）。

| 落点 | 内容 |
| --- | --- |
| **`app/resource/skills.py`**（新增） | 扫描 / 解析 / 渲染的**只读单点**：`scan_skills`（无参，只扫项目根 `skills/`）/ `read_body` / `catalog_block` / `skills_block` / `bodies_size`。只依赖 stdlib |
| `app/agent/state.py` | 加 `loaded_skills: List[str]`（无 reducer）——**只存名字** |
| `app/agent/tools.py` | `skill_tool` 组：`get_skill`（async，读盘走 `to_thread`）/ `drop_skill`（同步，纯 state）；**两者都不注入 `workspace`**（源在项目根）；`skill_tools_for()` 把目录 `model_copy` 进 docstring |
| `app/agent/tool.json` | `get_skill` / `drop_skill` → `{"need_review": false, "source": "skill"}` |
| `app/agent/nodes.py` | `ORCHESTRATE_SOURCES` 加 `skill`；`OrchestrateNode` 切片合并泛化；`LLMNode` 注入技能块 |
| `app/agent/graph.py` | `skill_tools_for(workspace_path)` 进 `all_tools` 与 `OrchestrateNode` |

**三条实现上的判断**（写下来备查）：

1. **铁律是结构性的，不靠纪律**：state 只存名字，正文从不进 ToolResult（成功回执里没有正文），所以"两份拷贝"在结构上不可能出现，卸载就是删一个名字。
2. **切片合并泛化**：`OrchestrateNode.__call__` 原先硬编码 `if "current_plan" in result`；现在按 `_EXTRA_SLICE_KEYS` 白名单循环，且**只回传本回合真被写过的键**——`working` 由 `state` 复制而来，所以"键存在"≠"被写过"，用 `written` 集合区分。`current_plan` 既有行为**未变**（恒带回）。加新切片 = 改这一行 + `state.py`。
3. **技能块独立于 `inject_session_context`**（§3.5 决定 5）：它不是"这个项目/用户是谁"那类会话背景，而是模型自己按需取来的领域纪律。worker 没有 `get_skill`，`loaded_skills` 天然为空，无需额外开关。

**测试**：`tests/test_skills.py`（扫描 / 解析 / **路径穿越被挡** / 预算 / 渲染与截断告警）、`tests/test_skill_tools.py`（登记与路由、加载 / 幂等 / 未知 / 预算拒绝、切片合并与同回合链式）、`tests/test_memory_injection.py` 补三例（注入位置、未加载不注入、独立于开关）。

**已知错配（一期当时）**：`skills/github/SKILL.md` 正文声明的工具此刻并不存在——它是能力型的稿子，一期验证的是**通道**（目录 → 加载 → 注入 → 卸载），不是那些工具。**二期已落地其中 8 条只读工具**（§13）；清单 2026-09-13 与 `git.py` 去重后为 **17 条**（8 ✅ + 5 ⏳ 只读 + 4 写——砍掉 `github_branch_list`、`github_branch_create`，并把 `github_push` 移进核心 git 服务，理由见 §8.3）。**⚠️ 此处是当日（2026-09-13）的清单快照，已被后续实现取代**：落地的最终形态是 **14 只读 + 3 写 = 17 条**（写工具里 `github_pr_merge` 明确不做），见 §13.10。（本节上文另有一处"18 条"是同一日的更早口径，同已作废。）

**二期（未做）**：能力型 `server.py` 生命周期、会话级工具注册表 + 动态 `bind_tools`、env 声明与白名单转发、`/skills` 控制面命令、§10 的 `PROMPT_COMMANDS` 收敛。**前置里程碑 §12（MCP 运行体常驻化）已落地（2026-09-12）**，二期设计见 §13——它骑在那批常驻运行体上。

---

## 12. MCP 运行体常驻化（**通用基建，非 skill 专属**）——**已落地（2026-09-12）**

> **位置说明**：这一节住在 skill 设计文档里，是因为它由 §3.3 的需要挖出来、二期骑在它上面；但它的**主体是 MCP 层**，不是 skill。将来它若长大（远程传输、可观测性），应独立成篇，不必因为它在本文里就把它读成 skill 的一部分。
> **本节已落地**（落点见 §12.6、测试见 §12.7）。**改这套机制前先读 §12.2 的承重约束**——它决定了这套东西只能长成现在这个形状；初稿那份"池 + 事后惰性回收"的设计已被它证伪。

### 12.1 改造前：没有池，而且已经坏了（**实测**，不是推理）

`app/platform/mcp.py::load_mcp_tool` 用 `client.get_tools()` 拿工具。适配器这条路径的语义是 **每次调用新建会话**：

- 源码：`langchain_mcp_adapters/tools.py` 里 `if session is None:` → `async with create_session(...)` + `await tool_session.initialize()`（**传了连接、没传会话**时走这条）；`client.get_tools()` 正是只传连接。
- 观测：一次 `load_mcp_tool` 后连续调三个工具，服务端日志出现**三组** `ListToolsRequest` + `CallToolRequest`。

stdio 下"新建会话"= **重起一个 `python -m mcp_service.*` 子进程 + 重新握手**。`_MCP_TOOLS_CACHE` 只缓存了**工具清单**，没缓存会话——它解决的是"langgraph dev 每次进图重拉子进程"，**没解决"每次调用重拉子进程"**。

**后果不只是慢，是一处真实缺陷**（实测原文）：

```
start_process(...)  → "进程 p1 仍在运行（常驻）：已在后台常驻"
下一次调用 process_list → "当前没有受管进程。"
```

`mcp_service/terminal.py` 的整套跨调用进程管理（`start_process` → `process_wait`/`process_read`/`process_kill`）在这个语义下**结构性失效**：句柄活在那个已经退出的 server 进程里，下一个调用是全新的空注册表。模型被告知"已常驻"，实际既认不到、也（子进程退出时 atexit 已）没人管。

> 这条**独立于 skill** 就该修。skill 只是让"必须有池"这件事变得不可回避。

### 12.2 承重约束：**anyio 的 cancel scope 与 asyncio Task 绑定**（本节最重要的新知）

三条事实叠在一起，决定了唯一可行的机制形状：

1. **anyio**：退出 cancel scope 时校验宿主 task —— `anyio/_backends/_asyncio.py` 在
   `current_task() is not self._host_task` 时抛
   `RuntimeError("Attempted to exit cancel scope in a different task than it was entered in")`。
2. **langgraph**：`pregel/_executor.py` 经 `run_coroutine_threadsafe` → `langgraph/_internal/_future.py`
   的 `loop.create_task(...)` 跑节点协程——**每个节点执行都在新 Task 里**。
3. **MCP**：会话内部就是 `anyio.create_task_group()`（`mcp/client/stdio/__init__.py`、
   `mcp/shared/session.py`），stdio 子进程挂在里面。

**推论（改这套机制前必须记住）**：

- 会话**建在** ToolNode 那个 Task 里，就**不能在**别的 Task 里关。所以初稿那份"池 + 事后惰性回收"
  **不可实现**：清理动作会在退出路径上抛 RuntimeError，而且都在最难查的位置（TUI 退出、
  evaluation 的 per-task finally——后者还会顶掉在传播的异常、打断整批评估）。
- **复用**会话是安全的，只有**建与关**受约束。于是唯一可行的形状是**专职 owner task**：建、调用、关闭
  全部发生在它自己的 Task 里（§12.4）。
- 顺带解释了一件事：适配器"每次调用新建会话"很可能不是疏忽，而是这个执行模型下最省心的写法。
  我们不是在修一个 bug，是在补一个**只有常驻才能有**的能力。

### 12.3 常驻模型：四个已定的决定（2026-09-12）

| # | 决定 | 理由 / 含义 |
| --- | --- | --- |
| 1 | **机制不在平台里，生命周期归宿主** | 机制的落点是 `app/platform/mcp.py`（2026-09-21 从 `app/agent/mcp.py` 整体搬来——判据见 docs/ARCHITECTURE.md §4 的 A/B/C；落点本身仍**不新增模块**）；"何时关"由宿主调 `close_session_pool` / `close_all_pools`。理由：worker 子进程（`mcp_service/sub_agent.py`）也要用同一套机制而**它没有平台**，机制不能寄生在平台里；关闭时机只有宿主知道 |
| 2 | **作用域 = (工作区, 会话)** | 与 `session_db_path` 同口径。terminal 的受管进程表是**会话语义状态**，按工作区共享就会跨会话泄漏（A 会话起的进程 B 会话能看见、能杀） |
| 3 | **懒起：首次真调用时** | 空会话零进程。schema 另走临时会话（按工作区缓存），所以构图期仍 spawn 一遍（与改造前相同），但**常驻体一个都不预养** |
| 4 | **全部不回收（常驻到宿主关闭）** | 没有空闲计时器、没有 `busy` 计数、没有 sweep。terminal 的常驻进程因此不会被静默杀掉；代价是"进程只增不减"的担忧整体**交接给 §13**（卸载技能必须关运行体） |

> **正面记下这次改口**：§3.3 写过"不是'懒起后常留'"。当时的假设是"skill 一多进程爆炸"；而 core 四件套的
> 规模固定（每 (工作区, 会话) ≤4，按需起），且 terminal 的**跨调用状态只有常驻才能成立**。所以结论改了，
> 理由是新的，不是被忘掉的。

### 12.4 实现

#### owner task：每 (工作区, 会话, server) 一个（`_ServerWorker`）

任何 Task 都能 `call()`，但它只是把请求投进队列、等 owner 执行完：

```
call(tool_name, args)  →  _ensure_started()  →  队列投 (tool, args, fut)  →  await fut
                                                          │
_run()（owner task）：  queue.get() → 建会话（懒）→ 执行 → fut.set_result
                        收到 _SHUTDOWN 哨兵 → 出循环 → stack.aclose()   ← 与创建同一个 Task
```

- **串行化是诚实的**：一条 stdio 管道本就串行；每 server 一个 owner，互不阻塞。**因此不需要 `busy`
  计数、也不会有"回收正在用的会话"**——调用在飞时 owner 根本不在等队列。
- **关闭走哨兵而非 `task.cancel()`**：取消会腰斩 `finally` 里的 `aclose`（子进程可能残留）；
  哨兵让它走正常退出路径，且在途调用会被先处理完（队列有序）。
- **自愈**：传输类异常（`anyio.ClosedResourceError`/`BrokenResourceError`/`EndOfStream`、`McpError`）
  → 关掉当前会话 → 重建 → **重试一次**。改造前"每次调用重开进程"天然有这个能力，常驻后必须显式补回，
  否则一个坏会话会让该会话**永久残废**。**业务错误不在此列**——`mcp_service` 的 guard 把工具失败收成
  普通返回值，根本不以异常形态冒到这里。

#### shim：工具**不绑会话**（关键解耦）

`load_mcp_tools(session)` 返回的工具把会话**闭包**在 coroutine 里——会话一关，工具立刻报
`ClosedResourceError`。所以不能"把会话连同它的工具一起放进池"（那样回收即废工具）。

本项目的做法：`load_mcp_tool` 返回**自造的 `StructuredTool`**，只带 schema，执行时才向 owner 要会话：

```
async def _call(**kwargs):
    worker = get_worker(workspace, session, spec.server, connection)   # 调用时解析，不闭包池对象
    return (await worker.call(spec.name, kwargs), None)
```

三条**不能踩的坑**（改了就与改造前的模型可见文本不一致）：

- **不给内层传 `tool_call_id` / `config`**：传了之后 `_format_output` 会提前返回
  `ToolMessage(status="error")`，`format_tool_result` 的 content-block 分支失效，模型可见文本变样；
- **不设 `handle_tool_error`**：内层（adapter 原生工具）已经吞掉 `ToolException`，这一层再也见不到它；
  设 True 只会把漏出来的裸 `ToolException` 降级成信息更少的字符串；
- **`args_schema` 原样搬运** MCP 的 `inputSchema`：不规范化成 pydantic、不 snake_case、不重排
  （server 顺序 = connections 顺序、server 内 = list_tools 顺序）——schema 一变就污染 prompt cache
  与评估复现。

**形状契约已由测试锁死**：shim 的返回值与真 adapter 工具**逐字节同形**（四种变体：普通 content-block、
`structuredContent` 非 None、`isError=True`、空 `content=[]`），模型可见文本因此不变。

> 顺带修正一处**既有**认知：adapter 工具在 `tool_call_id is None` 时，`_format_output` 就已经丢掉了
> artifact 与 `status="error"`——那是改造前就有的行为，不是 shim 引入的。

#### 会话 id 的传递（决定 2 的实施前提）

`session_id` 成为图的**构造期参量**（与 `workspace_path` 同级），不做运行时注入：

| 路径 | 会话来源 | 关闭时机 |
| --- | --- | --- |
| **TUI** | `build_session_runtime(workspace, session_id)` 原样传入 | ① `/session`、`/new session` → `switch_session` 关**旧会话**；② `run()` 的 finally 关全部（先 `await` 被取消的 turn，避免与在途调用并发踩同一运行体） |
| **evaluation** | `build_eval_graph(workspace, session)`，调用方传 `task.name`（与 state 的 `session_id` 一致） | 每任务 finally —— **必须在 `shutil.rmtree` 之前**（见 §12.5） |
| **worker 子进程** | `sub_agent.py` 先生成 uuid 再 `load_mcp_tool` | 不显式关：父进程死后 stdin EOF → 子进程正常退出 → `atexit` 照跑 |
| **langgraph dev** | 零参入口 → `None` | **没有关闭时机**（该路径无 finally）——已知偏差，见 §12.5 |

### 12.5 常驻的后果与代价（用户可见，别当实现细节）

1. **配置在启动时刻固化**：env / cwd / `WORKSPACE_PATH`（`mcp_service/file_io.py` 在 **import 时**校验）
   都是建运行体时定的 → 改 `.env`（如换 `TAVILY_API_KEY`）需重开会话。**补一句更准的**（2026-09-13 核）：
   `get_settings()` 是**进程级 `lru_cache`**，全仓没有一处 `cache_clear` —— 所以 host 自己读到的
   那份（含技能凭证、`SKILL_ENV_WHITELIST`）**要重启进程**才更新；"重开会话"只解决运行体那半边。
2. **Windows 下"活着的 server"会让工作区删不掉**：server 的 cwd 就是工作区。所以 evaluation 里
   "先关池、再 rmtree"是**硬顺序**，不是优化。（`create_session` 的 teardown 会关 stdin、等进程退出、
   超时后 SIGKILL，故 `aclose` 返回即子进程已收。）
3. **`terminal._PROCS` 成为会话级状态**：这正是本次要修的东西，也意味着长会话里受管进程会累积——
   模型需自己 `process_kill` 收尾，退出时 terminal 自己的 `atexit` 兜底。
4. **进程上界**：每 (工作区, 会话) ≤ server 数（当前 4），**按需起**（只用 file_io 就只起一个）。
   TUI = 1 会话；evaluation 靠 per-task 关闭压回；langgraph dev 会累积（无关闭点）。
5. **langgraph dev 的偏差**：该入口零参、没有会话，故所有会话共享一组运行体、且随 server 生命周期累积。
   开发工具，接受并记录在案。
6. **可观测性**：四条 `logger` 生命周期日志——**建**（server + 工作区 + 会话 + 工具数）/ **关闭** /
   **传输失败重建** / **更换事件循环丢弃**。"为什么有进程、这次为什么慢"要能答。

### 12.6 落地清单（2026-09-12）

| 落点 | 内容 |
| --- | --- |
| **`app/platform/mcp.py`** | 三件事合一：`_build_servers`（连接配置）、`_tool_specs` + `_make_shim` + `load_mcp_tool`（工具加载，**不绑会话**）、`_ServerWorker` + `get_worker` + `close_session_pool` / `close_all_pools`（运行体常驻）。原 `_MCP_TOOLS_CACHE` 从"缓存工具对象"改成"缓存 shim"，另加按工作区的 schema 缓存 |
| `app/agent/graph.py` | `get_main_agent_graph(workspace_path=None, session_id=None)` → 传给 `load_mcp_tool` |
| `app/platform/runtime.py` | `build_session_runtime` 把会话 id 传进构图 |
| `app/platform/loop.py` | 两个宿主挂点：`switch_session` 关旧会话、`run()` finally 关全部（`app.agent.*` 懒加载，保持"无 .env 可顶层 import 基座"） |
| `evaluation/runner.py` | `build_eval_graph(workspace, session)`；每任务 finally **先关池后 rmtree** |
| `mcp_service/sub_agent.py` | 会话 id 提前生成（一处顺序调整） |
| **`mcp_service/terminal.py`** | **一行没改**——跨调用进程管理自动恢复，这是"通用池化"相对"只为 skill 做池"的直接收益 |

### 12.7 测试与验证

- **新增 `tests/test_mcp_pool.py`**（13 例，不拉真 MCP）：复用不重起、**关闭发生在 owner 的 Task 里**
  （假会话的 `__aexit__` 断言创建者一致——把承重约束变成红灯）、按会话隔离、关闭后透明重建、
  传输失败自愈、调用方被取消不打死 owner、**shim 与原 adapter 工具形状逐字节同形**（4 变体）、
  保名保序 + `worker_tools` 过滤照旧、shim 缓存按会话分键、关闭时路径归一化。
- **端到端验收**（缺陷判据，实测通过）：`start_process` 起常驻进程 → **下一次调用** `process_list`
  认得它 → `process_read` / `process_kill` 通；`_WORKERS` 里只有真正用到的 server（懒起成立）；
  关池后注册表空、再调用透明重建。
- **全量回归**：`python -m pytest` → **295 passed / 4 skipped**（无回归）。

### 12.8 遗留与交接

- **不做空闲回收**（决定 4）⇒ 这条交接给 §13：**`drop_skill` 必须关掉该技能的运行体**，否则加载过的
  技能会永久占着进程（这正是 §3.6"进程只增不减"担忧的新落点）。
- **terminal 受管进程的自动上界**：现在靠模型自觉 `process_kill` + 退出时 atexit。有真实需求再单列。
- **传输面**：本设计假定 stdio。远程 / HTTP MCP（[[docs/MULTI_AGENT]] §9 的未落地项）进来时，
  运行体的键与生命周期要重看。
- **`_spawn_subagent_worker`（`app/agent/tools.py`）刻意没接进来**：它自建 client、每子任务一个进程，
  那正是子任务要的**进程隔离**；接进池会让子任务间状态互相污染。

---

## 13. 能力型：技能自带的工具（**已落地：14 只读 + 3 写**，2026-09-12 / 2026-09-14）

> **本节已落地**（落点见 §13.7、测试见 §13.8）。**机制在实现时被简化过一次**：原稿打算"造一个会话级
> 注册表对象、注入三个节点"，实现时发现 §12 的常驻运行体本来就是"任意 server 的运行体"——技能只是
> 第五类 server。于是改成 **技能 = 按需加入的 server**，mcp.py 只多一条"运行中增删 server"的能力。
> 下面按落地后的口径写；原稿的推理只在容易误解的地方留一句对照。

### 13.1 一句话

**能力型 = 知识型 + 一个进程**（§2.1）：`get_skill` 除注入正文外，把技能目录里的 `server.py` 拉起来、
把它的工具加进**本会话**；`drop_skill` 关掉运行体、移除工具。**工具随加载出现、随卸载消失。**

### 13.2 机制：技能 = 按需加入的 server

| 落点 | 职责 |
| --- | --- |
| `mcp.register_server` / `unregister_server` | 加/减一个 server 的工具，维护**工具集版本** |
| `mcp.session_tools` / `session_tool_names` / `registered_servers` / `tools_version` | 取当前工具表 / 名字集合 / 已注册 server / 版本（**同步纯内存**，每轮都被调） |
| `skills.server_module` / `read_skill_env` / `read_preflight` | 按**目录名**拼 import 路径 / 读 `skill.json` 的 env 声明与加载时体检声明（三者都只做**解析**，不碰 settings、不发请求） |
| `tools._skill_preflight_line` | 加载成功后按声明打一次只读端点验凭证（host 侧、`to_thread` + 8 秒超时），结论进 `get_skill` 回执；**不是工具**、失败也不阻断加载 |
| `tools.get_skill` / `drop_skill` | 加载期的四道校验 + 加/减；失败即回滚。**名字已在 `loaded_skills` 里、运行体却不在**时（对账重建失败被退避、或对账本身抛了异常），`get_skill` 会重新拉起并如实说明——退避之后这是本会话**唯一的重试入口**，少了它"正文在、工具没有"就成静默死路（`unknown_tool` 的回执正是指模型来这里） |
| `tools.SessionToolset` | 三节点共用的接线点（取工具 + **双向对账**，快路径靠实例上的**两份备忘**：`registered` 只含能力型、`loaded` 含两型，**直接比永远不相等**，得先记住 `_knowledge_only`（查实过的知识型）与 `_register_failed`（查实过建不起来的）才能回到纯内存比较。后者是 2026-09-13 补的：失败不退避的话，那名字每轮都会重新走一遍注册，而重列 schema 要真起临时会话——坏技能会让会话持续漏子进程，见该类 `_reconcile` 的 docstring） |

**mcp.py 不认识"技能"**：它只知道"某会话多了一个 server"。技能的知识（目录名、`skill.json`、
`tool.json` 的登记口径）全在 skills.py / tools.py 这一侧。

**会话键 = `state["session_id"]`**（不是构图期参量）：两个真实宿主里两者相同（TUI = 会话 id、
evaluation = `task.name`，关池用的也是它）；langgraph dev 零参 → `None` → **禁止注册能力型**
（否则所有 thread 共享一组工具）。这也是 §12"运行体按 (工作区, 会话) 分片"的同一个键。

### 13.3 加载期的四道校验（**失败即拒绝加载**）

能力型在写 `loaded_skills` 之前依次过**这几关**（顺序 = `_register_skill_runtime` 里的实际顺序，
2026-09-14 核对过一次），任一不过 → 回滚 + 可行动回执 + **不写 `loaded_skills`**
（"已加载"必须意味着正文与工具都到位）：

1. **会话**：`state["session_id"]` 非空（`langgraph dev` 的零参入口没有会话 id，能力型在那边一律
   被拒；知识型不受影响）；
2. **目录名**：要求 frontmatter 的 `name` == 目录名（启动模块与 `source` 都按目录名走），且目录名
   是合法 Python identifier **且不是关键字**。**两半都在扫盘期处理**（2026-09-13 改；关键字那半
   2026-09-14 补——`"class".isidentifier()` 为真，可 `import skills.class.server` 是 SyntaxError，
   于是那种技能会被标 `[带工具]` 却永远加载不上，正好破了下面那条不变量）：`scan_skills` 任一不
   满足即**降级为知识型** + 告警，正文仍可用。放在扫盘不是为了省事，是为了让**目录不撒谎**——
   否则 `catalog_block` 标着 `[带工具]`、模型照着去 `get_skill` 却每次都撞拒绝（加载期才判）。
   现在的不变量是：**目录里标 `[带工具]` 的技能一定加载得上**。加载期 `_register_skill_runtime`
   的同名校验保留为安全网（正常路径已到不了那里）。
   另外技能名**必须唯一**：两个目录声明同一个 `name` → 响亮跳过后者（静默覆盖会让目录与
   `get_meta` 指向两个不同目录，其中一个再也取不到正文——§3.6 说的"装了却不可发现"）；
3. **依赖**：`skill.json` 的 `requirements` 声明的顶层包必须在当前环境 import 得到
   （`PathFinder().find_spec` 在**起进程之前**查，见 §13.5）——等子进程起不来再说就晚了；
4. **凭证**：`skill.json` 声明的键必须在 `app/config.py::SKILL_ENV_WHITELIST` 里、取值非空；
   > 这几关是**加载闸门**。另外还有一道**不是闸门**的：`skill.json` 若声明了 `preflight`，加载成功后
   > host 会打一次只读端点验凭证（§13.5 末），结论进回执——**失败不拒绝加载**（见那里的理由）。
5. **工具名**：server 真暴露的每个工具都必须在 `tool.json` 登记、`source == skills/<目录名>`，
   **且不与核心工具 / 编排工具 / 其它已加载技能重名**（同名会让模型与审批策略都无法区分两者）。

第 5 条同时是**未登记工具的防线**：旧行为里"未登记 = 免审"，对写操作等于静默放行。它也是
**整包 fail-closed** 的：只要有一个工具没登记，**整个技能**拒绝加载（不是跳过那一个）——
漏登记与"工具名写错"在效果上都是"技能不可用"，早失败比半可用清楚。

**起不来时要说得出原因（2026-09-13 修）**：`register_server` 失败时，MCP 适配器只把
`ExceptionGroup → McpError: Connection closed` 交给 host——**真正的原因只在子进程的 stderr 上**
（缺依赖、语法错、server 自己抛 `ConfigError`…）。回执若照搬那串判词，模型拿着"把配置问题告知
用户"却说不出是什么问题，§13.3 承诺的"可行动回执"就落空了。所以失败路径会**重跑一次
`python -c "import <server 模块>"`**（用与真启动同一套 command/cwd/env，直接取 `stdio_connection`
的产物以免两处漂移），把 stderr 尾部的 traceback 带进回执——只在**已经失败之后**跑，成功路径零
开销；探针自身说"import 没问题"时回退到**展平后的异常**（import 能过却起不来 = 协议/超时那类，
不该拿无关 traceback 误导模型）。

### 13.4 动态 bind 与限定版 fail-closed

- `LLMNode` 持**未绑定** model + `SessionToolset`，按 `(工作区, 会话, 版本)` 缓存 binding，**只在版本
  变化时**重绑（§3.4 的原意）。`RunnableBinding` 不能再 `bind_tools`，所以"未绑定 model"与"当前
  binding"**必须分字段存**；`toolset is None`（worker / 单测）时**一次都不调** `bind_tools`。
- `ToolNode` 每轮从 toolset 取表——**必须每轮重解析**：同回合 `[drop_skill(A), A_tool()]` 在动态表下
  走 `unknown_tool`（正确）；在固定表下，shim 会在调用时把已卸载的运行体**重新造出来**（"卸载"失效）。
- `ReviewNode` **不再是原稿说的"不变"**：除按 `tool.json` 判 `need_review`，还多一道**限定版 fail-closed**
  ——名字**在当前工具表里**却没登记 → 转审批；名字**压根不在表里**（模型编的）→ 直接回 `unknown_tool`
  回执、**不弹面板**（否则每编一个名字就打断人一次，而它无论如何都会被 ToolNode 拒掉）。

### 13.5 env 声明与转发

- 载体 = `skills/<name>/skill.json`（`{"env": ["GITHUB_TOKEN"], "preflight": {"url": "...", "bearer": "GITHUB_TOKEN"}}`）——**不塞进 SKILL.md 的 frontmatter**
  （那里只认单行 `key: value`，列表要另写解析）。
- 白名单落 `app/config.py::SKILL_ENV_WHITELIST`（env 键 → Settings 属性名）。**绝不通用透传**：技能
  配置若能任意点名 `${VAR}`，就能读走 `CHAT_MODEL_API_KEY`。
- 值必须从 `get_settings()` 取、**不能读 `os.environ`**：`.env` 是 pydantic-settings 自己读的文件，
  其键值不在 `os.environ` 里——用 `os.environ.get` 会对用户写得清清楚楚的键报"未配置"。
- `GITHUB_TOKEN` **带空默认**（`SecretStr("")`），**有意偏离**"所有字段无默认值"那条约定：技能是可
  插拔的，它的凭证缺失不该让核心起不来；空值 = 该技能拒绝加载。
- 键不在白名单 / 值为空 → `skill_env` 抛 `ConfigError`（带键名），由 `get_skill` 翻成回执。
- **依赖声明 `requirements`**（2026-09-13 加）：技能用的**第三方顶层包名**列表（`{"requirements": ["mcp"]}`）。
  加载期在**起进程之前**用 `importlib.machinery.PathFinder().find_spec(name, [工作区, 项目根, *sys.path])`
  查一遍——**不执行模块代码、不起进程**，因为技能依赖装在主 venv、技能 server 与 host 是同一个解释器
  （§5.1 已决不做隔离），所以 host 侧查出来的结论就是子进程 import 时的结论。缺了 → 拒绝加载 +
  回执给"加进 `pyproject.toml` 后 `uv sync`，再重新 `get_skill`"（**不用重启进程**）。
  - 与"失败后的 import 探针"（§13.3 末）**不重叠**：`find_spec` 查不出语法错/模块级异常，探针说不清
    该装哪个包。也**不要**把探针提到注册前——那会让技能模块级代码在成功路径上被跑两遍（副作用重复）。
  - 没写 `requirements` 的技能少这层保护（退化成"报 traceback"），但不会因此加载不了。

#### 13.5.1 加载时体检（`preflight`，2026-09-13 加，**替掉了 `github_auth_status` 工具**）

**问题**：`skill_env` 那关只验"键有没有**值**"，验不出"值**对不对**"——token 过期 / 被撤销 /
权限不够，都要等第一次真请求才暴露。原先 GitHub 技能为此专门做了一个只读工具
`github_auth_status`，让模型自己想起来去调——**那正是坏的发现方式**：配置问题被推给"模型会不会
想到查、查了会不会看懂"，而它并不知道该查。

**做法**：`skill.json` 可声明一个探活端点：

```json
{"env": ["GITHUB_TOKEN"], "preflight": {"url": "https://api.github.com/user", "bearer": "GITHUB_TOKEN"}}
```

`get_skill` **真正加载成功后**用转发进来的那份凭证打一次这个只读端点，结论写进回执
（`凭证体检：GITHUB_TOKEN 可用（HTTP 200）。` / `⚠️ 凭证体检失败：…被拒（HTTP 401…）`）。

四条定下的口径：

1. **在 host 侧、不是工具**——它是"加载那一刻 host 就该知道"的事实，不该占一条模型可见的工具表
   条目，也不该指望模型主动查；
2. **失败不阻断加载**——技能的正文/纪律在拿不到远端时照样有用（照它的规范写 PR 描述不需要网络），
   所以只如实报关 + 让模型把这条告知用户；真去调工具时另有 `upstream_error` 兜底；
3. **`bearer` 必须是该技能 `env` 里声明过的键**——否则等于让技能凭空读一个它没申请的凭据（这条
   在 `read_preflight` 里挡，写错就按"没声明"处理并告警）。声明**只在 `skill.json` 里**，形状不对
   一律降级为"没声明"——体检是锦上添花，不该因为它写错就让技能加载不了；
4. **只报"凭证行不行"**：2xx = 可用 / 401·403 = 被拒 / 网络失败 = 连不上。不解析响应正文——
   账号、配额那些是技能工具自己的事（这正是砍掉那个工具要付的代价：模型不再能主动问"我是谁、
   配额还剩多少"；真需要时由技能工具自己提供，比如 Quartet 那种 `whoami` 风格的读工具）。
   > 这条留个口子：若将来真需要"体检也报出账号/配额"，**不外乎**把响应 JSON 里的少数键写进声明
   > （一个小语言），或者允许技能提供一个 host 侧可调用的探针模块——后者等于**技能代码进主进程**，
   > 是本项目最硬的边界之一（技能代码至今只在子进程里跑），**不做**。
5. 时序上：**只在"真的加载了"那条路径上打**。两条相邻路径**不**打（2026-09-14 核对代码后把口径
   改准，此前这里写成"含重新拉起运行体那条"）：
   - **幂等那条**（已加载、无需重复）保持零成本；
   - **重新拉起运行体那条**（名字在 `loaded_skills` 里、运行体却不在）也不打。理由：那条路修的是
     **运行体不见了**（对账失败退避、进程被回收），与"凭证行不行"是两回事——凭证在首次加载时
     已经报过一次；而且那条回执的用处是"已重新拉起、下次可以调了"，多挂一行体检结论只会让
     修复路径多等一次网络。
   想在会话中途重新体检 = `drop_skill` 再 `get_skill`（回执里就是这么提示的）。

### 13.6 卸载：**只有一条触发路径**（对 §3.4"两条都要"的改口）

**只靠 `drop_skill`**——不做空闲回收（与 §12 决定 4 一致：核心四件套也不回收）。代价说清：**模型忘
了卸，那个技能的进程就一直占着**（靠 §13.7 的目录标记与正文预算压力推动它卸）。`unregister_server`
里"摘 shim → 关运行体 → 版本 +1"是一步动作，§3.3 那个"拆 server 必须同时注销工具"的坑因此**结构性
不存在**。

### 13.7 落点清单（2026-09-12）

| 落点 | 内容 |
| --- | --- |
| `app/schema/agent_schema.py` | `SkillMeta` 增 `dir_name` / `capability` |
| `app/resource/skills.py` | `SKILLS_PACKAGE` / `SERVER_FILENAME` / `SKILL_CONFIG_FILENAME`；能力型判定（含 identifier 校验与降级）；`server_module` / `read_skill_env` / `read_requirements` / `read_preflight` / `get_meta` / `body_of`；`catalog_block` 标 `[带工具]`；`skills_block` 点名"已不可用的技能" |
| `app/config.py` | `GITHUB_TOKEN`（可选带空默认）+ `SKILL_ENV_WHITELIST` + `skill_env` |
| `app/platform/mcp.py` | `stdio_connection`（核心与技能共用）；**独立的 per-server schema 缓存**（见下）；`register_server` / `unregister_server` / `session_tools` / `session_tool_names` / `registered_servers` / `tools_version`；`close_session_pool` 一并清 extras 与版本 |
| `app/agent/tools.py` | `get_skill` / `drop_skill`（加注入 workspace；drop 改 async）+ 四道校验 + `_skill_preflight_line`（加载时体检）+ `_probe_skill_startup`（起不来时捞真 traceback）+ `SessionToolset`（含双向对账）+ `_tool_config()` 校验缝 |
| `app/agent/nodes.py` | `LLMNode` 动态 bind；`ToolNode` 动态查表 + 可行动 `unknown_tool` 文案；`ReviewNode` 限定版 fail-closed |
| `app/agent/graph.py` | 主图装配 `SessionToolset(workspace, static_tools)`、model **不在此绑定**；worker 路径一行未改 |
| `skills/github/` | `server.py`（14 个只读 + 3 个写工具，stdlib HTTP；§13.10）+ `skill.json`（env + preflight）+ `SKILL.md` 同步 |
| `app/agent/tool.json` | 17 条 `skills/github` 登记（14 只读 `need_review: false` + 3 写 `true`；§13.10） |

**最硬的一处坑（写在这里防复发）**：`_TOOL_SPECS` 按**工作区**缓存"核心四件套"的 schema，内容是
"调用方传进来的那批 connections"。若技能列 schema 复用它、只传技能自己的 connection，就会把该工作区
的缓存覆盖成"只剩技能工具"——**之后每个新会话的核心工具都会静默消失**。所以技能走**独立的
`_SERVER_SPECS`（键 = (工作区, server)）**，且两个列 schema 的函数都"完整列完后一次写入"。

### 13.8 测试与验证

- **`tests/test_skill_runtime.py`**（14 例）：加载/卸载、知识型不动工具、四道校验各一例、双向对账
  （重建一次 + 幂等；运行体在而 state 没有 → 注销）、对账失败不炸整轮、动态 bind 只在版本变化时重绑 /
  无 toolset 一次都不绑 / 工具表含静态工具，以及**一条真 spawn 的端到端**（`tests/fixtures/skills_pkg/echo/`：
  真起 `python -m skills_pkg.echo.server` → 真调它的工具 → 卸载后运行体与子进程都被收掉；技能源与包名
  一起指到 fixture，不碰仓库的 `skills/`）。
- **端到端验收（实测通过）**：`load_mcp_tool` → `get_skill("github")` → 15 个 `github_*` 进表、核心 34 个
  **不受影响**、版本 +1；真调一次工具 → 运行体键出现（懒起）；`drop_skill` → 工具与运行体一起消失、
  版本再 +1；**另一会话的核心工具仍是完整 34 个**（锁住上面那颗雷）。
- **`tests/test_github_skill.py`**（11 例，2026-09-13；2026-09-14 扩到 34 例）：技能自带 server 的**收口行为**——render
  抛 `_UpstreamError`（坏 base64）必须翻成 `upstream_error` 而不是被 `guard` 判成内部 bug；上游 HTTP
  失败与成功截断各一例。打磨时又加了 7 例：**限流要按响应头认出来**（`X-RateLimit-Remaining: 0`
  → "限流 + 恢复时刻 + 别重试"；429/secondary → 二级限流；**配额还有剩的 403 不许误报成限流**）、
  `file_read` 对**子模块 / 符号链接**不许再说成"太大"、大文件那条 `download_url` 路径没被碰坏、
  `github_tree` 超 300 条给可行动出路且**缺 path 的脏条目不再 KeyError**。不联网、不起子进程
  （`_request` 换成假响应；测限流那三条打的是 `urlopen`——patch `_request` 会把被测的翻译逻辑绕过）。
- **`tests/test_github_skill_e2e.py`**（1 例，2026-09-13 加）：**真技能**的端到端——用真 `skills/`
  源与真 `tool.json`，真 spawn `python -m skills.github.server`（假 token 经**真 `skill_env`** 转发；
  server 在 **import 期**就要 `GITHUB_TOKEN`，所以"加载成功"本身就是转发通了的证明）→ 断言它暴露的
  工具名与 `tool.json` 的登记**逐字一致**（2026-09-14 起 17 条）→ 真调 `github_repo_view`
  → 上游 200 / 401 / 404 三条路都按 `[upstream_error]` 分类回传 → **写工具真发一次 POST**
  → 卸载后工具与子进程一起没了。
  **不联网**：`GITHUB_API_URL` 指向进程内起的 stdlib HTTP 桩（技能 server 在子进程里，进程内
  monkeypatch `_request` 打不到它）。它补的正是另两块的**中间段**：`test_github_skill.py` 有真实现
  但不起进程，`test_skill_runtime.py` 真 spawn 的是 echo 夹具（无 env、无网络、无凭证）。
  写路径的验法（2026-09-14）：桩的 `do_POST` **自己校验 method / 路径 / 请求体**，对不上回 422——
  于是"POST 真发出去了、body 真是工具拼的那份"成了回执成功与否的一部分，不必往夹具外面递请求日志
  （夹具的返回值是个地址字符串，不为一条断言改它的形状）。
- **`tests/test_github_skill.py` 扩到 27 例（2026-09-14）**：新增 16 例覆盖列表族（**issues 接口
  会把 PR 一起返回**、翻页提示、上游缺字段不许 KeyError）、参数校验（`limit="abc"` / `number="abc"`
  必须是 `invalid_argument` 而不是 internal_error；`owner` 给整条 URL 要当场点破）、tree 的分支名
  `feature/foo` 必须整体转义成一个路径段、**默认分支那一步失败也得是 `upstream_error`**（删掉
  `_UpstreamError.error_type` 时揪出的残留引用就是从这里漏的）、diff 走 `raw` 通道且带 `v3.diff` 的
  Accept、每个文件的 patch 只留开头一段；写工具侧：**`APPROVE` 结构性拒绝且一个请求都不发**、
  `REQUEST_CHANGES` 的 method / 路径 / 请求体、空描述 PR 被拒、回执带 PR 号与 URL、
  `draft="false"` 不许被 `bool()` 读成 True、写操作 422 / 403 的翻译与读操作不同。
- **`tests/test_skills.py`**（2026-09-13 加）：同名技能跳过、能力型因 `name != 目录名` 降级、
  正常能力型不被误伤降级、目录名非 identifier 降级。（2026-09-14 加 3 例：坏编码的 `SKILL.md`
  **只弄坏它自己**——`UnicodeDecodeError` 不是 `OSError` 的子类，漏掉它会让**整次扫描**抛异常、
  所有技能一起消失；`body_of` 对坏编码回 `None`；目录名是 Python **关键字**（`class`）时降级。）
- **`tests/test_skill_runtime.py`** 加两例对账快路径回归（2026-09-13）：知识型在 `loaded_skills` 里时
  逐轮对账**一次盘都不读**；**重建失败只试一次**（server 起不来 / 工具没登记两种失败形态各钉一例，
  并断言告警只响一次、**重开会话会重试**）。后者是审查揪出的真漏洞：失败若不退避，名字会一直留在
  `missing` 里，而回滚已把该 server 的 schema 缓存丢掉 → 每轮（LLMNode + ToolNode 各一次）重列
  schema = **真起两个临时会话**，一个坏技能就让整个会话每轮多起两个子进程。
  同批还加了两例：**运行体缺失时 `get_skill` 会重新拉起**（退避后的唯一重试入口），以及重试仍
  失败时回的是**可行动的原因**（"工具没在 tool.json 登记"）而不是干巴巴的"已在加载中"。
- **起不来时的回执（§13.3 末）**：`tests/test_skill_runtime.py` 加 2 例——**真 spawn**
  `tests/fixtures/skills_pkg/broken/`（那个 fixture 在 import 期 import 一个不存在的模块），断言回执里
  出现**子进程的真 traceback**（模块名逐字在）；以及探针说"import 没问题"时回退到展平后的异常
  （`Connection closed` 看得见，不再是 `1 sub-exception`）。
- **加载时体检（§13.5.1）**：`tests/test_skills.py` 加 5 例（声明解析：合法 / 未声明 / 缺 url 或
  bearer / **bearer 不在本技能声明的 env 里** / skill.json 坏形状）；`tests/test_skill_runtime.py`
  加 3 例（结论进 `get_skill` 回执 / **体检失败不阻断加载**且工具照常注册 / 知识型不体检）；
  `tests/test_github_skill_e2e.py` 把体检端点也指到本地桩并断言回执里有"凭证体检：…可用"
  ——**这条是端到端的**：host 侧真拿转发进来的假 token 打了桩一次。
- 全量 `python -m pytest`：**381 passed / 4 skipped**（2026-09-14；上一期是 360 / 4）。

### 13.9 待定 / 未做（本期边界）

- ~~**GitHub 还没实现的那 9 个工具**~~：**2026-09-14 收口**——只读 5 个与写 3 个都已落地（§13.10），
  只剩 **`github_pr_merge` 明确不做**（理由见 §13.10：工具面上不存在"合并"这个动作）。`SKILL.md`
  里也**不写这个名字**，只写"合并没有对应的工具"——**文档不该列调不通的名字**，否则模型照名字硬调
  只会撞 `unknown_tool`，同"标 `[带工具]` 的技能一定加载得上"那条纪律。
  > 推送更早就**不在这张单子里**：它 2026-09-13 落在核心 git 服务的 `git_push`（理由见 §8.3）。
  依赖方面：本期**仍没引 PyGithub**（读写都只是现成的 REST 端点，stdlib 够用）；真要用时走
  `skill.json` 的 `requirements` 声明（§13.5），依赖仍进主 venv（§9 已决：不做隔离）。
- **技能运行体的空闲回收**：已定不做（§13.6）。
- **`/skills` 控制面命令**：注意它得读 **checkpoint 里的 `loaded_skills`**（注册表只反映本进程、不代表
  状态），照 `app/platform/commands/session.py` 的 saver 读法。
- **`tool.json` 有两份 `lru_cache`**（`tools._tool_config` 管 source 登记、`ReviewNode._tool_config`
  管审批策略，各自的测试缝是有意的）：两份都"读一次用一辈子"，**改 `tool.json`（含给新技能登记
  工具）要重启进程**才生效——不是只重启一处。
- **技能改用"进程内热改"的边界**（2026-09-13 定）：`unregister_server` 会一并丢掉该 (工作区, server)
  的 **schema 缓存**，所以"改 `server.py` → `drop_skill` → 再 `get_skill`"能拿到新代码**与新工具表**
  （此前会新代码配旧 schema：新增/改名的工具看不见）。但**目录条目录入（构图期烤进 docstring）与
  `tool.json` 的 lru_cache 仍是重启级**——真正干净的换法是重开会话 + 重启进程。
- **技能的 `sys.path` 面**：技能 server 的 `sys.path[0]` 是工作区（cwd），工作区里的同名模块可以
  shadow 技能 server 的 import——核心 server 早有这个面，但技能把"可插拔任意代码"引进来，记在案。
- **`GITHUB_API_URL` 目前只有测试用得上**（2026-09-13 加）：host 只转发 `skill.json` 里**声明过**
  的键，而声明它会让它变成"必须非空"——默认（官方 API）那条路径反而加载不上。所以要让用户真能
  指向 GitHub Enterprise，得先给 env 机制加"**可选键 + 默认值**"（§13.5 现在只有"声明了就必须
  非空"这一种）。在此之前这个 env 是**测试缝**：E2E 用它把请求指到本地桩。
- **受保护分支不做工具内拦截**（2026-09-13 定，`git_push` 移进核心服务之后）：`git_push` 是
  **平台无关**的（它只认地址），不认识 GitHub 的 protected branches 配置。**决定：不做。** 理由：
  ① 保护分支**本来就是平台侧的规则**，服务端自己会拒（"Restrict who can push" / 要求走 PR），
  拒绝原因会随 push 的 stderr 回来——在工具里再判一次既重复、又因为拿不到平台配置而判不准；
  ② 要在核心工具里做，就得让它认识"某个平台的某个仓库配了哪些保护规则"，那是把平台知识灌进平台
  无关的组件。剩下的是**软约束（`SKILL.md` §2.1）+ 审批面板**（`url` 与 `branch` 都可见），
  与 terminal 的任意命令同一档（"知情审批"）。
  > 真要有那一天（比如自建 Git 服务上没有任何保护规则、而模型又容易手滑），最小形态是给
  > `git_push` 加一个**平台无关的保守默认**：推 `main`/`master` 直接拒，除非调用方显式传
  > `allow_protected=true`——那个参数会显示在审批面板上，等于"它请求推主干"被人看见。
- **evaluation 与能力型**：带外依赖（token / 网络）会让评估结果不确定；评估任务要用技能得自己想清楚。

### 13.10 三期：平台侧写操作 + 列表族（2026-09-14）

`skills/github/` 从 7 个只读工具长到 **14 只读 + 3 写**，`tool.json` 相应 **17 条**登记（写工具
`need_review: true`）。同批还做了一次 `server.py` 的审计修复（下面单列）。

**新增只读 5 个**（全部免审，走同一条 `_fetch` + `render` 骨架）：

| 工具 | 端点 | 要点 |
| --- | --- | --- |
| `github_issue_list` | `GET /repos/{o}/{r}/issues` | ⚠️ 这个端点**把 PR 也当 issue 返回**——渲染时滤掉并注明"略去了 N 条 PR"，否则模型会把 PR 数当 issue 数 |
| `github_pr_list` | `GET /repos/{o}/{r}/pulls` | "有哪些在等我审" |
| `github_pr_diff` | `GET /repos/{o}/{r}/pulls/{n}` + `Accept: v3.diff` | 唯一走**原文通道**的只读工具（diff 不是 JSON）。这是 `git_diff` 给不了的**区间** diff（§8.3 判据） |
| `github_pr_files` | `GET .../pulls/{n}/files` | 每个文件的 patch 只留开头 40 行——整份 patch 会把回执挤爆（`_clip` 只从后面一刀切） |
| `github_release_list` | `GET /repos/{o}/{r}/releases` | "这库现在什么版本" |

**新增写 3 个**（`need_review: true`，走新的 `_submit` 入口）：

| 工具 | 端点 | 结构性拒绝（§4 第三类） |
| --- | --- | --- |
| `github_pr_create` | `POST /repos/{o}/{r}/pulls` | **`body` 必填**——空描述的 PR 等于把成本转嫁 reviewer（§2.2 #5），这条由工具挡、不靠提示词。回执必须带 PR 号与 `html_url`（人要拿它去点合并） |
| `github_pr_review` | `POST .../pulls/{n}/reviews` | `event` 白名单 = `COMMENT` / `REQUEST_CHANGES`，**`APPROVE` 直接拒**（`_one_of` 实现，样板同 `terminal.py::_deny_sudo`）——批准是人的权力，不问你、也不放行 |
| `github_issue_comment` | `POST .../issues/{n}/comments` | 无（PR 也走这个端点——GitHub 里 PR 就是 issue） |

**为什么"合并"连工具都没有（本期唯一的取舍点）**：`github_pr_merge` 在上一期被列进"待做的写
4 个"，但同一行又写着它的结构性拒绝是"合并权留给人"——**这两句是拧的**：只要工具在，模型就有办法
表达"我要合并"，剩下的只能靠审批闸门兜，而闸门是"问了人可以不批"，不是"压根没有这个动作"。
`github_pr_view` 回的 `可自动合并：true/false` 已经够模型把状况说清、把链接交给用户。所以口径定为：
**工具面上不存在合并**——与"`APPROVE` 被拒"同构，都是"越权的动作不是问了人不批，而是没有"。

**批次里没做的那半边**：两个搜索工具**不加 `page`**（GitHub 的搜索本身就限 30 条、按相关性排序，
翻页价值低），而列表族都加了——这个不对称是有意的。`skills.py` 的两处真 bug 另见 §13.8。

**`server.py` 的审计修复（同批，全部采纳）**：

- `quote(ref)` 默认 `safe='/'`，分支名 `feature/foo` 会把 tree 端点的 URL **拆成两段** →
  `quote(ref, safe="")`（`path` 那处保留 `/` 是对的，它本来就是多段路径）；
- `int(limit or 10)` / `int(number or 0)` 遇非数字字符串抛**裸 `ValueError`**，被 guard 判成
  `internal_error`（回执是"无需重试"）→ 抽 `_as_int` / `_positive_int` 抛 `InvalidArgumentError`；
  布尔同理不能用 `bool(value)`（`bool("false")` 是 **True**，草稿开关会被悄悄打开）；
- 渲染里的下标访问（`item['full_name']`、`t['name']`）上游缺字段就 `KeyError` → 一律 `.get()`；
- `download_url` 可能**缺席**（旧写法把 `None` 打进正文，模型会照着去取一个 "None"）；
- `JSONDecodeError` 分支补上 HTTP 状态码与"响应体为空"的判定；
- `_UpstreamError.error_type` 是**死参**（无调用点），删掉；
- **`owner` / `repo` 加 `_require_slug`**：只判非空时，把整条 URL 当 owner 传不会当场报错，而是拼出
  一条坏 URL 去撞 404——模型拿到"没有这个东西"，学不到任何东西；
- 写操作的 **422 / 403** 各自翻译（"参数或目标状态不允许" / "分支受保护或没有推送权限"）——与读
  操作的下一步动作不同，不能混成一句"失败了"。

**同日补遗（评审闭环收口）**：只读再加 2 个——`github_issue_comments`（`GET /issues/{n}/comments`，
issue / PR 通吃的**对话楼层**，带楼层号与翻页）与 `github_pr_reviews`（`GET /pulls/{n}/reviews`，
**评审结论**：谁、状态翻成人话、正文；无总评的轮次注明"意见可能写在具体代码行上"）。评审工作流
此前是断的：模型能 `github_pr_review` 写意见、却读不回别人的意见。同批 `github_file_read` 补
二进制回执：base64 解出后 NUL 嗅探 + 魔数猜类型（与本地 `read_file` 同日修复同款、口径一致）；
魔数表在技能内复制一份——技能子进程没有 `WORKSPACE_PATH`，import `file_io` 即炸。至此
**14 只读 + 3 写**，`tool.json` 17 条。`github_issue_create` / 关 issue **刻意不做**（2026-09-14
用户拍板：issue/PR 侧保持只读基调，开 PR 与评审的写权不外溢）。

| 模块 | 关系 |
| --- | --- |
| `mcp_service/`（file_io / terminal / git / web_search） | **内置核心能力**，skill 与它并列而不混入（§8.1）；`terminal.py::_deny_sudo` 是"结构性拒绝"的样板；`git.py` 的 `git_push` / `git_clone` 都收远程仓库**地址**（§8.3），核心服务因此**不持有任何平台凭证** |
| `skills/<name>/`（顶层库） | 一个技能一个目录：`SKILL.md` + 可选 `server.py`（能力型）+ 可选 `skill.json`（env 声明 + 加载时体检 `preflight`）。首个 = `skills/github/`（2026-09-12 起带 server.py；2026-09-14 起 **14 只读 + 3 写**工具 + 加载时体检，见 §13.10） |
| `mcp_service/sub_agent.py` + `app/agent/graph.py::get_sub_agent_graph` + `app/agent/tools.py::worker_tools` | **fork 执行的现成骨架**（独立上下文 / 工具子集 / 跨进程审批回流）——§6 要复用它 |
| `app/platform/mcp.py` | **§12 已落地**：现在同时承载连接配置、工具加载（**不绑会话的 shim**）与运行体常驻（`_ServerWorker` + `get_worker` + `close_session_pool`）。`load_mcp_tool(workspace, session_id)` 增加会话参量；`get_resources` 管道一期不做（§9 已决）——软约束由 host 读 `SKILL.md` |
| `mcp_service/terminal.py` | **§12 顺带修好了它的跨调用进程管理**（改造前 `start_process` 起的进程下次调用就认不到，实测）——池化后自动恢复，**本文件一行没改** |
| `app/platform/runtime.py::build_session_runtime` | **§13.2 的注册表宿主**：与 graph / checkpointer 一起造的会话级运行时对象，构图期注入 LLMNode / ToolNode / OrchestrateNode |
| `app/agent/tool.json` | 审批策略的**唯一权威表**，也是硬约束的落点之一。`get_skill`/`drop_skill` 走 `source: skill`（§11）；技能带来的工具走 `source: skills/<目录名>`（§13）。**未登记的工具，`get_skill` 会拒绝加载整个技能**（§13.3） |
| `app/agent/nodes.py` | `ORCHESTRATE_SOURCES` 含 `skill`（§11）；二期又动了三处：`LLMNode` 动态 bind、`ToolNode` 动态查表、`ReviewNode` 限定版 fail-closed（§13.4） |
| `app/resource/memory.py` | 知识型的同族通道（"工作区内容 → 注入"），新模块 `skills.py` 与它并列 |
| ~~`app/agent/profile.py`~~ | **已于 2026-09-12 并入 `LLMNode.agent_md_block`**——生产侧只有 LLMNode 一个消费者，按上面那条判据不单独立模块。`skills.py` 不受此影响（它是 memory.py 的处境，不是 profile.py 的） |
| `app/platform/commands/prompts.py` | `/init`、`/fast readme` 载荷——§10 待收敛 |
| `docs/MULTI_AGENT.md` §6 | "子 agent 写权限下放"是 §6 的前置里程碑 |
| `docs/TODO.md`「反思节点」 | 与本文 §6 打通：fork 本身就是反思载体 |

### 13.11 修订：审批声明随技能走（2026-09-19）

技能自带工具的 `need_review` 从中心表 `tool.json` **搬到技能目录自己的 `skill.json`**（新键
`tools`：`{"<tool_name>": {"need_review": bool}}`）。动机：约束三分法的收尾——软约束在
SKILL.md、能力声明与硬闸门在 skill.json（env / requirements / preflight 早就在那儿），
**加技能 = 落一个目录**，不再需要动中心文件；`tool.json` 回归"内置 MCP 工具 + 编排工具 +
worker 下放规则"这些 host 结构性决定。

- **fail-closed 强度不变，且从单向变三向**（`tools.py::_check_skill_tools`，加载期）：
  server 暴露的工具没声明 → 拒载（原契约）；声明形状不对（`need_review` 非布尔）→ 拒载；
  声明了 server 没暴露的工具 → 拒载（拼错名字的静默形态——旧中心表结构上查不出这一向）。
- **ReviewNode 两级查询**（`_tool_cfg`）：内置表优先，其次已加载技能的声明；"在工具表里却
  两头无政策"仍判 review（§13.4 的第二道防线原样保留）。声明**读盘不缓存**——改 skill.json
  重新 get_skill 即可，不必重启进程（比旧 tool.json 的"改完要重启"还少一步）。
- **信任条款**：自报政策可信的前提是**技能源 first-party（同仓库）**。若将来开放外部技能源，
  外部技能的政策声明 host 不认、一律按 `need_review: true` 兜底——这条写在 §2.2 的"不得自己
  造审批"旁边，是同一纪律的另一面。
- worker 规则（`worker_allow`）、编排工具的 `source` 分流、内置工具政策**留 tool.json**——
  那些是 host 的结构性决定，不随技能走。技能工具本来就被 worker 结构性排除，不受影响。
- 迁移：`skills/github/skill.json` 声明全部 17 条（14 免审 + 3 写），`tool.json` 相应删去
  17 行；`tests/fixtures/skills_pkg/echo/skill.json` 随 fixture 落一份。
