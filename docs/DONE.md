# Theta $\theta$ 已完成条目归档

> 这里放 `docs/TODO.md` 里**已勾掉**的条目——原文照搬（动机 / 决策 / 落地记录 / 验证都在），
> 检索与追溯照旧。**新条目只进 `docs/TODO.md`**；未完成的部分（标 `[~]` 的）也留在那里。
> 顺序：按条目里的完成日期倒序。


## [x] 节点过度实现：剥离"资源访问的实现"，保留"这一拍做什么"（2026-09-21）

**判据**（承下方「`app/agent/` 的判据（A/B/C）」与「工具面四问」两条——**这是同一条区分在节点层的
翻版**）：

> **节点 = 图里的一拍**。它的职责是"从 state 取输入 → 做这一拍该做的事 → 写回 state"。
> 它**调用**别的模块是正当的；它**实现**"某个具体资源怎么访问 / 怎么转换"就是**过度实现**。

| 层 | 定义侧（留下） | 实现侧（出去） |
| --- | --- | --- |
| 工具面（四问） | `app/agent/tools.py`：Agent **可以做什么** | `mcp_service/`：**怎么执行** |
| **节点面（本条）** | 节点：**这一拍做什么**（拼 messages / 调 / 写回） | 被它调用的模块：**资源怎么读、怎么转** |

**实例（用户原话）**：`LLMNode` 最基本的职责是**拼接 messages、发送给厂商、拿回模型回复**——
不需要实现图片 base64 转码，也不需要实现"确保图片路径在不在"。**而"格式化"可以保留**：把 state 里
的东西渲染成消息是"这一拍做什么"，不是资源访问。

### 审计（2026-09-21 实测）

| 节点 | 行 | 过度实现 | 处置 |
| --- | --- | --- | --- |
| **LLMNode** | 336 | 图片通道 **139** · **画像读取 31** | 都出去（见一） |
| **ReviewNode** | 325 | 免审策略与判定 **~95** · 提问参数校验 **~60** | 都归 `policy.py`（见二） |
| **CompactNode** | 230 | **9 个私有辅助共 ~110 行** | 分两步（见三） |
| OrchestrateNode | 159 | `_invoke` 的签名代填 ~30（注入机制） | 先记，等 dispatch 搬完再估（见四） |
| ToolNode | 188 | — | **干净** |
| QueueNode | 36 | — | **干净** |

### 一、`LLMNode`：同一个病有两处，只有一处被提过

`__call__` 里四条注入通道，**三条是"调用"，只有画像是"自己实现"**：

```python
profile    = await self._read_profile()                                # ← 自己实现：读盘 + 截断 + 实例 memo
block      = await asyncio.to_thread(memory_block, self.workspace_path)    # ← 只调用
pointer    = await asyncio.to_thread(handoff_pointer_block, ...)           # ← 只调用
skill_text = await asyncio.to_thread(skills_block, loaded_skills)          # ← 只调用
```

画像那 31 行（`agent_md_block` / `_read_profile` / `AGENT_MD_INJECT_CAP` / `_PROFILE_BLOCK_HEADER`）
是 2026-09-12 从 `profile.py` **并进来**的，理由是"生产侧只有本节点一个消费者"。**那个理由现在站不
住了**——`memory_block` 同样只被 `LLMNode` 调用，却住在自己的模块里。同一类东西，两种待遇。

而且那个**实例 memo 是个真 bug 面**：`_read_profile` 的 docstring 自己记着"零参入口（langgraph dev）
一图多 thread、同一实例跨会话复用，那条路径下不重开进程就不会刷新"。

**该保留的**：`format_plan_status`（22 行，把 `state.current_plan` 渲染成文本）与 `_model_for`
（17 行，按工具集版本决定要不要重新 `bind_tools`）——两者都是"这一拍做什么"。

### 二、`ReviewNode`：两处都归 `policy.py`

- **免审策略与判定**（`_COMMAND_POLICY_PATH` / `_command_policy` / `_SHELL_METACHARS` /
  `_free_shell_verdict`，~95 行）——已在「`app/agent/` 的判据」条归 `app/agent/policy.py`。
- **提问参数校验**（`_ask_request` / `_normalize_ask_answer` / `_MAX_ASK_OPTIONS` /
  `_ASK_SELECT_MODES`，~60 行）——按 C 它是 **`ask_user` 这个 action 的参数校验**，属于 action 的
  定义侧（`tools.py`）。**但**它必须在**弹面板之前**跑（"空问题 / 超限选项摆到人面前是最糟的形态"），
  搬去 `tools.py` 会造一条 `nodes → tools` 的边——而**现在 `nodes.py` 刻意不 import `tools.py`**
  （它拿的是构图期传进来的工具对象）。

  **2026-09-21 拍板：也放 `policy.py`**，不搬去 `tools.py`、不造那条边。理由：它是"闸门挂起前必须
  成立的判定材料"，与"这条命令要不要免审"同性质。

### 三、`CompactNode`：230 行里 ~110 行是 9 个私有辅助，分两步

**① 纯重复（先收，低风险、立竿见影）**——与职责无关：

- `CompactNode._short(text, 60)` 与**模块级** `_short(text, 120)` 同名不同值；
- 且与 `CompactNode._first_line(text, 80)` **逐字相同**；
- 加上 `app/platform/commands/session.py::_short_line`，这是**四处逐字重复**（第一轮扫描已列出，
  同族还有 `panels.py::truncate` / `skills/github/server.py::_clip`——后两者的**后缀语义刻意不同**，
  是人看的 vs 教模型补救的，**别顺手合**）。

**② 跨节点协议解析 / 归档策略（归到"约定落点"那条线一起做，别单独动）**：

- `_tm_status`——docstring 自己写着「优先读 `additional_kwargs` 的结构化戳（**ToolNode/ReviewNode
  生产端已挂**）」：**它知道另外两个节点的内部约定**；
- `_notes_kind`——决定"哪些工具的结果进 notes"，那是**归档策略**，不是折叠。

### 四、`OrchestrateNode`：先记账

`_invoke` 的签名代填（~30 行：按 `InjectedState` / `InjectedToolCallId` / `InjectedWorkspace`
注入）是**注入机制**的实现。但它现在服务 5 个工具（memory×2 / `get`·`drop_skill` / dispatch）；
**等 dispatch 搬去 MCP**（见下方那条）之后只剩 4 个，那时再估这 30 行值不值，别现在动。

### 五、反复出现的病：跨模块的"数据形状约定"**没有单一落点**

审计过程中三次撞见同一个形状——约定在两侧各写一份，靠注释互相指认，**没有一方是权威**：

| 约定 | 生产侧 | 消费侧 | 单点在哪 |
| --- | --- | --- | --- |
| MCP 跨进程序列化 | `mcp_service/utils.py::guard` | `app/platform/tool_results.py::format_tool_result` | **没有**（两侧注释互引 + 一条 `tests/test_mcp_pool.py` 用例代管） |
| `ToolMessage` 的 `error_type` 戳 | `ToolNode` / `ReviewNode` | `CompactNode._tm_status` | **没有**（docstring 里点名"生产端已挂"） |
| 编排工具的返回切片 | `app/agent/tools.py` 各工具 | `OrchestrateNode.__call__` | **没有**（两侧 docstring 各写一遍形状） |

**这和「`app/agent/` 没有入目录判据」是同一个病的两个面**——都是"**没有单一落点**"。工具面的
`ToolResult` 被否掉不做统一（见 `docs/EXCEPTION_DESIGN.md` §5），但那不等于这三处约定可以继续
无主；**该做的是给每处约定一个显式落点**（形如"生产端提供构造器 / 消费端只读它"），而不是让两侧
注释继续互相指认。

**未决**：第五条的落点形态（一个 `app/agent/contracts.py`？还是各自归入生产端模块？）；
`app/agent/nodes.py` 剥完 LLMNode 图片与画像后是否还需再分（当前 1357 行 → 预计 ~1160）。

**相关背景**：`docs/ARCHITECTURE.md` §4（工具面四问）、本文件「`app/agent/` 的判据（A/B/C）」
（目录层同一条区分）、`docs/CONTEXT_ENGINEERING.md`（`compact_node` 的折叠语义，改 CompactNode 前
先读）。代码落点：`app/agent/nodes.py`（六个节点 + 模块级 helper）、`app/resource/`（图片与画像的
新家，见另一条）。

