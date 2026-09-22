# Theta $\theta$ 待办 / 收口清单

> 当前工作树仍在演进：文档与代码不一致时以代码为准（约定同 CLAUDE.md 与 docs/README.md）。
> 每条记录动机/现状，避免"当初为什么没做"再次翻车；勾掉前应能指到验证它的提交。

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


## [x] 枚举 / 标签的真相在哪（2026-09-22 审计 + 第一批已落地）

**判据已落纸**（`docs/ARCHITECTURE.md` §4.6）：一个词表只有一份真相，落在"谁定义这个词"的那一侧——
跨边界 → `app/schema`（能派生就派生）；停在单模块 → 留模块但用 `Literal`（别裸 `str`）；住在数据文件
（`tool.json`）→ 数据是登记处、**取值清单要在代码里有一处**供派生与校验。外加两条：同义不许两套拼写、
名字清单不许手抄。

**实测分布（四类；按"静默失败"风险排）**：

**A · 已有单一真相（标杆，别动）**：`PlanStatus`（+`PLAN_STATUSES` 用 `get_args` 派生）、
`SearchDepth/Topic/TimeRange`（贴上游 Tavily）、`Decision.kind` / `ApprovalStatus` / `GATE_*`、
`_ORCHESTRATE_TOOL_NAMES`（由工具对象派生）。**例外两处**：`LLMNode.format_plan_status` 的
`status_map` 手抄三种状态、`approvals._record_to_value` 的 `or "one"` 抄了 `ASK_SELECT_DEFAULT`。

**B · 同义两套词（同一件事两种拼写）**：

| 词表 | 两套写在哪 | 唯一映射点 | 问题 |
| --- | --- | --- | --- |
| 闸门种类 | `Decision.kind ∈ {approval, answer}`（schema）↔ payload `type ∈ {tool_approval, ask_user}`（`GATE_*`） | 只有 `turn.decision_to_resume` 的 if 分支 | **没有一处同时写着两张词**，人得读两个模块才敢确定对应关系 |
| 可选模态 | `"one"/"many"`：`tools.ask_user` 的 `Literal`、`gates.ASK_SELECT_MODES/_DEFAULT`、`approvals` 的 `or "one"`、`panels` 的 `== "many"`、`tui/ui.py` 的 `== "many"`、`prompt.py` 的说明 | 无 | **8 处字面量（6 处代码判断）**；加第三种模态要改 6 处 + 提示词，漏一处就静默不一致 |

**C · 词表只活在散文里（写错不报错，最危险）**：

| 词表 | 现状 | 消费链 | 漏了会怎样 |
| --- | --- | --- | --- |
| 记忆 `type`（`user-preference`/`decision`/`convention`/`project-fact`） | `write_memory(type: str)`、`MemoryEntry.type: str`——**只有 docstring 与工具说明** | 模型 → `memory.append_entry` 落盘 → `read_memory` 展示 | **写错静默落盘成垃圾条目**，无人报警 |
| `NoteEntry.kind`（`web`/`extract`/`crawl`/`research`；docstring 还写了从没产出的 `command_output`） | `kind: str`；事实上的取值表是 `CompactNode._notes_kind` 的 dict | CompactNode → 注入 / `read_note` | 同上（只影响展示，危害小一档） |
| `error_type` 取值 | 一半由 `guard._type_token` 从异常类名派生，一半**手写字面量**（`file_io` 的 `io_error`/`invalid_argument`、`web_search` 的 `upstream_error`/`no_results`、`nodes` 的 `tool_error`/`unknown_tool`） | 各 server / ToolNode → `format_tool_result` 的 `[前缀]` 约定 + `CompactNode._FAIL_PREFIXES` 回退表 | **没有取值清单**，谁都能造新值；回退表已漂过一次（`[config_error]` 从来匹配不上） |
| `worker_id` 形态（`main` / `worker-xxxxxxxx`） | `LOCAL_WORKER_ID` 单点 ✅，构造在 `sub_agent` | 基座 / 面板渲染 | 轻 |

**D · 名字清单的副本（"枚举 = 一组名字"）**：`source` 标签（`tool.json` 是登记处，代码里 **5 处**副本：
`ORCHESTRATE_SOURCES` / `ASK_SOURCE` / `tools._WORKSPACE_SOURCES` / `_WEB_SOURCES` /
`skills._skill_server_name` 的 `f"skills/{dir}"`）；`CompactNode.ARCHIVE_TOOLS`（4 个联网工具名，与
tool.json 的 `source: mcp_service/web_search` 同义）；`_EXTRA_SLICE_KEYS`（`state.py` 字段名的副本）；
`_FAIL_PREFIXES`（C 组副本）；`evaluation.DEFAULT_DENY_TOOLS`（评估自己的，可接受）。

**第一批已落地（2026-09-22）**：

- **删悬空**：`NoteEntry.kind`（只写不读，连带 `CompactNode._notes_kind` 与其"联网四件套工具名"
  的第二份副本）——判据"生产了却没人按它拆解 → 删"，同当年 `topic/tags`。
- **删字典键常量**：`STAMP_ERROR_TYPE` / `STAMP_DECISION` / `STAMP_DENIED`——字典的键用变量承接与
  写字面量没有区别（用户拍板）；保留真正有价值的 `_tool_stamp()` 构造器（保证两个生产端同形）。
- **多处使用的词表进 schema**：`AskSelectMode` / `ASK_SELECT_ONE` / `ASK_SELECT_MANY` /
  `ASK_SELECT_DEFAULT` / `ASK_SELECT_MODES` 落 `app/schema/agent_schema.py`；六个消费点（gates 校验、
  tools 签名、approvals 透传、tools 回执渲染、panels、tui）全部改 import，`"one"/"many"` 字面量清零。
- **半悬空 → 给可引用常量**：`DECISION_APPROVAL` / `DECISION_ANSWER`（`turn.py` 的分派、`approvals`
  与两个前端造 Decision 处）、`STATUS_PENDING` / `STATUS_DECIDED`（主侧写、**worker 跨进程读**）；
  并在 `approval_schema.py` 一处写明 **`GATE_*` ↔ `DECISION_*` 两套词的映射**（此前只靠
  `decision_to_resume` 的 if 隐含）。
- **配置键的取值域跟键走**：`_THINKING_TYPES` 从 `nodes.py` 挪进 `app/config.py::THINKING_MODES`。

**仍未做**：

1. `source` 标签的 5 处副本（`ORCHESTRATE_SOURCES` 应改为从 tools.py 里既有的五组工具列表派生，
   再加一条契约测试与 `tool.json` 对齐）——D 组主体；
2. `CompactNode.ARCHIVE_TOOLS` 与 `tool.json` 的 `source: mcp_service/web_search` 同义（"谁进 notes"
   ≡ "谁是联网四件套"）；
3. `_EXTRA_SLICE_KEYS`（`state.py` 字段名的副本）；
4. `LLMNode.format_plan_status` 的 `status_map` 手抄三种状态（A 组例外）。

****原始建议（留档）****：

1. 给 `记忆 type` / `NoteEntry.kind` / `select` 加 `Literal`，校验表用 `get_args` 派生（照
   `PLAN_STATUSES` 的先例）——把 C 组的两处"写错静默落盘"变成当场报错；
2. 把 `ORCHESTRATE_SOURCES` 的来源改成 tools.py 里**既有的五组工具列表**（`orchestrate_tool` /
   `note_tools` / `ask_tool` / `memory_tool` / `skill_tool` 的分组本身就是"source → 工具"的真相），
   再用一条契约测试把 `tool.json` 的 `source` 与它对齐——消掉 D 组的 5 处副本；
3. 闸门那两套词（`kind` ↔ `GATE_*`）在 schema 里写明映射常数，别再只靠 `decision_to_resume` 的 if 隐含。

**相关背景**：`docs/ARCHITECTURE.md` §4.6（判据）、§4（tool.json 的键语义与"表与解析器同住"那条纪律——
注意 `ORCHESTRATE_SOURCES` 属**分流规则**，仍留代码，只是它的**词表**该与数据侧对齐）。

## [ ] 散落的数值口径：归属清单（2026-09-22）

**规则（2026-09-22 定）**：每个散落的数值常量都要能回答四问——**它是谁的**（owner 模块）/
**谁读它** / **可不可调** / **跟谁同族**。答不上来就说明它没主。配套两条：**同组合并**（同族的
值只留一个出处）、**多处调用 → 进 `app/config.py`**（config 是全局权威来源，见 §4.3 的两类内容）。
命名与可见性：**跨模块读 → 公开名；只服务本模块 → `_` 前缀**；**名字要带单位/量词**（字符数 vs 条数）。

| 常量 | 归属（owner） | 消费者 | 可调缝 | 同族/约束 |
| --- | --- | --- | --- | --- |
| `TEXT_BUDGET_CHARS = 8000` | **`app/config.py`**（同组合并后的**唯一出处**） | `memory.MEMORY_INJECT_CAP` / `profile.AGENT_MD_INJECT_CAP` / `skills.SKILL_BODY_BUDGET` / `nodes._NOTE_MAX`（各自名字保留——语义不同） | 四个 `cap=` / 构造参量 | 同族自身（此前 8000 抄四遍、靠注释互指"同量级"） |
| `_CONTEXT_BUDGET_DEFAULT = 60000` | `nodes.CompactNode` | CompactNode | `CompactNode(content_budget_chars=)` | — |
| `SKILL_CATALOG_CAP = 60` | `resource/skills.py` | `catalog_block` | 参量 `cap` | — |
| `MAX_ASK_OPTIONS = 5` | `agent/gates.py` | `ask_request` 校验（提示词文案里也有"5"，属文案） | — | — |
| `_MAX_IMAGES_PER_MESSAGE` / `_MAX_IMAGE_BYTES` | `resource/images.py` | `attach_images_to_payload` | — | 同族（一条消息的图） |
| `_MAX_STEPS = 100` | `mcp_service/sub_agent.py` | worker 图 `recursion_limit` | 调用处 | — |
| `_APPROVAL_TIMEOUT_S` / `_HTTP_TIMEOUT_S` | `sub_agent` | `_await_main_decision` / httpx | 函数参量 | **约束**：HTTP 超时必须 > `INBOX_BLOCK_SECONDS`（这条曾真出过 bug） |
| `INBOX_BLOCK_SECONDS = 120` | **`app/schema`**（主侧与 worker 两侧共用） | 主侧长轮询 + worker 客户端 | — | 跨进程 → 归 schema（与 `DEFAULT_INBOX_PORT` 同理） |
| `_PREFLIGHT_TIMEOUT` / `_STARTUP_PROBE_TIMEOUT` / `_STARTUP_PROBE_CHARS` | `agent/tools.py`（技能加载组） | 体检 / 启动探针 | — | 同族（技能加载的超时与截断） |
| `_SUMMARY_CHARS = 60` | `platform/commands/session.py` | `_short_line` | 参量 `limit` | **不是**与 `DEFAULT_SUMMARIZE_LIMIT` 同类（字符数 vs 条数）——2026-09-22 改名消歧 |
| `DEFAULT_SUMMARIZE_LIMIT = 10` | 同上 | `list_sessions` | 参量 `summarize_limit` | 同上 |
| `_TIMEOUT_SECONDS` / `_MAX_CHARS` | `skills/github/server.py` | 该技能自己的 HTTP | — | 技能自带（跟技能目录走 ✓） |
| 工具签名里的默认值（`run_command(timeout=300)`、`startup_wait=5` …） | 各 MCP server 的工具定义 | **模型**（schema 可见） | 调用参数 | **不是配置**：属工具契约，随服务端工具定义留原处 |

**本轮已做**：`8000` 同组合并进 config（`TEXT_BUDGET_CHARS`）；`_SUMMARY_LIMIT` → `_SUMMARY_CHARS`；
归属清单成形（本表）。**未做**：跨模块读的私有名（`nodes._EXTRA_SLICE_KEYS`）改公开名；`_CONTEXT_BUDGET_DEFAULT`
之外还有几个"可调缝"缺失（如 `MAX_ASK_OPTIONS` 没有参量口）。

**相关背景**：`docs/ARCHITECTURE.md` §4.3（config 的两类内容 + 进入判据）、本文件「配置的归属」条。

## [ ] 函数返回值：多类型 → 值对象（2026-09-22 审计）

**判据已落纸**（`docs/ARCHITECTURE.md` §4.5）：返回值只有两种合法形态——**单一类型**（含
`X | None`，那是"有没有"）与**数据结构体**；禁止"把互斥结果塞进元组"与"三元组以上的位置返回"。
配套约定：只有一句错误文案载荷的函数统一用 `str | None`（`None` = 通过）。
**列表同形的正例**：`ToolResult`、`parse_command → PromptCommand | ActionCommand`。

**实测清单（2026-09-22 grep `-> tuple[` + 多值 `return`；共 12 处）**：

| # | 位置 | 现在返回 | 判定 |
| --- | --- | --- | --- |
| 1 | `gates.ask_request` | `tuple[dict \| None, str \| None]` | **该改**：请求 ↔ 问题说明互斥 |
| 2 | `tools._probe_skill_credential` | `tuple[bool, str]` | **该改**：结果 + 原因 |
| 3 | `commands.session.resolve_session_id` | `tuple[str \| None, list[str]]` | **该改**：命中一个 ↔ 候选清单互斥 |
| 4 | `evaluation.fixtures.resolve` | `tuple[Fixture \| None, str \| None]` | **该改**：样本 ↔ 陈旧原因互斥 |
| 5 | `sub_agent._value_to_approval` | `tuple[str, dict, str]` | **该改（复用）**：主侧已有 `ApprovalRequest`（`app/platform/turn.py` 就在用），worker 侧却自己拼三元组 |
| 6 | `images.attach_images_to_payload` | `tuple[list, list[ImageRef]]` | 成对（请求体副本 + 记账）→ 值对象 |
| 7 | `CompactNode._plan` | `tuple[list, list[str], dict]` | **三元组** → 值对象（将来搬 `compact.py` 时顺手） |
| 8 | `CompactNode._tm_status` | `tuple[str, str]` | tag + 附注 → 值对象 |
| 9 | `CompactNode._new_note_ref` | `tuple[str, int]` | ref + 序号（最小） |
| 10 | `skills.{_split_frontmatter, _read_skill_md}` | `tuple[dict, str]` | frontmatter + 正文（两个函数同形，一起改） |
| 11 | `runtime.build_session_runtime` | `(connection, step, mailbox)`，**且无返回注解** | 三个运行时句柄 → 值对象（形状现在只活在 docstring 里） |
| 12 | `turn._race` / `loop._read_user_line` | `tuple[int, value]` | **刻意例外**：select 原语（编号 + 值），§4.5 已写明 |
| 13 | `evaluation/checks.py` 各 `check` | `tuple[bool, str]` | 与 #2 同形，一起统一 |
| 14 | 编排工具 state 切片 / `approvals._record_to_value` 的 UI 载荷 | `dict` | **刻意例外**：LangGraph 合并语义 / 跨边界协议形状，§4.5 已写明 |

**分批计划（用户 2026-09-22 决定：先记录，暂不动代码）**：

1. **第一批 = 互斥载荷那 5 处（#1–#5）**——最伤：读的人得靠顺序猜哪个是错误；#5 是**复用已有值对象**、
   几乎零成本。值对象落点：`AskCheck` / `ProbeOutcome` 这类只服务生产模块的留生产者模块
   （`frozen dataclass`）；worker 侧直接用 `app/schema` 的 `ApprovalRequest`（`TypedDict`，跨进程形状）。