### 落地（2026-09-21 当日做完）

- **`LLMNode` 336 → 约 160 行**：图片通道 139 行 → `app/resource/images.py`（`image_tokens` /
  `resolve_image_path` / `attach_images_to_payload`，全是无状态函数）；画像 31 行 →
  `app/resource/profile.py::agent_md_block`。**留在节点的**：`format_plan_status`（渲染 state）、
  `_model_for`（工具集版本决定 bind），以及 `_read_profile` 的**实例内 memo**——"读一次还是每轮读"
  是"这一拍做什么"，读盘/截断才是资源访问（这条线写进了 `_read_profile` 的 docstring）。
- **`model.py` 并入**（原计划见下文"二"）：`LLMNode.thinking_extra_body()` / `LLMNode.main_chat_model()`
  两个静态方法；`graph.py` 调它当默认值，**`model` 参量照旧可注入**（层 A 回放与
  `test_skill_runtime` 的 monkeypatch 都还在）。**`.env` 时机刻意不变**：`nodes.py` 顶部保留
  `settings = get_settings()` 一行（懒加载是 `docs/EXCEPTION_DESIGN.md` 里另一条未落地待办）。
- **`ReviewNode` 剥出 → `app/agent/gates.py`**（**命名按"更广的语义"重估**：装的是"闸门挂起前必须
  成立的判定材料"，`policy` 只覆盖前三样，故取 `gates`）：`tool_config()` / `command_policy()` /
  `SHELL_METACHARS` / `free_shell_verdict()` / `ask_request()` / `normalize_ask_answer()`。
  **顺带清掉两处真冗余**：① `tools.py::_tool_config()` 是**死代码**（生产零消费者，只有它自己的
  docstring 说"给测试留缝"）——直接删；② `worker_tools()` 里那份**不缓存的**读表改走
  `gates.tool_config()`。于是"同一份 tool.json、两个缓存、一个不缓存"变成一份缓存一个入口。
  `tool.json` / `command_policy.json` **仍在原地**（表与解析器必须同住，理由写在 `gates.py` 顶部）。
- **`CompactNode` 去重（第①步）**：`_short(text, 60)` 与 `_first_line(text, 80)` 逐字相同 → 合成
  一个 `_first_line(text, limit)`（`limit` 由调用点显式给）；模块级 `_short(text, 120)` 是**另一种
  语义**（把所有空白折叠成一行，给控制台日志用），**改名 `_one_line`** 并在两边 docstring 写明
  "刻意不合并"。`app/platform/commands/session.py::_short_line` 按拍板**保留**（理由见下"未决 1"）。
- **第②步（跨节点协议解析 / 归档策略）只做了一半**：`_tm_status` 改读**生产端常量**（见下"第五条
  的落地"）；`_notes_kind` / `ARCHIVE_TOOLS`（归档策略）**未动**——它不是"约定无主"，而是"折叠与
  归档同住一个节点"这件更大的事，动它得连 `compact_node` 一起设计，不在本轮。
- **`OrchestrateNode._invoke` 的注入代填：按原计划只记账不动**——dispatch 搬走后它仍服务
  memory×2 / `get_skill` / `drop_skill` 四个工具，30 行值不值留到那时再估。

### 第五条的落地：三处约定各给一个落点（拍板"归生产端，消费端只读"）

| 约定 | 落点与方向 |
| --- | --- |
| `ToolMessage` 的结构化戳 | **生产端单点** = `nodes.py` 的 `_tool_stamp()` + `STAMP_ERROR_TYPE` / `STAMP_DECISION` / `STAMP_DENIED`（ToolNode / ReviewNode 是全仓仅有的两个写出点）；CompactNode 只读常量 |
| 编排工具返回切片 | **执行器为权威** = `OrchestrateNode` + `_EXTRA_SLICE_KEYS` 上方那段说明（messages 必给 / current_plan 可选且链式 / 其余切片要登记）；`tools.py` 只描述"本组给哪些键"并指向它。**权威落在消费端是无奈的**：`nodes.py` 刻意不 import `tools.py`，放生产端就得造一条反向 import |
| guard ↔ format_tool_result 的跨进程序列化 | 随 `mcp.py` / `utils.py` 一起搬进 `app/platform/` 后**成了邻居**；生产端 = `mcp_service/utils.py::guard`，消费端 `tool_results.py` 只读它 |

**顺带修掉一个从来匹配不上的回退项**：`_FAIL_PREFIXES` 里的 `"[config_error]"` 从未命中——
`_type_token(ConfigError)` 去掉 "Error" 后缀得到的是 `config`。已按生产端的真实取值改正。

**未决（仍开放）**：`app/agent/nodes.py` 剥完图片与画像后是否还需再分（现 1101 行）。

**2026-09-22 量过一轮，结论是"先不动"，但下次要动请按下面这两刀切**（用户提的是"拆成
`nodes/` 包 + `tools/` 包"，评估后否掉）：

- **拆包的代价是具体的，不是理论**：测试里有 **5 处 `monkeypatch.setattr(nodes_module, …)`**
  （`interrupt` / `memory_block` / `skills_block` / `settings`）。变成包、靠 `__init__` re-export 的话，
  补丁**打在包对象上、读全局的是子模块** → **静默失效**（测试照绿、行为没被钉住）——与
  `app/resource/paths.py` 的 `RESOURCE_ROOT` 同一个坑（那条已在 docstring 里留痕）。另有 22 处
  `app.agent.nodes.X` 引用点要跟着改。收益只有导航性：**包边界不是判据**，判据问的是"它是不是一类
  独立的东西"（机制 vs 定义、引擎 vs 一拍）。
- **该切的两刀**（按同一把尺）：
  1. **`app/agent/compact.py`**——折叠**引擎**（扫块 / 判状态 / 渲染摘要行 / 归档策略，纯函数）从
     `CompactNode` 剥出，节点只剩"读 state → 调引擎 → 写 state"。**顺带就是上文 CompactNode 第②步**
     （`_tm_status` 的跨节点协议 + `_notes_kind` 归档策略）。`nodes.py` 1101 → 约 860。
  2. **`app/agent/skill_runtime.py`**——`tools.py` 里那 **466 行技能加载机制**（起 server / 三向校验 /
     体检 / 依赖预检 / import 探针 / `_register_skill_runtime`）加 `SessionToolset`(124)。它与
     `get_skill`/`drop_skill` 的**工具定义**是两种东西，性质同 `app/platform/mcp.py`（"起进程"的
     基础设施）——正是本条"记账 2"点名的那一族。`tools.py` 1165 → 约 700。
  两刀切完都落回本项目其它模块的量级（`mcp.py` 669 / `skills.py` 429）；在那之后再谈要不要拆包，
  且那时必须"各模块 own 自己的全局、测试打具体子模块"。

## [x] `app/agent/` 的判据（A/B/C）与资源层拆分（2026-09-21）

**动机**：承下方「`dispatch_subtasks` 该跟 worker 同侧走 MCP」一条——那条的起点是"`app/agent/` 是
唯一没有入目录判据的目录"。**现在判据有了**，本条是它的落地清单：按判据一过，`app/agent/` 里有
两个模块要出去、三处片段要记账、一个模块要合并进来。

### 判据（三条问，命中任一即属于 `app/agent/`；三条都不中则不属于）

| | 问什么 | 它回答 | 例 |
| --- | --- | --- | --- |
| **A** | 是否参与 Agent 的**决策上下文**？ | "**Agent 现在知道什么**" | 当前对话 / plan / notes / memory / loaded skills / 工具状态 / workspace 状态的语义表示 |
| **B** | 是否定义 Agent 的**决策过程**？ | "**Agent 如何从当前状态走向下一步**" | `LLM → tool call → queue → review → orchestrate/tool → LLM`；Graph / Node / AgentState |
| **C** | 是否定义 Agent **可以采取什么行动**？ | Agent action —— **不是** action implementation | plan / load_skill / write_memory / dispatch_subtask |

**C 的关键区分**（这条最容易被做错）：

- `app/agent/tools.py` 描述"Agent 可以做什么**决策性动作**"（*"我要写这个文件"*）→ **属于 agent**；
- `mcp_service/file_io.py` 负责"这个动作**实际怎么执行**"（真正写盘）→ **不属于 agent**。