2. **第二批 = 成组/三元组（#6–#11、#13）**——纯清爽化；#7 与 #11 分别顺手做（前者等 `compact.py`、
   后者补返回注解时就做）。
3. **#12 / #14 只写明例外**（§4.5 已写），不改。

**相关背景**：`docs/ARCHITECTURE.md` §4.5（判据与例外）、`app/schema/approval_schema.py`
（`ApprovalRequest` / `Decision` —— 值对象与"用结构体表达互斥"的既有范例）。

## [ ] 配置的归属：`config.py` 收什么（2026-09-22 审计）

**判据**（已写进 `docs/ARCHITECTURE.md` §4.3）：`config.py`（`Settings`）收"**随环境 / 部署 / 人变，
且改它不该动代码**"的值——凭证、端点、模型名、开关、技能白名单。凡"改了要重审行为、要跟测试与
文档"的，都是**口径**（预算 / 上限 / 超时 / 词法 / 协议标记），留代码 + 构造参量缝。
**子进程读 `os.environ` 是运输、不是配置**：真值在 `get_settings()`，由 host 经
`stdio_connection(extra_env)` / `skill_env` 注入。

**实测分布**（2026-09-22 全仓 grep `os.environ`）：

| 读取点 | 性质 | 判定 |
| --- | --- | --- |
| `app/platform/approvals.py:280` 读 `AGENT_INBOX_PORT` | **部署配置**（用户可换端口） | **缺口一，见下** |
| `app/platform/mcp.py::_inbox_env` 读 `AGENT_INBOX_URL`（透传给 dispatch server） | 运行时**握手值**（收件箱起来后主侧写回 env） | 不是配置；读取点重复 3 处（+`sub_agent` / `dispatch`），可收口，登记即可 |
| `mcp_service/file_io.py`（import 期）+ `sub_agent` + `dispatch` 读 `WORKSPACE_PATH` | 子进程启动配置 / 图构造参量 | 不是配置（刻意不走 `.env`）；但**四处"取 env + resolve + 存在性校验 + 同文案报错"是真重复**（见本文件「减法审计遗留」条） |
| `mcp_service/web_search.py` / `skills/github/server.py` 读 `TAVILY_API_KEY` / `GITHUB_TOKEN` | 子进程侧运输 | ✓ 正确形态（值由 config 取出、经 env 注入） |
| `.env.example` 里的 `LANGCHAIN_*` / `LANGSMITH_*` | 第三方库自己读 `os.environ` | **写进 `.env` 不生效**，见缺口二 |

**缺口一：`AGENT_INBOX_PORT` 的承诺未兑现（真 bug 级）。—— ✅ 已修（2026-09-22）** `CLAUDE.md` 与 `docs/MULTI_AGENT.md`
都写着"主侧可 `AGENT_INBOX_PORT` 覆盖"，但它读的是 `os.environ`——而 `pydantic-settings` 读 `.env`
**不会**把值注入 `os.environ`（这条坑就写在 `config.py` 自己的 docstring 里）。**后果：用户按惯例
写进 `.env` 的端口被静默忽略，只有 shell 里 export 才生效。**
**修法（已落地）**：`AGENT_INBOX_PORT: int = DEFAULT_INBOX_PORT` 进 `Settings`（可选键、带默认——
默认值仍取自 `schema` 那份共用常量，主侧/worker 不会漂）；`ApprovalInboxServer` 改从 `get_settings()`
取，且**显式给了端口就不读配置**（保持 `ApprovalInboxServer(port=0)` 这类调用不需要 .env）。
走 Settings 后 **`.env` 与 shell env 都生效**（shell env 优先级更高）。
**实测两条**（2026-09-22）：① `Settings` 里 `int` 键写空值（`AGENT_INBOX_PORT=`）→ **ValidationError**，
所以 `.env.example` 里这一行是**注释掉的**（要用才取消注释），不是留空；② `.env` 里的键确实
**不会**进 `os.environ`（`CHAT_MODEL_NAME` / `TAVILY_API_KEY` / `LANGSMITH_PROJECT` 实测皆 False）。
**测试**：`tests/test_approval_inbox.py` 两条——`Settings.model_fields["AGENT_INBOX_PORT"].default`
钉"默认值 = 共用常量"（不实例化 Settings，该文件仍无需 .env）、`test_port_comes_from_settings_not_os_environ`
钉"真值只有 Settings 一个出口"（打桩 `get_settings`，并断言 `os.environ` 同名键不再被读）。

**缺口二：`LANGCHAIN_*` / `LANGSMITH_*` 写进 `.env` 同样不生效**——同一根因。两条路：
(a) 文档写明"**由 `os.environ` 读的键（`AGENT_*`、第三方库键）只认 shell env**"（最小代价）；
(b) 入口用 `dotenv_values` 把 `.env` 一次性灌进 `os.environ`（能一并修好所有第三方键；
代价是改变"哪些键在哪里可见"的心智模型——子进程 env 是替换制，不受影响）。
**2026-09-22 走了 (a) 的一半**：`README` 的 `.env` 小节、`.env.example` 的 LangSmith 段、
`CLAUDE.md` 都写明了这条（哪些键只认 shell env）；**(b) 仍未拍板**——真要做就是在 `app/main.py`
入口灌一次，届时把这段升级成机制。

**缺口三：`GITHUB_API_URL`（指向 GitHub Enterprise）不是漏了，是已知未做**——
`docs/SKILL_DESIGN.md` §13.5 末记着：技能 env 机制只有"声明了就必须非空"这一种，声明它反而会让
默认（官方 API）路径加载不上。要让用户真能指 GHE，得先给机制加"**可选键 + 默认值**"。在此之前它
是**测试缝**（E2E 用它把请求指到本地桩）。

**相关背景**：`docs/ARCHITECTURE.md` §4.3（判据）、`app/config.py`（`Settings` + `SKILL_ENV_WHITELIST`）、
本文件「减法审计遗留」条（`WORKSPACE_PATH` 的四处重复）。

## [ ] 运行中转向：用户消息队列（输入不等整轮结束才递进）（2026-09-19）

**场景**：用户给 agent 派了任务、指了 A 方向，agent 已开跑；跑到一半用户发现 A 不合意图、
想改 B——今天的现实是：**输入必须等整个图路由跑完才被写入**（现状见下），agent 把 A 一路
做完，仓库被写了一堆要删/回滚的东西，token 白烧一整段。目标 = 运行中敲的消息**进队列、
在最近的安全点递进图**，模型带着"用户改向 B"继续，而不是等整轮结束后才看到。

**现状**（三段都核过，2026-09-19）：

- 前端**已经在收**：stdin 单 reader 线程 + pump（`app/tui/input.py`）整轮不停，运行中敲的行
  就躺在 `_out_q` 里；面板出现前的预输入由 `TerminalUI._hold_pretyped` 挪进 `_held`
  （2026-09-15 审计），留给**下一个** turn——所以捕获这半已经在了，缺的是消费。
- 基座**不消费**：turn 在跑时主循环只 `_race(turn_done.wait, inbox.new_pending.wait)`
  （`app/platform/loop.py::run` 第 2 步），不读用户输入 → 这些行必然等到 turn 结束才被
  `read_line` 取走、起新 turn。
- 图内**没有承接通道**：state 没有"运行中插入的用户消息"切片，`LLMNode` 没有对应注入点；
  interrupt 的 resume 载荷只运闸门决定（审批的 `approved` / 提问的回答）。

**要点（2026-09-19 对齐后细化；库行为已按 langgraph 1.1.10 探针实测——一次性脚本
`tmp/probe_astream.py`（tmp/ 不入库），结论以本条为准）**：

- **封闭性只对入向成立**：出向库有现成通道、我们没用——`drive_turn` 现在是裸
  `await compiled.ainvoke` 拿终态（`app/platform/turn.py`），每个 superstep 发生了什么一概
  不可见。换 `astream(stream_mode="updates"|"custom")` 即可逐拍观察；`drive_turn` 的
  park/resume 循环形状不变，消费方式从"等终态"改"逐拍"。**这半与消息队列独立，可先行**——
  顺带为流式显示与「长回合止损」的活性观察铺路。**已实测**：interrupt 处流出
  `{"__interrupt__": (Interrupt(value,…),)}` 一拍后流**正常耗尽**（无异常、无残余拍）；
  resume = `astream(Command(resume=…), cfg)` 重进，续流含被中断节点**整节点重跑**的一拍 +
  余下节点，终态正确。
- **入向选型，定 (c)；形状定「转向 = 提前收口 + 新起一轮」（用户 2026-09-19 拍板，取代
  早先"消息并进本轮"的排水口形状）**：
  - (a) **interrupt/resume 顺带捎**（resume 载荷扩 user_messages）：只覆盖有闸门的轮，
    且把转向混进 `Decision` 值对象不干净——不选；
  - (b) **`update_state` 写运行中线程**：**已实测否决**——调用本身成功（返回新
    checkpoint_id），但注入**静默丢失**（终态不含它）：运行中的 superstep commit 按自己的
    内存视图落盘，外部写被穿透丢弃、全程无报错。最坏的失败形态，不选；
  - (c) **进程内信箱 + 提前收口（定案）**：langgraph 封闭的是 **API 层的 state 写入**，
    不是进程内对象——图与基座同进程同事件循环（探针 4 已验：运行中向图外队列投递，节点
    在 superstep 边界读得到）。**消息内容从头到尾不进图**：信箱由基座持有，图侧只读布尔
    信号；转向 = 当前轮在下一个 llm 边界提前收口，基座把信箱里的行当**下一轮输入**新起
    turn——模型经完全标准的输入路径（HumanMessage）看到转向，CONTEXT_ENGINEERING 的消息
    流转一律不动。收口时 `compact_node` 照常参与（超预算才动手），新 turn 轻装开局。
  - 备查：cancel + 从 checkpoint 续跑——None 输入的语义**已实测**：对停在 interrupt 的
    线程，`ainvoke/astream(None)` **原样回报 `__interrupt__`**（不推进、不吞闸门）；对已
    完成的线程是 no-op（0 拍 / 返回终态）。即 None 输入**不能当续跑通道**，续跑必须
    `Command(resume=…)`；"cancel 后带新消息重入"这条路本身仍悬而未测，留作备选。
- **图侧改动收敛到三点（比"三条条件边"的草图更小）**：
  1. `state.is_inject: bool`；`build_turn_state` 每轮置 False；
  2. **唯一 peek 点 = `llm_node` 入口**：信箱有货 → **跳过本次模型调用**、返回
     `{"is_inject": True}`（不产生新 AIMessage）→ 既有 `route_after_llm` 看到
     "末条非 AIMessage(tool_calls)" → 走既有 compact/END。**不需要新增任何条件边**——
     "模型这拍没说话"与"给了最终答复"在路由眼里同形，自然收口；
  3. 信箱接线：`runtime.py::build_session_runtime` 造信箱，图工厂与基座各持一份引用
     （探针 4 同款）；`llm_node` 加可选 mailbox 参量。
- **草图里的地雷（为什么不能在 llm 出口收口）**：若按"llm_node 条件边 → compact"，模型
  恰在本拍吐了 tool_calls 时会带着**悬空调用**进 END——历史里 AIMessage(tool_calls) 没有
  兑现的 ToolMessage，下一轮直接 API 400。入口**跳过**（而非出口拦截）天然没这个问题：
  跳过时不产生新调用，旧调用都已兑现。
- **转向时延上界 = 在途一批**：信号只在 llm 入口被读——正在跑的批次照常执行（已过人批）、
  orchestrate 切片照常合并，然后才收口。**刻意不做"在途批次入口丢弃"**：同样的悬空问题，
  要做就得 seal（给未兑现调用补 `[已收口未执行]` 回执，同 approval_denied 回灌的机制）——
  留作后续优化；默认"执行完再收口"，用户本就可以在面板上拒绝，拒绝回灌路径是现成的。
- **基座两端职责**：主循环第 2 步 `_race` 加第三路 `ui.read_line`——行在 `turn_done` 前
  到 → 投信箱 + 前端回执（"已收到，本回合将在边界收口"；`_held` 那句"已留给下一个回合"
  的提示语跟着分化）；turn 结束 → 信箱非空则取**第一行**当下一轮输入（其余留队，与既有
  "预输入一行一 turn"语义一致）。内容只在基座侧流动、图侧 peek 不消费——同一行既触发
  收口又成为下一轮输入，不丢不重。
- **收口回合的呈现（别漏）**：被收口的 turn 没有模型答复，`messages[-1]` 是 ToolMessage
  ——`loop.py` 现状会把它当 `TurnFinished` 正文打出来。基座须看 `is_inject` 翻成专门提示
  （"回合已按你的新消息收口"），别把工具输出当答复。
- **worker 是 no-op**：`llm_node` 被子图共用 → mailbox 是可选参量、worker 恒传 None；
  不设标记就永不触发（worker 图无 compact 也无妨）。
- **重放语义写明**：`llm_node` 入口读了图外可变状态（布尔），checkpoint 重放不再纯——
  本仓库不用 time travel，接受，注释留痕。
- **先验**：LangGraph Platform（server）对"run 运行中来新输入"的官方策略是
  `multitaskStrategy: enqueue/interrupt/rollback/reject`——需求普适的佐证；但 server 只能在
  **run 边界**处置（run 是它的黑盒），本设计在 **superstep 边界**（llm 入口）收口，
  粒度更细一档。
- **验证路径**：机制层假模型 + 真图（沿 `test_command_review_e2e` 的路子）：信箱有货时
  llm 跳过（该拍零模型调用）、在途批次已执行、`is_inject` 落终态、路由落 END；基座层沿
  `test_platform_loop`：运行中投行 → turn 提前结束 → 下一轮输入 = 该行。层 A 回放对转向
  是瞎的（回放模型不读输入），若动提示词照旧 `--live`。
- **范围**：只做主 agent——worker 短命、无用户交互，结构性不涉及。

**入向已落地（2026-09-19，出向 astream 化仍未做，本条目保持开着）**：

- **图侧三点**：`state.is_inject`；`LLMNode` 入口 peek（`__call__` 第一条语句——收口拍零模型
  调用且**零读盘**，画像/记忆/技能都不碰）；mailbox 经 `get_main_agent_graph(mailbox=)` 参量
  接线，`build_session_runtime` 造信箱、基座与图各持一份引用（`UserMailbox` 落在
  `app/platform/runtime.py`：基座 put/take_first/clear，图侧只有 has_pending 的 peek 权）。
- **基座三件**：`_race` 第三路收行（**EOF 即撤下输入臂**——None 秒回会把 race 变忙轮询）；
  空闲分支**信箱优先消费**（`take_first` 当下一轮输入、其余留队，命令解析与退出词判定与
  正常输入共用同一段）；收口回合翻成 `Notice("回合已按你的新消息收口")`，不把 ToolMessage
  当 `TurnFinished` 正文。运行中回执措辞"**已收到，将在最近边界生效**"——刻意不承诺收口
  （最后一次 peek 之后到的行只会成为下一条消息）。
- **`build_turn_state` 每轮显式置 `is_inject: False`**：那是**上一轮**留在 checkpoint 里的
  收口痕迹，不归零下一轮一进来就又跳过。