**一条反向限制**：**代码只因为 Agent 恰好调用了某个外部边界，并不因此属于 Agent。**

### 审计结果（2026-09-21 实测）

| 模块 | 行 | A | B | C | 判定 |
| --- | --- | --- | --- | --- | --- |
| `graph.py` / `nodes.py` / `state.py` / `__init__.py` | 245 / 1357 / 54 / 7 | | ✓ | | 留 |
| `tools.py` | 1313 | | | ✓ | 留（Agent action 的定义） |
| `prompt.py` | 348 | ✓ | | | 留（含一处越界，见"记账"3） |
| `memory.py` | 294 | ✓ | | | **搬**（见一） |
| `skills.py` | 428 | ✓ | | | **搬**（见一） |
| `model.py` | 72 | | △ | | **并入**（见二） |
| **`mcp.py`** | **638** | **✗** | **✗** | **✗** | **出**（见四） |
| **`utils.py`** | **111** | | | △ | **出**（见四） |

### 一、`app/resource/` 资源包（认知 / 能力 / 输入）

`app/resource/paths.py`（94 行，落盘路径单点）**升格成包**——不另起 `app/memory/` + `app/skills/`
（**`app/skills/` 会与顶层 `skills/`（技能库本体）重名**，而 `skills.py` 里还有
`SKILLS_PACKAGE = "skills"` 指向那个包，读起来极绕）：

```
app/resource/
  paths.py    ← 现在的 app/resource/paths.py（workspace_key / workspace_dir / session_db_path /
                memory_root / sessions_dir / iter_session_dbs / remove_legacy_single_db）
  memory.py   ← 认知资源（294 行）
  skills.py   ← 能力资源（428 行）
  images.py   ← 输入资源：@路径 → data URI 的调用期转码（139 行，从 LLMNode 剥出）
```

判据一致——**读 agent 之外的东西、转成内部表示**：落盘路径 / 记忆文件 / 技能目录 / 图片文件。

- **`images.py`**（2026-09-21 认定）：139 行全是 `@staticmethod`，测试从头到尾不构造 `LLMNode`
  （`tests/test_image_attach.py` 直接调静态方法）；输入是"HumanMessage 文本 + workspace"、输出是
  "请求体副本 + 记账"，**完全不碰 state**。它还有一条明确的资源访问政策：**不做沙箱拒绝**（`@` 是
  用户手输的通道，沙箱约束的是模型的工具调用、不约束用户自己）。它占 `LLMNode` 41%。
- **memory / skills 暂时各自保持单文件**（2026-09-21 拍板）：我另发现"技能内部有缝"（解析渲染
  vs 加载机制），**先不切**。
- **红利**：`app/platform/runtime.py:68` 现在是函数内**懒加载**（`# noqa: PLC0415`），为的是避开
  `app.agent.*` import 即需 `.env`；`memory.py` 只依赖 `app.resource` + `app.schema`，**搬进资源层后
  没有 `.env` 依赖，懒加载可以去掉**（`app/resource/paths.py` 的 docstring 正是这么描述自己的定位的）。
- **搬家成本低**：两者都是"叶子"（`memory.py` → `app.resource` + `app.schema`；`skills.py` → **只有
  stdlib + `app.schema`**），无循环依赖要解。真正的成本在测试：**9 个文件**改 import
  （`test_memory` / `test_memory_injection` / `test_memory_tools` / `test_skills` / `test_skill_runtime`
  / `test_skill_tools` / `test_handoff` / `test_github_skill_e2e` / `test_steering`）。
- **`memory.py` 还有一个后续项**：长期记忆分层（见下方那条）实施时它多半还要**再出去一次**——
  ① ② 层的路径不在 `resource/<ws_key>/` 下，`memory_block(workspace)` 的签名会失效，`mN` 编号与
  `MEMORY_INJECT_CAP` 的"单份"假设也都不成立。那时它从"单文件读写单点"变成"多层记忆的路径 +
  解析 + 预算分配 + 播种"——**按四问第三问，那是 Resource abstraction 方向**。

### 二、`model.py` 合并进 `LLMNode`

`model.py`（72 行）= `get_main_chat_model()` + `thinking_extra_body()`。合并形态：
**成为 `LLMNode` 的 `@staticmethod`**（`graph.py` 调它当默认值）。

- ⚠️ **硬约束：`model` 参量必须保留可注入**。`get_main_agent_graph(model=None)` /
  `get_sub_agent_graph(model=None)` 的参量不能收掉——`evaluation` 的层 A 回放靠
  `build_eval_graph(..., model=replay_model)` 注入 `ReplayChatModel`，`tests/test_skill_runtime.py:870`
  也 monkeypatch 它。所以是"**默认构造跟着 LLMNode 走**"，**不是**让 `__init__` 自己造模型。
- **先例**：`profile.py` 2026-09-12 并入时就是这么做的——"**留作静态方法而非内联**——测试仍可直接
  调用它，不必构造 LLMNode 实例"。`model.py` 的测试（`test_thinking_config.py` /
  `test_vision_input.py` / `test_image_attach.py`，都写 `import app.agent.model as model_module`）
  正好吃这一套。
- **连带改**：`app/config.py:23` 的注释引用了 `app/agent/nodes.py::LLMNode::thinking_extra_body`。

### 三、`app/agent/policy.py`：权限读取**单点**（表不动）

**`tool.json` 与 `command_policy.json` 保留在原地**（2026-09-21 拍板，理由是纪律不是偏好）：
CLAUDE.md 明写"**元字符刻意留在代码里**（`_SHELL_METACHARS`）：那是解析器的**词法定义**不是策略，
进数据文件等于让人能在不改代码的情况下削弱解析器"。把表搬进资源包、解析器留在 `ReviewNode`，会
制造"只改表就能改行为"的误读——而第①步（元字符串）恰恰改不动。`tool.json` 同理：四个键
（`need_review` / `source` / `worker_allow` / `free_when`）的语义全在代码分支里。
按 A/B/C，`free_shell_verdict` 是 **B**（闸门的决策过程），表是它的输入，**不可分**。

**但实测有真重复要收**——`tool.json` 被**三处各读一遍**：

| 读者 | 缓存 |
| --- | --- |
| `app/agent/nodes.py:644` `ReviewNode._tool_config()` | lru_cache |
| `app/agent/tools.py:714` `_tool_config()` | lru_cache |
| `app/agent/tools.py:89` `worker_tools()` 里的 `_WORKER_TOOL_CONFIG` | **每次读盘，无缓存** |

**同一份文件、两个缓存、一个不缓存。** 落点 `app/agent/policy.py`，装：`tool_config()` /
`command_policy()` / `free_shell_verdict()`（从 `ReviewNode` 搬出）+ `_SHELL_METACHARS`。
这样"策略表 + 解析器仍住在一起"（纪律不破），`nodes.py` 掉约 90 行、`tools.py` 去掉重复读取。
**"权限有单一入口"的目的能达到，代价是它留在 `app/agent/` 内（它是 B 的配置层）。**

**还要装一块**（2026-09-21 与「节点过度实现」条一并拍板）：`_ask_request` /
`_normalize_ask_answer` / `_MAX_ASK_OPTIONS` / `_ASK_SELECT_MODES`（提问闸门的**输入校验**，~60 行，
从 `ReviewNode` 搬出）。它按 C 是 `ask_user` 这个 action 的参数校验、本该归 `tools.py`，但**必须在
弹面板之前跑**，搬去 `tools.py` 会造一条 `nodes → tools` 的边（现在 `nodes.py` 刻意不 import
`tools.py`）——所以归这里。

⚠️ **命名要重估**：该模块最终装的是"**闸门挂起前必须成立的判定材料**"（策略表 + 免审判定 +
提问校验），`policy` 只覆盖了前三样。动手时按这个更广的语义取名（`gates.py` 之类），别让名字窄于
内容——这条正是本项目反复吃亏的地方。

### 四、判出 `app/agent/` 的两个（独立于上面三件）