- **实测钉住的刻意行为**：连敲 N 行 = 前 N-1 轮空转收口、只有最后一行真进模型（所有行都在
  历史里，模型一并看到）；在途 `ask_user` 照常弹出、回答照常兑现，然后才收口。
- **切会话**：旧信箱随运行体作废；正常流程下到不了"有残留"（空闲分支优先消费），防御分支
  丢弃残留行并 `Notice` 明说数量。
- **验证**：`tests/test_steering.py`（3 例真图：零模型调用+零读盘 / 消息尾部形状=ToolMessage /
  在途 ask_user 序列 / mailbox=None 行为不变）+ `tests/test_platform_loop.py` 新 3 例
  （race 投递与续轮 / 两行连锁空转 / 切会话丢弃）。全量 **562 passed / 7 skipped**；
  层 A 回放 **8/8、退出码 0**。

**相关背景**：`app/tui/ui.py` 的 `_held`/`_hold_pretyped`（预输入那半已经在了）、
`app/platform/turn.py::drive_turn`（astream 化 + park/resume 都在这里改）、
docs/CONTEXT_ENGINEERING.md（消息流转改动前必读）、docs/ARCHITECTURE.md §4（图内 vs 基座）、
文末「相关但未立项」的**长回合止损**（取消 = 扔掉整轮，队列 = 带着上下文转向，互补不互替）。

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

## [ ] 工具面缺口：一次真实运行的实测（2026-09-13）

**样本**：一个会话、3 轮用户请求（克隆 Luna-Agent → uv 补环境 → 查/拉分支），共 **55 次**工具调用。
下面的 `git_*` 计数是**当时的形态**——那批工具 2026-09-15 已删，保留原样是为了让结论可复核。

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
   GitHub 平台操作）。（**数字是当日快照**：七天后的 2026-09-14 三期已长到 **14 只读 + 3 写**，
   见 `docs/SKILL_DESIGN.md` §13.10；"没有克隆/分支/拉取"这条**结论不变**——那族至今不在技能里。）

**收敛清单（原按日志频次排序）—— ❌ 已整条作废（2026-09-15）**：

下面这几条都是"给 git 原子工具补参数"，而 **git 专用工具已于 2026-09-15 整体删除**（`mcp_service/git.py`
连同 12 个工具；git 一律走 `run_command`）。这些诉求由 shell 天然满足：`--depth` / `--track` /
`ls-remote` / `--unshallow` 直接就是命令行。**不要再去给不存在的工具加参数。**
（原文留档，供理解当初的判断依据）

- ~~`git_clone` 加 `depth`（浅克隆；日志里它直接用了 `--depth 1`）；~~
- ~~`git_switch` 支持"从远端建跟踪分支"（`--track` / `-B` / `-u`）；~~
- ~~**远端分支清单**（`ls-remote` 语义）独立成只读工具、或给 `git_branches` 一个开关；~~
- ~~`git_fetch` 支持 refspec / `--unshallow`。~~

**不要做**：给 `git_clone` 透传 `-c`——那等于给模型一个正式"关掉证书校验"的开关（MITM 面）。
（**该顾虑已由命令白名单承接**：`git -c …` 一律不判免审，见 `app/agent/command_policy.json`。）

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

- ~~**① 克隆落点**：`git_clone` 的 `dest` 改成**必填**~~ —— **已作废（2026-09-15）**：`git_clone`
  随 `mcp_service/git.py` 一起删了，克隆现在走 `run_command`，**工具面已不存在"落点参数"可改**。
  原来的两个诉求**都没接住**（2026-09-17 核）：审批面板看得见落点这件事，随工具一起消失
  （`run_command` 的命令行本来就整条可见，勉强算兑现）；而"工作区空就别套一层子目录"这条
  **提示词里没有**——`prompt.py:130` 只有一句"**clone 的目标目录要落在工作区内**"，管的是
  "别越界"，对"落根还是落子目录"**没有口径**。所以 ① 现在要么在 prompt 里补一句，
  要么只能靠 ② 的机制（后者不依赖模型自觉）。
- **② 更根本**：空工作区**默认**落根（零操作，但面板看不到落点）；或加 `/workspace <path>` **切换
  工作区**（工作区是"项目容器"时用它；要动基座：关旧运行体 → 换 `ws_key` → 重建图与库），后者顺带
  解决"TUI 起在了错的目录"。**这条是现在唯一还成立的解法**——克隆走 shell 之后，②不再有替代品。

**副作用（记在案）**：工作区=项目根时，`/init` 写的 `AGENT.md` 会落进**克隆下来的那个仓库**，成为
它的未跟踪文件。这跟"在一个项目里起 agent"的常态一样（想不提交就 gitignore），只是值得知道。

## [ ] 长期记忆分层化：设备环境层 → 用户层 → 工作区层

**动机**（2026-09-16 记）：agent 目前**没有任何地方知道"这台机器上有什么"**。要判断"能不能直接跑
`uv sync`""本机有没有 java""该敲 `python` 还是 `py`"，它只能**现场探**——而每一次探测都是一条
`run_command`，要么过闸门、要么走免审白名单，哪条路都有成本。这正是「工具面缺口」那次实测里
`run_command` 占 55 次调用中 30 次的一部分：真实会话里出现过"**一条复合命令
（`uv --version; python --version; git --version`）批准执行之后，模型又把三条分别重跑了一遍**
——4 次审批换一份信息"，以及用户侧的体感"克隆下来项目、**补环境老是补不全**"。

而这些事实的特点是：**慢变**（几个月不动）、**跨工作区通用**（同一台机器上每个工作区都成立）、
**体积极小**（十几行）。今天的长期记忆却只有**一层**——`resource/<ws_key>/memory/memory.md`，
按工作区私有。于是同一份"本机 python 3.13.9、uv 是 `/usr/sbin/uv`（别 `uv run`）、没有 java"
会在**每个新工作区里被重新学一遍**，或者干脆没学会、每次再探一遍。

**目标态**：长期记忆分成若干层，**越靠上的层越通用、越稳定、注入优先级越高**；最后一层仍是
工作区的项目记忆（现状不变）。

```
① 设备/环境层   本机 python/uv/git/node/java 的版本与"有没有"、OS/shell、包管理器、
                代理与网络约束（"本机 github 被 Watt Toolkit 劫持到 127.0.0.1"就是这类事实）
② 用户层(待拍)   跨工作区的个人偏好：语言、提交习惯、输出风格
③ 工作区层      resource/<ws_key>/memory/memory.md   ← 现状，不动
④ 项目仓库层    工作区根 AGENT.md                     ← 现状；与 ③ 是**不同的轴**（随仓库走 vs 私有）
```

**待拍（实现前须钉死）**：

1. **中间层要不要**：①③ 是点名的两端。② 用户层要不要单列？今天的 `write_memory` 把
   `type=user-preference` 写在**工作区**记忆里，于是"注释用中文"这种偏好在每个工作区各写一份。
   **倾向要**，但得先定它和 ① 的分界——"本机没有 java"是环境事实，"用 python 不用 java"是用户
   偏好，可两者常常同源，别切成两份自相矛盾的东西。
2. **`AGENT.md` 算不算一层**：它是**另一个轴**（进仓库、随真值收敛）而非另一层（私有、跨会话）。
   文档里要写明这个区别，别让"分层"把它也吞进去。
3. **谁写 ①**（最关键，两条路差别很大）：
   - (a) **host 侧自动探测并播种**：像 `ensure_memory_template` 那样，在建会话时探一次
     （`python --version` / `git --version` / 各工具在不在），写进设备层文件；环境指纹
     （主机名 + 关键版本）变了就刷新。**优点**：零模型成本、零审批、永远是最新事实，正对
     "别让 agent 再探一遍"这个动机。**代价**：多一张"探什么"的表 + 起子进程，而探测命令在本仓库
     有前科（terminal 的 stdin 继承坑，见本文那条 `[x]`）。
   - (b) **agent 探到后自己写**：加 `write_memory(scope="machine", …)`。**优点**：不动 host、
     探测范围由模型按需决定。**代价**：探那一次照样过闸门（动机只兑现一半），且得先"想到要记"。
   - (c) **两者都要**：host 探**客观版本**，agent 记**主观结论**（"这个仓库的测试得用
     `-m pytest`"这类留在 ③）。
4. **注入预算怎么分**：现在 `MEMORY_INJECT_CAP = 8000` 是**单份**上限。多层后是"每层各自 cap"，
   还是"合计一个 cap、按层优先级分配"？**倾向后者**：越靠上的层越该**整份**注入（它小且通用），
   ③ 才是需要"从最新往前装、装不下退化成编号清单"的那一层。等于把现有的截断策略**按层分别定义**。
5. **工具面怎么指定层**：`write_memory` / `read_memory` 现在靠 `InjectedWorkspace` 定位——**模型
   碰不到路径**，这是既有的安全不变量。分层后模型要能说"写哪层"：加 `scope` 参数（枚举，不是
   路径，不破坏那条不变量），还是"默认 ③、① 另有工具"？`read_memory` 取回时要不要标出条目来自
   哪一层？
6. **陈旧与失效**（分层真正的难点）：越靠上的层影响面越大——**一句错的"本机有 java"会污染所有
   工作区**。设备层不能只靠"人记得改"：环境指纹写进层头、不匹配就自动作废重探？还是只标
   "记于 2026-09-16"、注入时提示模型"这是快照，可能过期"？**这条决定了 ① 能不能自动写**：
   (a) 之所以敢自动写，前提就是有指纹兜底。
7. **落点**：① 放哪？`resource/` 现在的语义是"按工作区分区"，设备层**不属于任何工作区**——
   是 `resource/_machine/memory.md` 这类兄弟目录，还是挪出 `resource/`（如项目根 `.theta/`）？
   定它时连带定 ② 的落点。
8. **worker 吃不吃 ①**：现状 worker **不注入任何记忆**（`inject_session_context=False`）。可 ①
   对 worker 恰好最有用——它要跑命令、又只有自己那份上下文（还看不到主 agent 的对话）。给不给？
   （worker 的隔离是**设计原则**不是图省事，所以要显式拍板。）

**相关背景**：`docs/LONG_TERM_MEMORY.md`（§1 三条通道的边界表、§3 单文件格式与编号规则、§4 注入
与 cap、§5 工具与路径安全不变量、§8 分期）——**注意 §8 的 Phase C 把"跨工作区共享记忆"与
"分层摘要"列为"明确不做、后续再议"，本条正是把那条提前**；动手前先读该文，别与它的单文件格式 /
`m1,m2,…` 编号 / "只存工作区里重取不到的结论"这三条既有纪律打架。代码落点：`app/resource/memory.py`
（读写单点）、`app/resource/paths.py`（落点单点）、`app/agent/nodes.py::LLMNode`（注入点，
`memory_block` 在 `app/resource/memory.py`、`agent_md_block` 在 `app/resource/profile.py`）、
`app/agent/tools.py`（`write_memory`/`read_memory` 与
`InjectedWorkspace`）。证据见本文「工具面缺口」与「工作区语义」两条。

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

## [~] 技能（skill）：按需加载的"领域包"——**三期均已落地**（见 docs/SKILL_DESIGN.md）

> **设计已独立成文** → [[docs/SKILL_DESIGN]]。本条只留指针与当前状态，**别再往这里堆内容**。
> ⚠️ 下面按日期追加，**读最新一条**（`docs/README.md` 的写作约定）——中间那几段标着"当前状态
> （2026-09-12）"的是当日快照，其中"`skills/github/` 没有 `server.py`、当前是知识型"**已被三期取代**。

一句话：把某个领域要用的**工具**和这个领域的**纪律**打成一包、按需加载。典型 = **GitHub**（推送 /
PR / issue / 搜索 / 不克隆就读远端代码，外加"先开分支""master 不能随便提"等约束）。差异化在
**约束三分法**——软约束走 `SKILL.md` 正文（host 读取、注入系统提示）、硬闸门走 `tool.json`、
结构性拒绝写死在工具里。

**当前状态（2026-09-12）**：**两型共用的那条通道已落地**——`app/resource/skills.py` +
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
**14 只读 + 3 写**（`github_pr_create` / `github_pr_review` / `github_issue_comment`，都要人批）。
越权动作在工具内**结构性拒绝**：`github_pr_review` 的 `event` 白名单不含 `APPROVE`；**"合并"连工具
都没有**（"合并权留给人"最彻底的落点是这个动作压根不存在，与"APPROVE 被拒"同构）。同批修掉了
`server.py` 的 8 处审计问题与 `skills.py` 两处真 bug（坏编码的 `SKILL.md` 会拖垮整次扫描；目录名是
Python 关键字时被误标 `[带工具]`）。

**未做**：技能运行体的空闲回收（已定不做，只靠 `drop_skill`）；`/skills` 控制面命令。口径见 §13.9。

**与本文其它条目的关系**：SKILL_DESIGN §6 与上面的「反思节点」打通——skill fork 到隔离子 agent 执行，
本身即一次结构性反思，且 `mcp_service/sub_agent.py` 那套骨架已经在了；但它卡在
`docs/MULTI_AGENT.md` §6「子 agent 写权限下放」这个里程碑上。

## [ ] 图像输入：@路径 附图通道已落地；工具返回图仍无通路（2026-09-14）

**用户通路已落地（本期）**：消息里写 `@路径`（相对工作区或绝对路径；带空格用 `@"…"`），
`LLMNode` 构造请求体副本时把图**临时**附上——**base64 从不进 messages/checkpoint**：state 里的
消息始终是带 @路径 的纯文本（本身就是"看过哪张图"的日志），轮末没有"剔除"这回事。机制收在
`app/resource/images.py::attach_images_to_payload`（解析/读盘/拼接/记账；2026-09-21 从 `LLMNode`
的静态方法剥到资源层，节点只剩"这一拍要不要附图"）；
记账 = state 的 `attached_images`（`ImageRef` 元数据，单调追加）→ 同轮不重发（**首次-only**，
重启后重读盘即得）。宽进策略（议定）：扩展名不像图片的 @token 是普通文本、原样放行；失败不炸
turn，处置（未找到 / 过大>5MB / 超 4 张 / 读取失败）写进正文让模型转告用户。worker
`attach_images=False`：dispatch prompt 不认 @，杜绝模型借 worker 附带本地文件（结构性排除）。
机制测试 `tests/test_image_attach.py`（13 例无网络；真调用 opt-in，见该文件尾）。

**模型层事实**（此前已通，`tests/test_vision_input.py` 固化）：

| 路径 | 工具绑定 | 结果 |
| --- | --- | --- |
| base64（LangChain 规范块 `{"type":"image","base64":…}`） | 无 | ✅ 认出一张左红右蓝的 64×64 图 |
| base64（`image_url` + data URI） | **有**（`bind_tools`） | ✅ 答对颜色 + 吐出规范 `tool_calls` |
| http URL（`image_url`） | 无 | ✅ 认出百度 logo（**厂端自己抓的图**） |

三条结论：

- **唯一前提是模型**：当日配置的 `glm-4.7-flash` 硬拒图片（`400 / code 1210 /
  messages.content.type 参数非法，取值范围 ['text']`）；实测可用的是 `glm-4.6v-flash`（base64 与
  URL 都行，**且能同时挂工具**）；免费层的 `glm-4v-flash` 只吃 URL、不吃 base64。
  **2026-09-15 复测：当前配置的 `glm-5.3-flash` 已支持视觉**（`tests/test_vision_input.py` 的
  live 用例三连过）——换模型后重跑一次，别照抄上面的历史结论。附图真调用曾持续 429（1305 访问量
  过大）——免费端点负载问题，非通道缺陷，限流缓解后跑 `-k apple` 即验。