- **`mcp.py`（638 行）**：A/B/C **三个都不答**——它是运行体机制（拉起子进程 / 常驻会话 / owner task
  / schema 缓存 / 关闭），不是"Agent 知道什么"、不在图里、也不是"Agent 能做什么"。独立证据早就
  摆在眼前：消费者是 `app/platform/loop.py`（`close_all_pools` / `close_session_pool`）、
  `evaluation/runner.py`、`mcp_service/sub_agent.py`——**基座管着它的关闭**；`graph.py` 只用它的
  `load_mcp_tool` 两次。
- **`utils.py`（111 行）**：按上面那条**反向限制**，`format_tool_result` / `coerce_tool_result` 的
  全部存在理由就是"MCP 跨进程返回的形状需要解开"（content-block → `json.loads` → 还原
  `ToolResult`）——它服务的是**边界**，不是 Agent 语义。住在 `app/agent/` 只因为两个调用方
  （`ToolNode`、dispatch 的 worker spawn）碰巧都在 agent 侧。
- **两者是同一件事的两半**（主进程侧的 MCP 边界：一个管运行体、一个管返回形状），**建议放一起**。
  落点三选一，**未定**：跟 `mcp_service/` 做邻居 / 跟 `app/resource/paths.py` 一样做 `app/` 顶层单文件 /
  `app/platform/`（因为基座管关闭）。另外 `utils.py` 若要独立，它和服务端 `guard` 之间那条
  **跨进程序列化契约**目前**没有单一落点**（只靠两侧注释 + 一条 `test_mcp_pool.py` 的用例代管，
  见 `docs/EXCEPTION_DESIGN.md` §5）。

### 记账（先不切，各自独立成立）

1. **`memory.py::ensure_memory_template`** —— 建会话播种，消费者是 `app/platform/runtime.py:68`。
   性质是**资源生命周期**，不是 A/B/C。它只是恰好和读写语义住在同一个文件里。
2. **`skills.py` 的加载机制那半**（`read_skill_env` / `read_requirements` / `read_preflight` /
   `server_module`）—— 实测消费者**只有 `tools.py` 的加载组**（`_register_skill_runtime` /
   `_missing_requirements` / `_skill_preflight_line` / `_probe_skill_startup`）。按四问的口径，这一族
   和 `mcp.py` 同性质（"起进程"的基础设施）。**2026-09-21 拍板暂不切。**
3. **`prompt.py::handoff_pointer_block` + `HANDOFF_MD_FILENAME`** —— project-handoff **技能**的指针
   注入，住进了提示词模块。

### 判据已落纸（2026-09-21）

A/B/C 已写进 `docs/ARCHITECTURE.md` §4，**紧挨工具面四问**（配对关系：一个判"是 MCP 还是编排"、
一个判"进不进 `app/agent/`"，三条都不中才轮到四问决定去处）。文档里带上了三个要点：**配对关系**、
**C 的同构性**（工具面 / 节点面是同一条区分）、**可操作的排除判法**（"外部边界不存在还需要吗"）。
本条的审计表留在这里作为**实例证据**——它是"为什么 `mcp.py` / `utils.py` 该出去"的原始记录，
文档里只放判据、不放大表。

**相关背景**：`docs/ARCHITECTURE.md` §4（工具面四问——先过那一关，再过本条的 A/B/C）、本文件
「`dispatch_subtasks` 该跟 worker 同侧走 MCP」条（四问的第一次应用）、`docs/LONG_TERM_MEMORY.md`
§8（分层分期，决定 `memory.py` 的第二次搬迁）。**未决**：`mcp.py` + `utils.py` 的落点（见四）、
A/B/C 要不要给"闸门判定的配置"开例外（本条的结论是不开，表因此不动）。

### 落地（2026-09-21 当日做完）

- **一 · `app/resource/` 包**：`paths.py`（原 `app/resource.py` 原样搬入，只多一级 `parent`）/
  `memory.py` / `skills.py`，新增 `images.py`（从 `LLMNode` 剥出）与 `profile.py`（同）。
  `__init__.py` **只转出 paths 那几个函数**——`RESOURCE_ROOT` 刻意不转出：它是可 monkeypatch 的
  模块全局，转出去会让 `app.resource.RESOURCE_ROOT = …` 这种补丁**静默失效**（理由写在 `paths.py`
  的 docstring 里；9 个测试文件改成 `import app.resource.paths as resource`）。
  **红利兑现**：`app/platform/runtime.py` 的 `ensure_memory_template` 懒加载去掉、改顶层 import
  （资源层不 import app.agent，`import app.platform` 仍不触发 .env）。
- **二 · `model.py` 并入 `LLMNode`**：见上一条的落地记录（同一件事）。
- **三 · `app/agent/gates.py`**：见上一条的落地记录。三处读表收拢成一份缓存；
  **`tool.json` / `command_policy.json` 保留在原地**（纪律：表与解析器同住）。
- **四 · `mcp.py` + `utils.py` 出 `app/agent/`**：**用户拍板归基座** → `app/platform/mcp.py`（运行体
  机制）与 `app/platform/tool_results.py`（工具结果归一化）。两条代价已认下：① 这是全仓**唯一一处
  反向 import**（`app.agent.*` → `app.platform.*`，单向；这两个模块不 import app.agent，故
  `import app.platform` 仍不触发 .env）；② `import app.platform` 会连带拉进
  `langchain_mcp_adapters`（重，但不读 .env）。`loop.py` 的两处懒加载随之改回顶层——原先懒加载的
  理由是"import 即需 .env"，那个理由随搬家消失了。
- **记账三条的现状**：`ensure_memory_template` 仍住 `memory.py`（未切，性质是资源生命周期）；
  `skills.py` 的**加载机制半**（`read_skill_env` / `read_requirements` / `read_preflight` /
  `server_module`）**仍未切**（拍板暂不切）；`handoff_pointer_block` 仍在 `prompt.py`。
- **未决（本轮已消化）**：`mcp.py` + `utils.py` 的落点 → `app/platform/`（用户拍板）；
  A/B/C 给不给"闸门判定的配置"开例外 → **不开**，表与解析器同住，故 `gates.py` 留在 `app/agent/`。

## [x] `dispatch_subtasks` 该跟 worker 同侧走 MCP；memory 按拍板留在编排工具（2026-09-21）

**动机**：起点是 `LLMNode` 越来越肿（`app/agent/nodes.py`，336 行 = 装配 35 + 图片通道 139 +
画像 31 + 调模型若干）。但 LLMNode 只是症候——真正的病是 **`app/agent/` 是这套架构里唯一没有
入目录判据的目录**：`app/schema/`（只有属性、可序列化的纯数据）、`app/platform/`（基座纪律）、
`app/tui/`（UI 协议）、`mcp_service/`（是不是一个 MCP server）、`skills/`（是不是可插拔领域包）
都有可判定的标准，只有 `app/agent/` 的标准是"agent 要用的东西"——等于没有标准。于是任何新功能
问一句"算基座吗？算 MCP 吗？"，答案都是"不算"，只能落进来。**顺着这条线查工具面，发现同一个病
在编排队列重演：判据也是混的。**

**现状（2026-09-21 实测）**：编排工具 10 个，按注入需求分三组——

| 注入 | 工具 |
| --- | --- |
| 只 `InjectedState` | 计划三件套、`read_note`、`ask_user` |
| `InjectedState` + `InjectedWorkspace` | `get_skill`、`drop_skill` |
| **只 `InjectedWorkspace`** | **`write_memory`、`read_memory`、`dispatch_subtasks`** |

第三组的**返回形状**与普通 MCP 工具相同（纯回执）——但**返回形状不是判据**（见
`docs/ARCHITECTURE.md` §4 的四问）：按"改变 agent 自身运行语义"这条定义，memory 改的是跨会话
先验、dispatch 改的是执行结构，**两件都算编排**，它们待在该队列里是对的。真正出错的是它们在编排
队列里的**理由**——`app/agent/tools.py:534-536` 的注释写着："与 dispatch 同走 OrchestrateNode，
**因为都需要注入工作区**（对模型隐藏的 `InjectedWorkspace`）。"

**这条因果链是错的**，反例在同一份表里：`mcp_service/file_io.py:32` 同样需要工作区
（`_workspace_path = os.environ.get("WORKSPACE_PATH")`），却是普通工具、走 `ToolNode`。真正的
链条是：