- **库侧零障碍**：`ChatOpenAI` 原样透传 `image_url`，并把 LangChain 的规范图块**自动翻译**成
  `data:<mime>;base64,…`；`ToolMessage` 带图也被端点接受。
- **两条路各有代价**：base64 自包含（本地图直接给）但体积 +33%；URL 便宜但要求图在公网。
  用户通路选 base64（本地文件）；URL 留给公网图源。

**原勘察的拦路石，处置如下**（1/2/4 被通道设计整体绕开，3 未动）：

1. ~~预算与折叠对 base64 失控~~：base64 不进 messages，`needs_compact` 永远看不到它。
2. ~~块列表撞"按 str 消费"的老代码~~：state 消息保持 str，`TurnFinished` / `/list session` /
   `CompactNode._tm_status` 全部照旧。
3. **工具返回图的通路仍是断的（未动）**：`format_tool_result`/`_join_block_texts` 丢非文本块
   （`app/platform/tool_results.py:22-33`，**已被 `tests/test_format_tool_result.py:65-70` 钉住**），且
   `ToolResult.content: str`（`app/schema/agent_schema.py:9`）是硬墙——模型还不能把工具读到的图
   送进对话。要做时另开一期。
4. ~~一批测试会红~~：str 断言测试全部照旧（全量回归 401 passed）。

**顺带的独立缺陷已修（2026-09-14）**：`read_file` 读二进制时 `UnicodeDecodeError`（非 `OSError`
子类）曾冒到 `@guard` 被误判成 `internal_error`——现已改为字节读 + NUL 探测（`_is_binary`，
与 `search_content` 共享同一口径）+ 魔数猜类型，二进制/非 UTF-8 文本都给"它是什么 + 多大"的
可预期回执（success=True，不进错误分类）；行尾显式保持旧文本模式的 universal newlines 行为
（`\r\n`→`\n`），编辑锚定不受影响。`tests/test_file_io.py` 二进制回执一节共 5 例。

## [ ] 文档解析技能（skills/documents）：勘察完毕，暂缓（2026-09-14）

**结论**：值得做，但用户拍板 agent 提问模式（前面那条）效益更高、先做它——本条设计已收敛，
捡起即可开工，无需重新勘察。

**落点判定**：能力型技能 `skills/documents/`，不进 `mcp_service`（§8.1 并列不混入）。"不做、
让模型走 `run_command` + 临时脚本"能顶：但每次读都要审批、依赖装没装不可控、表格还原靠模型
肉眼拼、"怎么读 docx"的知识散在提示词里。技能侧的额外意义：这会是 `requirements` 通道
（`skill.json` 声明 + host `find_spec` 起进程前预检 + 未装 fail-closed 拒载）的**第一个真实消费者**
——github 是纯 stdlib，没走过这条路。

**依赖成本（与 github 技能的本质区别）**：没法纯 stdlib，主 venv 装 3–4 个纯 Python 包——
`pdfminer.six`（PDF；MIT、CJK 支持尚可）/ `openpyxl`（xlsx）/ `python-docx` / `python-pptx`。
全纯 Python、无编译依赖；§9 已决依赖进主 venv，机制零新增。

**范围（已议定）**：

- 一期 **xlsx + pdf**（编码场景最高频）：`xlsx_read`（工作表 → markdown 表格，限行列防爆炸）、
  `pdf_read`（按页 range 分段，回执带页码）；二期 docx + pptx。
- **只读不写**：生成 docx/pdf 体大频低，转换类任务用文本工具输出 markdown 就够。
- **扫描件 PDF 不做 OCR**：提取不到文本层就如实回执"疑似扫描件（无文本层）"，OCR 另立项。
- `need_review: false`（纯读本地）+ 路径过工作区沙箱（对齐 file_io）+ 损坏文件给可预期回执
  （read_file 2026-09-14 口径）。

**待拍**：真实频次（几个月遇不到一次的话，继续搁置就是对的）；命名 `documents`（倾向，一个
技能装四族、共享沙箱与渲染）还是按 pdf / office 拆。

## [ ] 减法审计遗留：低危清理项（2026-09-15）

**背景**：2026-09-15 对全仓做了一轮"减法审计"（逻辑冗余 / 死代码 / 逻辑错误 / 过时注释），
高危与中危已就地修掉（见下"已清"），剩下这批是**不承重的零碎**——每条都确证过行号，但单独
不值得开一轮，做别的改动顺路带上即可。**不必再全仓扫一遍**：已清的部分不会复现，下面这些
是**仅剩**的清单。

**已清（同批，供对照，避免重复排查）**：

- 高危 4 条：`@图片` 引号分支 KeyError（`Path(raw).suffix` 反查 mime，用户写 `@"x.png "` 即崩
  整轮）、`process_read` 吞掉已结束进程的输出（先 `read_new` 消耗再 `drain`）、`edit_file` 读
  GBK/二进制被误判 `internal_error`、技能体检漏捕**读阶段**的裸 `OSError`（逃出 `get_skill`）；
- 中危 7 条：`glob` 输出基准与 `read_file` 不一致、`copy_path(recursive=False)` 对目录假成功、
  `get_directory_tree` 的符号链接逃逸、`/list session` 把 UTC 的 checkpoint `ts` 当本地、
  `git_commit` 的 `Co-authored-by` 子串误判（他人的 trailer 会让 Theta 尾注被跳过）、
  `prompt.py` 漏登记 `git_push`/`git_clone`、`_register_failed` 跨会话串味（改为按
  `(会话, 名字)` 记账）；
- 文档漂移：`README.md` / `app/main.py` / `docs/EXCEPTION_DESIGN.md` / 3 个测试 docstring
  **共 5 个文件 11 处**的 `uv run`（会另建/重同步 Linux venv、破坏现有环境）、GitHub 工具
  计数统一到 **14 只读 + 3 写 = 17 条**、`SKILL.md` 的 PR 流程与示例文档冲突 + `git_pull`
  参数顺序写错、`Phase B 预留` / `尚未接入` 一类失效陈述；
- 删除：`_MAGIC_PREFIXES` 魔数表（file_io + github 两份）——它只产出显示标签、无分支依赖，
  且回执里本就回显 path（后缀在内），参见 `mcp_service/file_io.py:78` 的留痕注释；
  `tool.json` 的 `safe_tool`/`risky_tool`、`NoteEntry.topic/tags`、`auto_approve`/`auto_reject`、
  `InterruptRecord`（三个字段无人读，改为 `EvalResult.interrupt_count`）、terminal 的 6 处
  只写不读的进程状态、`graph.py` 的 `from app.agent.nodes import *`（连带 31 个无关名灌进建图
  模块的命名空间）、9 处未使用 import。

**2026-09-17 第二批已清**（同属低危，顺手做掉）：逐条改动与验证搬到了独立条目
「低危清理第二批 + 评估 CLI 退出码（2026-09-17）」，**本节只留"还剩什么没做"**（见下面「遗留」）。

**同时作废（引用的代码已删/早已修，不必再做）**：

- ~~`app/schema/__init__.py` 漏导 `ImageRef` / `SkillPreflight`~~——**早已导出**（`__init__.py:4,10`）；
- ~~`QueueNode` 空 docstring + 空 `__init__`~~——docstring 已写全（`nodes.py:1014`），且它本就没有 `__init__`；
- ~~`mcp_service/git.py:110` 的 `_get_repos()` 双扫~~——`git.py` 已于 2026-09-15 整体删除；
- ~~`file_io._resolve_path` ↔ `git._resolve_repo_path` 逐字同构~~——只剩下面那条 `file_io` /
  `mcp.py::_validate_workspace` 的两份近亲。