1. 它们是**主进程内的普通函数**（不是独立进程）；
2. 主进程**同时服务多个工作区**——`evaluation/runner.py:230` 每任务 `build_eval_graph(workspace, …)`
   建一张图、一个进程串跑 N 个任务；`langgraph dev` 更是一图多 thread。**env 是进程级的，一个
   进程只有一个值，表达不了"我是哪个工作区"**；
3. 所以只能把 workspace 当参数递进去 → `InjectedWorkspace`（`app/agent/tools.py:384`）；
4. 而**唯一能代填这个注入的执行器是 `OrchestrateNode._invoke`**（`ToolNode` 走的是
   `await tool_obj.ainvoke(args)`，没有注入源）；
5. → 只能待编排队列。

**所以"需要 workspace"不是理由，「住在主进程 + 一进程多工作区 ⇒ 必须靠注入」才是。** 前者是
处境，不是结构。`file_io` 从不需要 MCP 化，因为它一开始就是进程——工作区就是它的启动配置。

**决定（2026-09-21 拍板；与上一轮"两者都独立成 MCP"的提议不同）**：

按 `docs/ARCHITECTURE.md` §4 的**四问**逐条走——两者**第一问、第二问相同**（都不是对外部世界的
调用；都改变 agent 的运行语义），分岔在第三、四问：

- **memory 留在编排工具**：命中**第三问**（还涉及"持久化 / workspace"等资源）→ 方向是
  **Resource abstraction**（资源访问单点），**不是 MCP**。附带好处：读写都在主进程，避开"注入在
  主进程读、写入在子进程写"这对跨进程碰同一批文件的组合。
- **dispatch 走 MCP**：命中**第四问**（需要独立进程 / 独立 Agent runtime——它派生 worker）→
  **multi-agent 正落在这里**。它与 `mcp_service/sub_agent.py` 是**一对**（一个拉起、一个被拉起），
  worker 既然住在 MCP 侧，拉起它的入口就该在同一侧。

**代价与副产物（要摆明）**：

1. **编排队列仍要求两种注入**（`InjectedState`：计划三件套 / `read_note` / `ask_user` /
   `get`·`drop_skill`；`InjectedWorkspace`：`write_memory` / `read_memory`）——但**注入不是判据，
   是后果**：凡"改变 agent 运行语义"的工具都住在主进程，住主进程才需要注入（四问见
   `docs/ARCHITECTURE.md` §4）。`ORCHESTRATE_SOURCES` 只删 `"dispatch"`，`"memory"` 保留。
2. **`InjectedWorkspace` 的定位随之清楚**：它是"住主进程"的后果，服务的是按第二问/第三问留在
   主进程的工具（memory×2 / `get`·`drop_skill`）——不是历史遗留，但也不是判据（见未决 3）。
3. **`app/resource/memory.py` 的归属问题不变**（未决 1）：消费者仍跨 agent + platform 两处，
   与 dispatch 搬不搬无关。

**dispatch 迁移的工作项（落到 `mcp_service/dispatch.py`）**：

- **落点**：新建 `mcp_service/dispatch.py`，**不并进 `sub_agent.py`**——后者是 **worker 侧**的
  server（跑在被拉起的子进程里），dispatch 是**主侧**入口；同放一个 server 会变成"这个 server
  的进程里再 spawn 一个同款 server 进程"的嵌套。
- **`_PROJECT_ROOT` 要重算，且不能靠 cwd**：`tools.py:46` 是
  `Path(__file__).resolve().parent.parent.parent`，搬到 `mcp_service/` 下要改成 `parents[1]`。
  **尤其别靠 cwd**——`stdio_connection` 给 server 设的 `cwd=workspace`（工作区）而非项目根；
  项目根只能靠 `PYTHONPATH`（它已被设成 `PROJECT_ROOT`）或 `__file__`。
- **`AGENT_INBOX_URL` 的转发链多一跳，必须显式接上**：现在是
  `主进程 env →（_worker_child_env）→ worker`；搬后变成
  `主进程 env →（stdio_connection 的 extra_env）→ dispatch server env →（server 内组装）→ worker`。
  **漏掉中间那跳不会报错，只会静默发往默认端口**——正是 `tools.py:417-418` 注释警告过的失效。
  主侧 `_build_servers` 要为这个 server 读 `os.environ["AGENT_INBOX_URL"]` 塞进 `extra_env`。
- **`worker_tools()` 的排除要复核**：现在靠 `source: "dispatch"` 不命中 `_WORKSPACE_SOURCES` /
  `_WEB_SOURCES` / `worker_allow` 而被剔除（`tools.py:94-99`）。搬成 `mcp_service/dispatch` 后
  **仍然不命中**，防线自动保持——但**别顺手把它加进 `_WORKSPACE_SOURCES`**（那是"新 mcp_service
  server 都算工作区只读"的错误推广），那会打开**递归派发**。
- **`tool.json`**：`source` 改 `mcp_service/dispatch`、`need_review` 维持 `false`；
  `ReviewNode.ORCHESTRATE_SOURCES` 删 `"dispatch"`。
- **顺带简化**：现有 `async + asyncio.to_thread` 包装（`tools.py:538-540`，为避开 langgraph dev
  的 blockbuster 拦事件循环里的阻塞调用）在子进程里**不需要**——主进程的 blockbuster 管不着
  MCP server 进程（`file_io` 的工具就是同步的）。

**与「长期记忆分层化」的关系**（该条见下方 `[ ] 长期记忆分层化`）：memory 既然留在编排工具，
分层要改的就是**编排工具自己的签名**（加 `scope`）——不再有"MCP 化顺带重签"这回事，一次改完
即可。该条 §待拍 5（"工具面怎么指定层"）问的仍是那个签名。分层后 ① ② 层跨工作区、路径不在
`resource/<ws_key>/` 下，这条与 memory 是否 MCP 化无关，仍要在 `app/resource/paths.py` 长出路径单点
（注入在主进程 `LLMNode`，直接 import 即可）。

**未决**：

1. **`app/resource/memory.py` 的归属**（独立于 dispatch，仍然成立）：它被三方消费——`LLMNode`
   （注入用的 `memory_block`）、`app/platform/runtime.py`（`ensure_memory_template` 播种）、
   `app/agent/tools.py`（读写工具）。**一个被 agent / 基座两处 import 的模块不该住在 `app/agent/`
   里**——它和 `app/resource/paths.py` 是天然的一对（一个管路径 `workspace_key`/`memory_root`，一个管
   内容解析/追加/覆写），该做邻居。是否本轮一起搬？
2. **dispatch 的审批策略**：`need_review` 维持 `false`（"派发本身免审"是既有语义），还是借搬家
   重审？（worker 侧真正有副作用的调用仍各自过闸门，见 `docs/MULTI_AGENT.md`。）
3. **`InjectedWorkspace` 的存废与落点**：dispatch 搬走后它剩 memory×2 / `get_skill` / `drop_skill`。
   按上面"代价 2"它是**长期设施**而非将就——那么 `tools.py:384` 的 docstring 得改（现在写着
   "dispatch 往该工作区 spawn worker，memory 系列工具用它定位记忆文件"，dispatch 那半将过时），
   以及要不要把它挪到更显眼的位置、把"只为留在主进程的工具服务"这条定位立住？

**已顺带修（2026-09-21）**：`OrchestrateNode` 的 docstring 曾把"**不产生真实副作用**"写成编排
工具与普通工具的关键区别——**10 个里 4 个有**（`dispatch_subtasks` / `write_memory` /
`get_skill` / `drop_skill`）。那句话正是 ReviewNode 承重约束「interrupt 只能待在'挂起之前无副
作用'的节点里」的依据，留着会误导后来者把闸门搬进本节点。已改为：关键区别只留**返回值的形状**
（state 切片 vs `ToolResult`），副作用逐条点名（无的四个 / 有的四个），并补一条 ⚠️ 明写"本节点不
承载 interrupt、闸门唯一落点在 ReviewNode"。

**相关背景**：`docs/LONG_TERM_MEMORY.md`（§5 的路径安全不变量——"模型碰不到记忆文件路径"；
memory 留在编排工具，这条靠 `InjectedWorkspace` 继续对模型隐藏参数；§8 的分期）、
`docs/MULTI_AGENT.md`（dispatch 的形态与跨进程审批回流）、`app/platform/mcp.py`（运行体常驻机制，
`mcp_service/dispatch.py` 直接复用同一套 `_ServerWorker`）。代码落点：`app/agent/tools.py`
（`InjectedWorkspace` / `write_memory` / `read_memory` / `dispatch_subtasks`）、
`app/agent/nodes.py::OrchestrateNode._invoke`（代填逻辑——memory 留下则它仍是必需）、
`app/agent/tool.json`（`source` 与 `need_review`）、`app/resource/paths.py`（路径单点）。