**遗留（均为低危）**：

- **只被测试用的生产函数**：`app/resource/skills.py::read_body`——生产路径走 `get_meta`/`body_of`，
  只有 `tests/test_skills.py` 在用；而同文件 docstring:24 把"host 侧只读 SKILL.md、不拿模型
  给的名字拼路径"这条安全不变量的落点指成了它（真正落点是 `get_meta`）。删需同步改
  `tests/test_skills.py` 里 4 条用例的调用。
- **自我标注的 YAGNI 字段**：`app/schema/agent_schema.py::MCPToolSpec.metadata`（注释自述
  "当前无消费者，保留以备将来"）。留就换成具体计划，删就顺带清 `app/platform/mcp.py` 两处透传。
- **跨文件重复**：`WORKSPACE_PATH` 的"取 env + resolve + 存在性校验 + **同文案** `ConfigError`"
  在 `mcp_service/file_io.py`（import 期校验）与 `app/platform/mcp.py::_validate_workspace`（构图期）
  各一份。抽到 `mcp_service/utils.py` 前先想清楚：file_io 那份是 **import 期**触发的，搬过去会
  把"import 即校验"也一起搬走——要么只抽纯函数（校验时机留在各调用点），要么显式拍板改时机。

**2026-09-22 追加（路径名已收束，这条是剩下的那一半）**：路径的资源清单、基准与落点**已经**全部
收进 `app/resource/paths.py`（规定见 docs/ARCHITECTURE.md §4.4；`Path(__file__)` 现在只许出现在
白名单里，`tests/test_resource.py` 末尾有用例守着）。**没做的是"工作区是从哪来的 + 校验"**——
现在有 5 个形近函数：`app/agent/graph.py::_resolve_workspace`（参量 / 默认 tmp）、
`app/platform/mcp.py::{_normalize_workspace, _validate_workspace}`（归一 / 构图期校验）、
`mcp_service/{sub_agent,dispatch}::_resolve_workspace`（env）、`app/tui/runner.py::resolve_tui_workspace`
（cwd）、`mcp_service/file_io.py`（import 期 env）。**它们语义真的各不相同**（来源分别是参量 / cwd /
env，时机分别是 import 期 / 构图期 / 调用期），所以不能一刀切合并——可共用的只有"归一化 +
存在性校验 + 同文案报错"那一段，且 file_io 那份的**时机**（import 即校验）搬走就会变。真要动就
按上面那条的原则来：**只抽纯函数，校验时机留各调用点**。
- **小冗余**：`mcp_service/file_io.py:699-700` 的 `ext_set` 两次赋值可合一
  （`(e if e.startswith(".") else f".{e}").lower()` 一次到位）。
- **远端 GBK**：`skills/github/server.py::github_file_read` 只做了二进制那半防线（NUL 探测），
  GBK 文本仍会 `errors="replace"` 成一片替换字符喂给模型——本地 `read_file` 2026-09-14 已补
  第二半，这里是漏掉的孪生。

**待拍（一条）**：`app/agent/tools.py:872` 的 `meta.name != meta.dir_name` 分支**生产不可达**
（扫盘期已保证能力型两者相等），但 `tests/test_skill_runtime.py:467` 自述是"安全网…即便拿到
构造出来的 meta，加载期也必须拒绝"。二选一：① 删掉该分支 + 同步删那条用例；② 按安全网保留
——保留的话请在注释里点明它不可达，并修掉 `app/resource/skills.py:174` 那处把校验位置指向
"get_skill 的校验"的指针（实际执行处是**扫描期**）。

## [ ] 评估框架的已知缺口（2026-09-16 记，09-17 更新）

**背景**：2026-09-16 给 `evaluation/` 补了**层 A 录制回放**（零 token，拿真实录制的模型输出当
固定输入样本，断言"除模型之外的一切"没变）与**闸门策略能力**（`deny_all` / `scripted` /
`allow_except(answers=…)`），默认命令从此不花钱。用法与两条层的分工见 README「模型行为评估」。
下面的事当时**显式没做**（2026-09-17 又核出"C 仓库里还没有 CI"一条），记在这里免得下次重新勘察：

- **任务集与断言覆盖面**：示例任务只有 3 个（读 / 写 / 计划），断言工厂只有 6 个。未覆盖：拒绝
  路径之后模型怎么办、`ask_user` 提问闸门、长期记忆写入、技能加载、`dispatch_subtasks`、compact
  触发。**策略侧已经能表达了**（`deny_all()` 测拒绝、"`allow_except(answers=[…])`" 答提问），
  缺的只是任务与断言本身。
- **量化与闸门**：`EvalResult` 只记次数与耗时，**不记 token**（要从 `AIMessage.response_metadata`
  聚合）。~~`__main__` 没有退出码，进不了 CI~~ **[已落地 2026-09-17]**：退出码三档
  （`0` 通过 / `1` 不通过 / `2` 用法错误），判据与理由记在独立条目
  「低危清理第二批 + 评估 CLI 退出码（2026-09-17）」。**仍缺两件**：① 上面这条 token 聚合；
  ② **基线快照对比（本次 vs 上次）**——退出码能拦"变红"，还拦不住"悄悄变慢、审批变多"。
- **仓库里还没有 CI**（2026-09-17 核）：**有了退出码 ≠ 接进了 CI**——全仓一个 CI 配置文件都没有
  （既无 `.github/` 也无 `.gitlab-ci.yml`）。真要把层 A 当回归闸门，缺的是那条流水线本身
  （跑 `python -m evaluation` 吃它的退出码；顺带跑 `-m pytest` 的 528 例——**注意测试那两条前提**：
  必须 `python -m pytest`、且 `WORKSPACE_PATH` 要经 `WSLENV` 传对，见 README / CLAUDE.md）。
- **按工作区复用编译图**：每任务建一次图、各拉一组 MCP 子进程（`runner.py` 模块 docstring 的 TODO）。
- **evaluation/ 自己的测试仍不完整**：同日补了 `tests/test_eval_{policy,fixtures,runner}.py`（策略
  语义 / fixture 读写与指纹 / SKIP 装配与清理顺序），2026-09-17 又补了 `tests/test_eval_report.py`
  （通过判据的三种红 + 用法错误退出码），但**真图回放**只有 `python -m evaluation` 手工跑
  ——没有人守着"回放这条路本身没坏"（这是最值得补的一环：它是唯一能守住
  "回放≠坏掉"的手段，且零成本）。

**顺带修掉的既有 bug（同日）**：`run_suite` 曾在返回前就 `rmtree` 掉临时工作区，而检查项要到报告
阶段才跑——于是**文件类断言恒失败**（`--keep-workspace` 时 8/8、默认 6/8）。清理已挪到
`cleanup_workspaces()`，由 CLI 在报告之后调。这正是"骨架没人真跑过"的直接后果，也是上面最后一条
值得补的理由。

## 相关但未立项（讨论过，待显式拍板再单列）

- 读策略太保守：`read_file` docstring 的"避免大文件整段塞满上下文"引导模型把 1280 行文件
  拆 ~200 行 × 多段读，徒增往返与重发成本；考虑改成"需要整份就整读、超大/只需探测才分段"
  + "相互独立的读取/检索尽量同回合并发"。
- ~~思考内容剥离~~：**2026-09-14 查明它在当前栈下是空转的**——`langchain-openai` 根本不提取
  `reasoning_content`（没有东西可剥），已改为由契约用例钉住库行为，见上面那条 `[x]` 条目。
- 长回合止损：run_tui 运行中响应 `q` 取消当前 turn；主 ainvoke 显式设 recursion_limit。
- 大体量广度探索的 dispatch 触发信号（docs/MULTI_AGENT §10"三模式如何被选中"）。