### 落地（2026-09-21 当日做完）

- **落点**：新建 `mcp_service/dispatch.py`（**不并进 `sub_agent.py`**——后者是 worker 侧的 server，
  dispatch 是主侧入口，合起来会变成"这个 server 的进程里再 spawn 一个同款 server 进程"）。
  `_PROJECT_ROOT` 用 `Path(__file__).resolve().parents[1]`（**不靠 cwd**：stdio_connection 给的 cwd
  是工作区）；`source` 改 `mcp_service/dispatch`；`ReviewNode.ORCHESTRATE_SOURCES` 删 `"dispatch"`。
- **`AGENT_INBOX_URL` 的转发链显式接上**（本条最容易静默失效的一跳）：
  `主进程 env →（新增 _build_servers::_inbox_env）→ dispatch server env →（它内部的
  _worker_child_env）→ worker`。新用例钉住"有地址就带、没有就不带"
  （`tests/test_mcp.py::test_dispatch_server_env_forwards_inbox_url_only_when_present`）。
- **递归派发的防线自动保持**：`worker_tools()` 的两条 source 规则都不命中它、也没有 `worker_allow`
  → 天然被剔除；新用例把它写成断言
  （`test_dispatch.py::test_source_registered_and_stays_out_of_orchestrate_and_worker`），并在
  注释里点名"**别把它加进 `_WORKSPACE_SOURCES`**"。
- **顺带简化**：`async + asyncio.to_thread` 的包装去掉（主进程的 blockbuster 管不着 MCP server
  进程）；新增参数校验——空 `sub_tasks`（或全空白）→ `InvalidArgumentError`，不再回一条
  "已并发派出 0 个 worker"让模型以为查过了。
- **未决逐条处置**：① `app/agent/memory.py` 的归属 → **本轮一起搬**（→ `app/resource/memory.py`）；
  ② dispatch 的审批策略 → **维持 `need_review: false`**（用户拍板：派发只是起只读资料收集 worker，
  worker 侧真正有副作用的调用各自过闸门）；③ `InjectedWorkspace` 的存废 → **留存**（仍服务
  memory×2 / `get_skill` / `drop_skill`），docstring 重写为"**它是'住在主进程'的后果，不是判据**"，
  并点名 dispatch 已离开这条路径。
- **验证**：`tests/test_dispatch.py` 重写（9 例：接线 / env 转发 / 并发汇总 / 失败隔离 / 空列表 /
  工作区缺失）、`tests/test_dispatch_worker_approval.py` 改为**直接调 MCP 工具**跑真链路
  （批准 → 工具真执行、拒绝 → 回灌 `[approval_denied]`，断言不变）、`tests/test_mcp.py` 补一跳转发用例。

## [x] 技能：project-handoff（跨会话交接）——已落地（2026-09-19）

**动机**：Theta 换会话 = 任务态全丢——`/new session` 之后 notes、`current_plan` 全部留在旧
checkpoint 里，长期记忆又只收"约束/偏好/项目事实"、不收**任务态**（做到哪、下一步是什么、
哪些决定有效）。工作做一半想换新会话，只能靠用户人工重述。外部仓库
[duoduoler-ops/Table-skills](https://github.com/duoduoler-ops/Table-skills) 的
`project-handoff` 技能正好补这个缺：在压缩检查点/阶段切换/已核实的混淆时评估"该不该交接"，
提醒 → 保存材料 → 新建会话并接续。

**技能内容概要**（原文 SKILL.md + 两个附页 `references/compaction-reminder.md`、
`references/handoff.md`）：核心纪律是**「提醒、保存、新建接续是不同动作，不互相授权」**；
评估只看任务事实——**安全位置**（成果/验证/归属可定位，未验收不能写成已验收）、**明确后续**
（新对话能确定第一步）、**切换收益**（不能只说"对话太长"）；首次提醒在第 3 次自动压缩后，
之后同阶段去重、冷却 ≥ 3 次压缩；建议放最终答复正文最前（"交接建议："开头）；用户说
"先不交接"再冷却；"本任务不提醒"关掉主动提醒直到用户主动恢复；无 Hook 时退化为"最终答复前
自检，不宣称有程序兜底"。

**落点判定**：`skills/project-handoff/`，**知识型**（原文无 server.py、无工具面，是行为
纪律不是能力包）；SKILL.md 正文走既有的 `get_skill` 注入通道（docs/SKILL_DESIGN.md §11）。

**适配 Theta 须拍板的**：

1. **references 不可达**：知识型只注入 SKILL.md 正文，而 `skills/` 在 Theta 仓库里、通常
   **不在用户工作区沙箱内**——agent 的 file_io 读不到两个附页。三选一：内联进正文（体积换
   可达）；技能通道扩展"host 侧按需读 references 注入"；或砍掉附页、正文自足。
2. **触发锚点**：原文锚在"第 N 次自动压缩后"，Theta 的 `compact_node` 是轮末确定性折叠、
   没有"压缩次数"概念，hooks 也没有。候选：给 state 加 compact 触发计数；或按技能自带的
   退化模式起步——靠模型在阶段切换/已核实混淆时自评。**倾向后者起步**，锚点等运行中 compact
   （上面那条）落地再搭车。
3. **保存落点**：交接材料是任务态，**别写进 memory.md**（污染"只存重取不到的结论"的既有
   语义）。候选：`resource/<ws_key>/handoff/`（`app/resource/paths.py` 加单点函数）或工作区内
   约定路径。落点定了才谈"新会话怎么知道有交接材料"（新会话播种时提示模型去读？）。
4. **动作映射**：保存 ≈ 资源区写入（免审与否沿用现有策略表）；新建接续 ≈ `/new session`
   （已有）；恢复/查看 ≈ `/session <id>`（已有）。"在指定目录新建并打开"在 Theta 没有对应物
   （工作区 = 启动目录），砍掉或映射成"提示用户到目标目录重启"。
5. **合规**：外部仓库搬进来要按 Theta 口径改写（中文文档、工具面、会话命令、闸门语义），
   不是原文照抄；来源 URL 与概要留档在本条。

**落地（2026-09-19，五个待拍的逐条定案）**：

1. **references → 正文自足**：原文两个附页拿不到（网络受限，用户只贴了主文），且正文本就
   把关键纪律收拢了——技能写成单文件自包含，不做 references（要扩通道等真有需求再说）。
2. **触发锚点 → 退化模式起步**：无压缩计数、无 hooks，按原文自带的降级路径——模型在最终
   答复前自评三条件（安全位置/明确后续/切换收益）；同阶段去重、"先不交接"冷却、"本任务
   不提醒"关闭，全部写成正文纪律。压缩锚点等运行中 compact 落地再搭车。
3. **保存落点 → 工作区根 `HANDOFF.md`**：不进 `resource/`（agent 的 file_io 沙箱就是工作区，
   用既有 `create_file`/`write_file` 过审批写入——**零新工具**，人看得见保存了什么）；
   整份覆盖重写（任务态快照，不是追加日志）。
4. **动作映射**：保存 = 写 HANDOFF.md；接续 = `/new session`（已有）+ 新会话自动注入指针；
   查看 = 读文件；"在指定目录新建并打开"砍掉（工作区=启动目录，无对应物）。
5. **合规**：已按 Theta 口径改写（`skills/project-handoff/SKILL.md`，知识型、无 server.py、
   无 skill.json；来源 URL 在上）。
6. **host 侧新增"接续"入口**：`prompt.py::handoff_pointer_block`——工作区根存在
   HANDOFF.md 时注入一段指针（**只 stat 存在性、不读正文**，正文由模型按需 read_file）；
   挂在 `LLMNode` 的 `inject_session_context` 分支下，worker 结构性吃不到。新会话靠这一行
   知道"有交接可接"。与长期记忆的分工写进技能正文第 5 节（memory 收跨任务约束，HANDOFF.md
   收当前任务态）。
7. **验证**：`tests/test_handoff.py` 4 例（指针有无 / LLMNode 注入与 worker 排除——对假模型
   收到的消息断言，因为系统消息是临时列表不进返回值 / scan_skills 可发现）。全量
   **566 passed / 7 skipped**；层 A 8/8 退出码 0（评估工作区无 HANDOFF.md，注入恒为空，
   层 A 对此改动本就不瞎）。

**相关背景**：docs/SKILL_DESIGN.md §11（知识型通道）、上文「运行中 compact」（压缩锚点若
落地，本技能触发点可搭同一班车）、「长期记忆分层化」（交接材料落点的分层语义别打架）。

## [x] 技能工具的审批声明随技能走：tool.json → skill.json（2026-09-19）

**动机**：用户问"既然技能工具的存在性随技能走（加载出现、卸载消失），为什么审批策略反而
放在中心的 tool.json？"——约束三分法确实没收尾：软约束在 SKILL.md、env/requirements/
preflight 声明在 skill.json，唯独硬闸门要跑中心表登记，加技能得动两处。拍板：**搬到
skill.json**（同日）。

**落地**（详见 docs/SKILL_DESIGN.md §13.11）：

- `skill.json` 新键 `tools`：`{"<tool_name>": {"need_review": bool}}`；
  `skills.py::read_tool_policies` 是解析单点。
- `_check_skill_tools` 从"查 tool.json 的 source"改为三向校验：**暴露未声明 → 拒载**（原
  fail-closed 契约）；**声明形状不对 → 拒载**；**声明了没暴露的工具 → 拒载**（拼错名字的
  静默形态——旧中心表结构上查不出这一向）。
- `ReviewNode._tool_cfg` 两级查询：内置表优先 → 已加载技能的声明；`_decide` 的第二道防线
  改为"在工具表里却**两头**无政策 → review"（原语义保留）。读盘不缓存：改 skill.json
  **重新 get_skill 即可**，比旧 tool.json 的"改完要重启"少一步。
- **留 tool.json 的**：内置 MCP 工具政策、编排工具 `source` 分流、worker 的 `worker_allow`
  ——host 的结构性决定不随技能走。
- **信任条款**：自报政策可信的前提是技能源 first-party（同仓库）；将来开放外部技能源时，
  外部声明 host 不认、一律 `need_review: true` 兜底。
- **迁移**：github 17 条（14 免审 + 3 写）搬进 `skills/github/skill.json`；echo fixture
  落 `tests/fixtures/skills_pkg/echo/skill.json`。
- **验证**：`tests/test_skill_runtime.py`（未声明拒载 / 声明了没暴露拒载 / 形状不对拒载 /
  ReviewNode 两级查询四连断言）+ e2e 的"真表"改读 skill.json。全量 **566 passed /
  7 skipped**；层 A 8/8 退出码 0。

## [x] 低危清理第二批 + 评估 CLI 退出码（2026-09-17）

**动机**：两件都是"已有性质没兑现"的收口——① 低危清理是 2026-09-15 减法审计的**剩余清单**
（清单本身见下「减法审计遗留」）；② 评估的层 A 回放**零成本**却进不了 CI，因为 CLI 没有退出码。

**落地**：

- **评估退出码三档**：`0` 通过 / `1` 不通过 / `2` 用法错误（`--task` 名不存在）。判据收在
  `evaluation/report.py::print_report` 的**返回值**里（打印与判定同一函数，避免两处口径漂移），
  三条同时成立才算通过：**检查全过** ∧ **图本身没报错** ∧ **至少有一个任务真跑了**。
  后两条是刻意加的——它们正是"光看通过率会漏成绿色"的口子：图报错的任务往往一条检查都没有
  （`0/0` 看着像满分），而全部 SKIP（fixture 缺失/陈旧）更是什么都没验证。SKIP 单条仍不算失败
  （同"不计入通过率"的口径）。用法侧写在 README「模型行为评估」。
- **低危清理**：
  - **重复真相**：`tools.py::PLAN_STATUSES` 改为 `get_args(PlanStatus)` 派生；`known_names` 里
    `{…} or set(_ORCHESTRATE_TOOL_NAMES)` 的空表兜底**删除**——`_static_tools` 生产恒由构图期
    传入（`graph.py:101` 组装、`:107` 构造 `SessionToolset`），兜底只会把接线 bug 藏成
    "编排调用被判成未知工具"，症状更难查；
  - **校验口径拉齐**（`mcp_service/web_search.py`）：`extract_urls.format` 与
    `crawl_website.extract_depth` 按各自 docstring 的声明补校验（此前只有
    `extract_urls.extract_depth`、`crawl_website.max_depth`、`deep_research.model` 有），
    并删掉空的 `# ... 其他校验 ...` 占位。**`deep_research.citation_format` 不加**：它的
    docstring 写的是"**如** numbered/mla/apa/chicago"= 举例而非枚举，加了就是凭空收紧。
  - **恒假条件**：`mcp_service/file_io.py` 的 `if total == 0 or lo > total` 前半删除
    （`lo ≥ 1` 由上游保证，空文件天然落在 `lo > total` 里）；
  - **过时注释三处**：`needs_compact` 上方错位的 `_FOLD_HEADER` 注释（它早已搬进类体）、
    `ui_schema.Notice` 的"**将来的**帮助/会话切换"（早已落地）、`session_schema` 把渲染者指成
    `commands/__init__.py`（实际是 `commands/session.py` 自己把行文本包成 `Notice`）。

**验证**：新增 `tests/test_eval_report.py`（6 例：通过判据的三种红 + 部分 SKIP 仍绿 + 用法错误
退出码）；全量 **528 passed / 7 skipped**；层 A 真跑 `python -m evaluation` = 8/8、退出码 `0`，
`--task nope` 退出码 `2`。

**未做**（都还在「减法审计遗留」的遗留清单里，均低危）：`read_body` 删除（需同步改 4 条用例）、
`MCPToolSpec.metadata` 去留、`file_io` ↔ `mcp.py::_validate_workspace` 的 `WORKSPACE_PATH` 去重、
`file_io:699-700` 的 `ext_set`、github server 的 GBK 半防线。

## [x] agent 提问模式：需求不明确时，给用户选项等回答（2026-09-16）

**动机**（2026-09-10 记）：agent 把"不明确之处的选项"摆给用户、等回答。本质是**把"审批闸门"
泛化成"人机闸门"**：在此之前图↔人的往返只运一个 bool（`Command(resume={"approved": …})`）。

**落地**（2026-09-16）：见下。原设计要点里那几条"须钉死"的，决定一并记在这里。

**四条拍板**（用户 2026-09-16）：

1. **复用 interrupt**（不选"工具把选项写进对话、不挂起图"那条）：给闸门加第二种载荷。
   payload 的 `type` 区分 `tool_approval`（既有）/ `ask_user`（新），不新造通道。
2. **`UI.decide` 的返回值泛化**成 `Decision` 值对象（`app/schema/approval_schema.py`）——
   一个闸门、一条通道。这是"为第二个前端留的缝"的第一次实质扩展。
3. **无人回答 = 未回答**（不设超时，人一直在等）：回执如实写"用户未作答"，模型按最佳判断继续
   并在收尾时说明假设。与审批的 fail-closed（EOF → 不批准）**刻意不同**：没人批必须挡下动作，
   没人答只意味着模型得带着假设走。
4. **回答是两段**：**选一个选项** + **一段补充说明**（补充挂在**用户侧**，不是模型多给的字段）。
   两段都可留空，也**可以只给其中一段**——"没选任何给定选项、自己写了一段方案"是**有效回答**，
   不是没回答（回执单独有一档写清它）。两段皆空才算未作答。

**落点与形状**：

- **工具**：`app/agent/tools.py::ask_user(why, question, options)`，`source: "ask"`；
  `why` 必填（对齐 `run_command` 的 `description`），`options` 0–5 项（留空 = 开放式提问）。
- **闸门在 `ReviewNode`**，不在工具体里。**这条是承重约束**：LangGraph 的 resume 会把节点
  **整体重跑**（interrupt 之前的外部副作用会真切第二次），而 `OrchestrateNode` 同批次里可能有
  `write_memory`——所以 interrupt 只能待在"挂起之前无副作用"的节点。该行为已实测钉死，
  结论写在 `ReviewNode` 的 docstring 里。
- **回答的回程走 state 切片** `ask_answers[tool_call_id]`：**闸门的产出是 state，执行器消费
  state**（与 `approved_*` 队列同一条路子）。`ask_user` 于是是个**有真身体**的编排工具——
  拿到两段回答 + 自己的 `options` 渲染出回执。好处是**路由零改动**（复用既有的
  `orchestrate_node → llm_node`），也不必改写模型的 tool_call（args 保持原貌）。
- **worker 结构性排除**：`source: "ask"` 不在 `worker_tools()` 的两条 source 规则里、也没有
  `worker_allow` → 不登记即不可能调用；`WORKER_SYSTEM_PROMPT` 另补一句"你不能向用户提问"。
  worker 的 HTTP 回传仍只认审批（`ApprovalInbox.status` 保留 bool 投影，不随 Decision 泛化）。
- **参数在弹面板之前校验**（`nodes.py::_ask_request`）：编排工具不走 langchain 的参数校验管线，
  "必填/上限"得自己拦——把一个空问题或 6 个选项摆到人面前，比回一条可行动回执让模型自己改更糟。

**验证**：`tests/test_ask_user.py`（19 例：闸门分流 / 参数校验 / 四档回执 / 登记与 worker 排除 /
真图端到端）+ `tests/test_main_tui.py` 的提问面板一档（选项、自由文本、越界、EOF 两处）；
全量 489 passed。

**未做**：提问的超时（与审批一样刻意不做）；`/ask` 命令（提问由模型驱动，不经控制面）；
worker 侧的提问通道（结构性不需要）。

**缺口已补（2026-09-17）：复选（复选框）。**

原状是「**一个选项** + 一段自由补充」，序号在六个环节全按**标量**写死；模型想表达"这几项可以
同时要"时只能绕路——*造一个组合选项*（"两者都要"），或指望用户把"1 和 3 都要"写进补充里再由
它自己解析。现已把序号泛化成**列表**、把模态的选择权交给模型。

- **形状**：`Decision.option_indexes: list[int]` 与 `AskAnswer.option_indexes: list[int]`
  （空列表 = 没选；**单选是它长度 0/1 的特例**，不为两种模态分叉）。**必须是 `list` 不能是
  `tuple`**——它随 resume 字典与 `AskAnswer` 过 checkpoint 序列化，tuple 会漂成 list，故上面
  原设计记录的"倾向 tuple"按这条**推翻**。同时**删掉 `Decision.option_text`**（生产零消费者：
  工具自己持有 `options`，消费端按序号取原文）。
- **一条数据链**：`Decision.option_indexes` → `app/platform/turn.py::decision_to_resume`
  （**唯一翻译点**）→ `_normalize_ask_answer`（逐个越界校验、丢越界项、去重保序）→
  `state.ask_answers` → `ask_user` 渲染回执。
- **工具面**：`ask_user` 加 `select: Literal["one","many"] = "one"`（schema 里是 enum、默认
  单选）；工具 docstring 与 `SYSTEM_PROMPT` 的 `[提问 · ask_user]` 都补了"什么时候该用多选"
  （选项**互相独立可叠加**时用 many；**互斥取舍**保持单选——能单选就别多选）。
- **面板（终端）**：`app/tui/ui.py::parse_option_indexes` 认逗号（半角/全角）与空格分隔；
  **容错按模态分**——复选**丢掉越界项、保留合法的**，单选则**提示并重问**（`1,3` 与 `9` 都重问；
  单选容不下第二个有效态，静默丢一半比让人重输更糟）。重问**可逃逸**：空回车跳过、任意文本当
  补充、EOF 落回未作答。
- **回执**：复选写明"**多选，共 N 项**"（**哪怕只勾 1 项**——不写会让模型怀疑丢项了）；
  单选的四种文案**逐字未变**（有断言钉住，多选是纯增量）。

**验证**：`tests/test_ask_user.py`（32 例，含复选分流 / 丢越界项 / 去重 / 回执文案 / 真图 e2e
多选闭环）+ `tests/test_main_tui.py` 提问面板（三种分隔符、越界重问与丢弃提示、EOF 逃逸、
复选提示行、解析器不炸不误判）+ `test_platform_loop.py` / `test_tui_agent_run.py` 的 resume
形状。全量 **556 passed / 7 skipped**；层 A 回放仍 **8/8 且退出码 0**（改提示词不会让 fixture
陈旧——指纹只比任务的 prompt + setup）。

**顺带修掉一个既有隐患**：面板的序号解析原先用 `str.isdigit()`，而它对 `²`（Numeric_Type=
Digit）也返回 True、`int("²")` 却抛 `ValueError`——用户从文档粘一个上标字符进来就会炸掉整个
面板。已改用 `isdecimal()`（为真 ⟺ `int()` 一定解析得动），并加了"不炸不误判"的参数化用例。

**漏网落点（原"六处"清单之外）**：`app/platform/approvals.py::_record_to_value` 在提问分支是
**白名单透传**——不同步加 `select`，面板永远收不到模态（`type`/`why`/`question`/`options` 都列了，
新字段最容易漏在这里）。

**仍未做**：多选下的**区间输入**（`1-3`）——刻意不做（多一个语法面要测，且与"补充里写的话"
边界更模糊）；提示词文案的实际效果层 A 测不了（回放的模型不读输入），要验得跑 `--live`。

## [x] 思考模式：配置化 + "思考不进 messages"（2026-09-14）

**动机**：厂商的思考（reasoning/thinking）此前既没有开关、也没有任何观测点——当前 `.env` 指向的
智谱 GLM-4.7 系列**默认就开思考**，代码里却既无参数也无痕迹，等于"花了钱却不知道"。

**落地**：

- **配置三态**：`app/config.py::CHAT_THINKING`（`enabled` / `disabled` / 留空 = **不下发该参数**、
  交给服务端默认）→ `app/agent/nodes.py::LLMNode::thinking_extra_body()`（**provider 映射的单点**）→
  当 `extra_body=` 传给 `ChatOpenAI`（它是一等字段，不走"未知 kwargs → model_kwargs"那条带警告的路）。
  **默认留空 ⇒ 行为与现状完全一致**。`.env.example` 有分区说明。
- **"思考不进 messages" 天然成立，故不写剥离代码**：`langchain-openai` 的响应转换是白名单式的，
  `reasoning_content` 压根没被提取进 `AIMessage`（实测：把带该字段的假响应喂真实转换路径，
  `additional_kwargs` 是空的）。但这条**依赖库的行为**，所以用一条**契约用例**钉住
  （`tests/test_thinking_config.py`）——库哪天开始提取，用例会红，那时在 `LLMNode` 的唯一写回点补剥离
  （那里有注释指向该文件）。
- **实调用验证**：同文件一条 opt-in 用例（设 `THETA_LIVE_THINKING=1` 才跑，默认 skip），真打一次 API
  断言"厂端确实返回思考 + 正文非空 + 我们这边拿不到思考"。端点限流/网络不通时 **skip 而非 fail**
  （环境问题不是被测代码的缺陷）。

**实测记录（2026-09-14，`glm-4.7-flash` / 智谱 BigModel，同一问题只改参数）**：

| 配置 | `content` | `reasoning` | reasoning_tokens / completion |
| --- | --- | --- | --- |
| `enabled` + `max_tokens=300` | **空** | 509 字 | 299 / **300** |
| `enabled` + 不传 `max_tokens`（= 本仓库现状） | 正常 | 1547 字 | 865 / 889 |
| `disabled` + `max_tokens=200` | 正常 | 0 字 | 0 / **26** |

⚠️ **思考 token 计入 `completion_tokens`，因此也计入 `max_tokens`**：给少了会"想完没话说"（第一行）。
本仓库不设 `max_tokens` 所以安全，但哪天要设，必须把思考的额度算进去。

**没做**：不显示思考（`ui_schema` 只有 5 种事件、`TurnFinished` 只带正文）；不做 `/think` 会话级开关
（本期按环境级走）；不碰 `clear_thinking`（服务端默认 `true` = 历史思考不随上下文给模型，正是
"不进 messages"的对应语义）。**"跨轮保留思考"是另一件事**：它要求把思考**逐字原样回传**，
与"不占历史"直接冲突，要做得重新拍板。

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
