# 会话样例：IndexTTS2 环境搭建（单轮长任务，2026-10-18）

> **性质**：真实会话的量化复盘，不是目标态设计。数据来自
> `resource/E-code-python-IndexTTS2_1732f309/sessions/f2158119aa0b4b26ae9ddc42295e347f/agent.db`
> （LangGraph checkpoint 库），采集与分析只读该文件，不改仓库。
> **一句话**：一个 42 分 44 秒、只有 1 条用户消息的回合，产生 44 次工具调用、19 次人工中断，
> 上下文涨到 139,380 字符；**62.2% 的墙钟里 agent 在等一条 `run_command` 结束**，而累计送入
> 模型的 146.9 万字符中有 71.3% 来自三份安装日志。它同时给「运行中 compact 未落地」
> 「工具输出无上限」「长任务阻塞整个回合」三条缺口提供了可复算的证据。
>
> **后续案例**：同一工作区在 2026-10-26/27 的多轮部署与 token 归因见
> [SESSION_CASE_INDEX_TTS2_2026-10-27.md](SESSION_CASE_INDEX_TTS2_2026-10-27.md)。

## 1. 为什么收录

这次会话不是"跑通了一个工具"的演示，而是一次**单轮长任务**的完整样本：

- 覆盖了图的主要路径：`llm → queue → review → orchestrate/tool → llm`、审批 `interrupt`、
  `ask_user` 提问闸门、`write_memory`、回合末 `compact_node`；
- 44 次工具调用里有 4 次失败并被模型自愈，能看清恢复成本；
- 上下文从 73 字符涨到 139,380 字符，且**全程没有发生折叠**——为 `docs/TODO.md`
  「运行中 compact」条目提供了比 2026-09-07 那次更极端的实测（那次是 ~67 次调用/20 分钟，
  这次是 44 次调用/42 分钟、单条消息 139k 字符）；
- 成本与闲置可量化：模型 glm-5.3-flash 全程花费 **0.47 元**（用户提供），而 42.7 分钟里
  **26.6 分钟（62.2%）** agent 阻塞在安装命令上；累计送入模型的字符是终态的 **10.6 倍**，
  其中三份安装日志占 **71.3%**——上下文压缩的收益比只看终态时大得多；
- 结果可核对：工作区、`resource/<ws_key>/memory/memory.md` 与 DB 终态一致。

## 2. 会话元数据

| 项 | 值 |
| --- | --- |
| 工作区 | `E:\code\python\IndexTTS2`（用户从该目录启动 TUI） |
| `ws_key` | `E-code-python-IndexTTS2_1732f309` |
| `thread_id` / `session_id` | `f2158119aa0b4b26ae9ddc42295e347f` |
| 起止（本地 UTC+8） | 2026-10-18 12:40:20 → 13:23:04 |
| 墙钟时长 | 2564.4 s = **42 分 44 秒** |
| 用户消息 | **1 条** |
| 消息构成 | 1 human / 23 AI / 44 Tool |
| 图 superstep | 120 个（step −1 … 118；step 119 为终态 checkpoint） |
| checkpoint / writes | 121 / 495 |
| DB 大小 | 9.7 MB |
| 模型 / 花费 | `glm-5.3-flash` / **0.47 元**（用户提供；DB 不含模型与计价信息） |
| 累计送入模型 | 23 次调用合计 **1,469,372 字符**（均值 63,886；为终态上下文的 10.6 倍） |
| 阻塞窗口 | 工具执行 1697.2 s，其中四条安装命令 **1596.2 s（62.2% 墙钟）agent 空闲** |
| 终态 | `pending_tool_calls=[]`、两个 `approved_*` 空、`ask_answers={}`、`current_plan=[]`、`notes` 未出现 |
| 结束方式 | 最后一条 AI 给出总结 → 预算超限路由到 `compact_node` → 空操作 → END |

任务原文：

> 帮我在本地克隆下来indexTTS2,并且补全所有的环境依赖，然后确保能够正确运行，环境使用conda进行管理，新建一个indextts2的环境名

## 3. 口径与方法

后续所有秒数、字符数都由 DB 直接解码得到，口径如下（复现脚本见 §12）：

1. **时间**：`checkpoint.ts` 是 superstep 的**起点**。相邻 checkpoint 的 `ts` 差 = 该
   superstep 的实际耗时；所有节点耗时之和与 2564.4 s 的墙钟误差 < 0.1 s。
2. **节点归属**：`writes` 表按 `checkpoint_id` 归档该 superstep 的输出，据此判定节点——
   `branch:to:queue_node` / `branch:to:compact_node` → `llm`；`ask_answers` → `queue`；
   `__interrupt__` → `review`（含人工等待）；其余 `branch:to:tool_node` → `review`；
   `messages + current_plan` → `orchestrate`；`messages + approved_tool_calls` → `tool`。
3. **状态可见性**：checkpoint N 的 `channel_values` 已包含 superstep N−1 的写入，故
   "AI 消息出现在 checkpoint N+1"是正常现象。
4. **存储**：checkpoint blob 内嵌**完整 `channel_values`**（含全部 `messages`），不是增量引用。
   本会话 121 个 checkpoint 的 blob 合计 9.64 MB，平均 79.7 KB、最大 185 KB——上下文越大，
   每个 superstep 的落盘成本越高（见 §7.3）。

## 4. 执行链

模型先建 6 步计划，随后按步骤推进；计划工具共 14 次调用（1 次 create、12 次 update、1 次 clear）。

| 计划步骤 | 实际动作 | 关键工具 | 结果 |
| --- | --- | --- | --- |
| 1. 查看工作区现状 / git | `list_dir(".")` 确认空目录；`git --version` | `list_dir`, `run_command` | 空目录；git 2.51.1 |
| 2. 检查 conda | `conda --version && conda info --envs` | `run_command` | conda 26.1.1；无同名环境 |
| 3. 克隆仓库 | `git clone https://github.com/index-tts/index-tts.git` | `run_command` | 成功，落到 `index-tts/` |
| 4. 新建环境 | `conda create -y -n indextts2 python=3.11` | `run_command` | Python 3.11.17（项目要求 `>=3.10,<3.12`） |
| 5. 安装依赖 | 先 cu128 源装 torch 2.8，再 `pip install -e .[webui]` | `run_command` | torch 2.8.0+cu128、gradio 5.45.0、indextts 可编辑安装 |
| 6. 验证 | GPU 实算、核心依赖导入、`infer_v2`/`infer_v2_5` 导入、CLI `--help` | `run_command` | 全部通过；ffmpeg 9.0.2 装进环境 |
| 收尾 | 标完成 → 写 2 条长期记忆 → `clear_plan` → 总结 | `update_plan_step`, `write_memory`, `clear_plan` | memory.md 落 m1/m2 |

关键分叉：第 6 步之前模型发现 `checkpoints/` 只有 `pinyin.vocab`，于是用 `ask_user` 问
「下哪个版本、从哪个源下」；用户选了「暂不下载权重」，验证因此止步于导入级。这是全会话
唯一一次由人改变执行方向的决定。

## 5. 总览统计

### 5.1 工具调用（44 次）

| 工具 | 次数 | 结果字符 | 说明 |
| --- | ---: | ---: | --- |
| `run_command` | 18 | 112,113 | 环境探测、clone、conda/pip、验证 |
| `update_plan_step` | 12 | 2,634 | 每步 in_progress + done 两次 |
| `read_file` | 4 | 15,376 | `.python-version`、`pyproject.toml`、README 两段 |
| `list_dir` | 3 | 515 | 工作区 / `index-tts` / `checkpoints` |
| `search_content` | 2 | 4,686 | 1 次成功、1 次参数用错 |
| `write_memory` | 2 | 579 | m1 项目事实、m2 决策 |
| `create_plan` / `clear_plan` / `ask_user` | 各 1 | 272 | 计划生命周期与唯一一次提问 |

### 5.2 图与闸门

| 指标 | 数量 | 说明 |
| --- | ---: | --- |
| `llm` superstep | 23 | 22 个带 tool_calls + 1 个最终答复 |
| `queue` superstep | 22 | 每个 AI 工具批一次 |
| `review` superstep | 44 | 25 次免审通过 + 19 次挂起；**每个 tool_call 单独一次 review pass** |
| `orchestrate` superstep | 11 | 计划/记忆类调用 |
| `tool` superstep | 18 | 每个普通工具批一次 |
| `interrupt` | 19 | 18 次 `tool_approval` + 1 次 `ask_user` |
| 被拒绝的调用 | 0 | 18/18 审批通过 |

### 5.3 长期记忆写入（2 条）

| 项 | 值 |
| --- | --- |
| 调用 | `write_memory` ×2（source=`memory`，免审，走 `orchestrate`） |
| 时机 | AI 在 step 107 发起，`orchestrate` 在 step 112 落盘（全部验证完成之后） |
| 正文 / 参数+回执 | 214 + 225 字符 / 522 + 579 字符 |
| 落点 | `resource/E-code-python-IndexTTS2_1732f309/memory/memory.md` |
| 条目 | `m1` project-fact（环境布局与运行方式）、`m2` decision（权重暂缓 + 下载入口） |
| `read_memory` | 0 次（开场记忆为空，注入的只有模板） |
| 注入影响 | 文件 1,103 字节；下次会话每轮注入，远低于 8,000 字符上限 |

内容复核：

- `m1` 记的是"仓库在哪、conda 环境叫什么/在哪、Python/torch/gradio/ffmpeg 版本、用
  `conda run -n indextts2 python` 运行"——属于"怎么跑"，跨会话有用；其中版本号可以现场
  重取，但重取的代价正是本次会话花掉的那些探测调用。该工作区没有 `AGENT.md`（画像通道
  为空），长期记忆是唯一可用的持久通道，记为 project-fact 可以接受。
- `m2` 记的是用户拍板"暂不下载权重"以及两种权重包的落点与下载入口——**不可从仓库重取**，
  正是该写的那类结论；还附了国内网络的 `HF_ENDPOINT` 提示。
- 两条都是追加（未用 `key` 覆写），type 合法，无空内容，回执与文件一致。
- 成本可忽略：参数 + 回执约 1.1k 字符，占累计 prompt 的 **0.08%**；`orchestrate` 本身 0 s。
- 时机偏晚：写入发生在所有安装与验证之后（step 107 发起、step 112 落盘）。若会话在长安装
  期间中断，本次的环境结论不会留下；在环境验证通过后（step 95 左右、ffmpeg 验证完成）
  先写 `m1` 可以更稳。这是权衡，不是错误。

**漏记的内容**：本次最有复用价值的操作教训没有进记忆——`ffmpeg.exe` 直接调用返回 255、
改用 `conda run -n indextts2 ffmpeg` 才成功（环境内二进制的 DLL/PATH 问题）；以及
`nvidia-smi` NVML 失败但 torch 可用、可用 PowerShell 查显卡。前者正是 `write_memory`
docstring 所说"工作区的既有约定或特殊之处"，下次会话很可能再次踩到。

### 5.4 环境检查的成本：13 次调用、13 次审批

按用途把 44 次工具调用切开（`list_dir` 归入项目阅读；若把工作区目录也视为环境检查则 +1）：

| 类别 | 次数 | 占比 |
| --- | ---: | ---: |
| 计划维护（create/update/clear） | 14 | 31.8% |
| 项目阅读 / 定位（README、pyproject、目录、search） | 9 | 20.5% |
| **安装前环境探测** | **7** | **15.9%** |
| **安装后运行验证** | **6** | **13.6%** |
| 安装 / 克隆 | 5 | 11.4% |
| 记忆写入 | 2 | 4.5% |
| 提问 | 1 | 2.3% |
| **环境相关合计** | **13** | **29.5%** |

- 安装前探测 7 次：`git --version`、`conda --version && conda info --envs`、`nvidia-smi`、
  PowerShell 查显卡型号、`ffmpeg -version`、PowerShell 查驱动版本、`Get-PSDrive` 查磁盘。
- 安装后验证 6 次：GPU 矩阵实算、核心依赖导入、`infer_v2` 导入、`ffmpeg.exe` 直调
  （失败）、`conda run ffmpeg -version`、双版本导入 + `infer_v2_5` + CLI `--help`。

**成本的不对称**：

| 口径 | 环境检查的份额 |
| --- | ---: |
| 工具调用数 | 13 / 44 = 29.5% |
| 审批次数 | **13 / 18 = 72.2%** |
| 人工等待 | 143.8 / 272.2 s = **52.8%** |
| 实际执行时间 | ≈ 95 s / 2564.4 s ≈ **3.7%** |

即：**环境检查在机器时间上很便宜（<4%），在人的注意力上很贵（七成审批）**。7 次探测全部
是只读命令，却每条都要停一次；6 次验证也全部要审批。降低这部分成本有两条路：

1. **合并命令**（不作为主方向）：把探测合成 1 条脚本能省审批，但审批面板里只剩一条复合
   命令，透明度下降；
2. **分层长期记忆**（推荐，见 F7）：把这些"慢变、跨工作区通用"的事实放进设备层，**会话
   开始就注入系统提示**——7 次探测里的大部分根本不需要发生，省的是工具调用、审批、人工
   等待三样，而且不牺牲单条命令的可读性。

## 6. 时间画像

### 6.1 按节点归类

| 节点 | 次数 | 总耗时 | 占比 |
| --- | ---: | ---: | ---: |
| `tool`（真实命令执行） | 18 | 1697.2 s | **66.2%** |
| `llm`（模型推理） | 23 | 594.9 s | **23.2%** |
| `review` 挂起（人工等待） | 19 | 272.2 s | **10.6%** |
| `queue` / 免审 `review` / `orchestrate` / `input` / `compact` | 60 | ≈ 0 s | ≈ 0% |
| 合计 | — | 2564.3 s | 100% |

### 6.2 安装占了三分之二

| 阶段 | 耗时 | 占墙钟 |
| --- | ---: | ---: |
| `pip install torch==2.8.* torchaudio==2.8.*`（3.46 GB） | 1111.4 s | 43.3% |
| `pip install -e .[webui]` | 234.1 s | 9.1% |
| `conda install ffmpeg` | 145.7 s | 5.7% |
| `conda create -n indextts2 python=3.11` | 105.0 s | 4.1% |
| 其余 14 个工具批（clone、探测、验证等） | 101.0 s | 3.9% |

**四条约 1600 秒的安装命令 = 62.2% 的墙钟**。对"长任务"体验而言，瓶颈不在模型，
而在不可压缩的下载/求解；这也解释了为什么 188 秒的模型延迟在总时长里并不刺眼。

### 6.3 模型延迟

23 次调用的分布：均值 25.9 s、中位数 13.4 s、最小 4.1 s、最大 **188.5 s**。

- 最大的一次（188.5 s）发生在 conda create 输出 24,190 字符之后，当时上下文仅 43,742 字符；
- 后面上下文涨到 134k–138k 时，反而出现 7.0 s、10.5 s、4.1 s 的快速调用；
- 上下文字符数与延迟的 Pearson 相关系数 ≈ **−0.06**（剔除离群点后 ≈ 0.04），本样本里
  **看不到"上下文越大越慢"的线性关系**。延迟更像 provider 侧波动/思考量差异。
  （字符数不等于 token 数，也没有观测缓存命中，结论只限本样本。）

### 6.4 人工等待

19 次挂起合计 272.2 s（均值 14.3 s、中位数 **6.9 s**）。分布极不均匀：

- 14 次 ≤ 14.1 s，多数是"看到面板 → 按 y"；
- 两次明显停顿：批准 `pip install -e .[webui]` 前 **56.9 s**、批准 GPU 自检前 **52.2 s**；
- `ask_user` 的回答花了 **27.7 s**（读 4 个选项并选择）；
- 另有 36.9 s 是批准最后的双版本导入 + CLI 验证命令。

即使按中位数算，18 次审批也意味着 18 次注意力切换；这部分成本不会出现在 42 分钟的
墙钟里，但对使用者是实打实的摩擦（见 §8、§10）。

### 6.5 空闲窗口：62.2% 的墙钟里 agent 什么都没做

- 四条安装命令合计 **1596.2 s**。期间图停在 `tool_node` 的
  `await tool_obj.ainvoke(...)` 上，没有 LLM 调用，也没有其它工具在跑。
- `ToolNode.__call__` 是**顺序** `for … await`（`app/agent/nodes.py:341`）：即使模型把多条
  命令放进同一批，也是串行执行，批处理不会带来并行。
- 最典型的是 torch 安装的 **1111.4 s（18.5 分钟）**：模型用
  `run_command(timeout=1800)` 一次等到底；工具面其实已有 `start_process` +
  `process_wait`（立即返回受管句柄），但模型没有用。
- 另一个本可并行的项是**模型权重下载**：它不写 conda 环境，本可以与 26.6 分钟的环境搭建
  重叠。但 `ask_user` 直到 ffmpeg 装完（step 101）才问，此时已进入收尾——"下载 10–20 GB"
  被整体推迟到会话之外。
- 反事实（只谈排序，不承诺收益）：把"哪个版本、哪个源"提前到读完 README 时问，并用
  `start_process` 启动权重下载，理论上可与环境搭建重叠；实际收益取决于带宽是否被打满
  （torch 从 pytorch.org 约 10 MB/s、清华 PyPI 12–20 MB/s，是否饱和本样本未观测）。
- 结论：**"agent 在下载时没事做"不是模型偷懒，而是三件事叠加**——阻塞式 `run_command`、
  工具批内的串行执行、需要人拍板的问题问得太晚。

## 7. 上下文增长

### 7.1 终态构成

| 来源 | 字符 | 占比 |
| --- | ---: | ---: |
| ToolMessage（44 条） | 136,175 | **97.7%** |
| AIMessage 正文（23 条） | 3,132 | 2.25% |
| HumanMessage（1 条） | 73 | 0.05% |
| **合计** | **139,380** | 100% |

模型自己写出的可见文字只有 3.1k 字符；**上下文几乎全部由工具输出构成**。

### 7.2 三次跳变与预算

| 事件 | 上下文变化 | 增量 |
| --- | ---: | ---: |
| 读 `pyproject.toml` + README | 2,688 → 7,417 → 14,852 | +4.7k / +7.2k |
| `conda create` 输出（solver/下载进度） | 19,437 → 43,742 | **+24,305** |
| `pip install -e .[webui]` 输出 | 47,867 → 89,476 | **+41,609** |
| `conda install ffmpeg` 输出 | 95,319 → 134,689 | **+39,370** |
| 其余全部 | 73 → 139,380 | 剩余 ~29k |

三条安装日志合计 **105,169 字符 = 终态上下文的 75.5%**，其中绝大部分是 pip/conda 的
进度条与包列表。`run_command` 的结果里，这三次占 **93.8%**（105,169 / 112,113）。

### 7.3 为什么 compact 没救场

终态 139,380 字符 > 默认预算 60,000，`route_after_llm` 确实把收尾路由到了
`compact_node`（DB 里有 1 条 `branch:to:compact_node`，随后是该节点的一次
`__no_writes__`）。但 `CompactNode._plan` 的折叠区是**最后一条 HumanMessage 之前**；
本会话只有一条 HumanMessage，折叠区为空，节点原样返回 `{}`。

即：**单轮内无论工具循环跑多久，都不会发生任何折叠**。若要等下一轮收口，第二轮的首个
LLM 调用仍要完整吞下这 139k，直到第二轮结束才可能把第一轮折掉。这正是
`docs/TODO.md`「运行中 compact」的现状；本样例可作为该条目的验收基线（实施后重放，应看到
`messages` 在工具循环中途出现 `SystemMessage` + `RemoveMessage`）。

### 7.4 落盘放大

checkpoint blob 内嵌完整 `channel_values`，所以每次 superstep 都复制一遍历史：

| 指标 | 值 |
| --- | ---: |
| checkpoint blob 合计 | 9.64 MB |
| 平均 / 最大 | 79.7 KB / 185 KB |
| 与上下文的关系 | 近似 O(superstep × 上下文) |

单轮 121 个 checkpoint（120 个 superstep）时这只是 9.7 MB；若同一轮再长一个数量级，增长是平方级的。这是
LangGraph `SqliteSaver` 的行为，不是 Theta 的代码；列在这里是为了说明"单轮不设收口"
的代价不只在 token 上。

### 7.5 成本视角：终态不是账单，累计 prompt 才是

23 次 LLM 调用每次都重发全部历史，所以计费口径应是**累计 prompt**，不是终态上下文：

| 指标 | 值 |
| --- | ---: |
| 累计 prompt（23 次之和） | **1,469,372 字符** |
| 单次均值 | 63,886 字符 |
| 终态上下文 | 139,380 字符 |
| 累计 / 终态 | **10.6 倍** |

三份安装日志会一直留在历史里，按"首次出现后每次调用都重发"计算：

| 日志 | 单条字符 | 首次出现后剩余调用数 | 累计贡献 |
| --- | ---: | ---: | ---: |
| `conda create` 输出 | 24,190 | 13 | 314,470 |
| `pip install -e .[webui]` 输出 | 41,609 | 11 | 457,699 |
| `conda install ffmpeg` 输出 | 39,370 | 7 | 275,590 |
| **合计** | **105,169** | — | **1,047,759（71.3%）** |

即：**累计 prompt 的 71.3% 是三份安装日志的重复重发**。反事实：

- 每条工具结果截到 2,000 字符 → 累计 prompt 1,469,372 → 483,613（**−67.1%**）；
- 截到 8,000 字符 → **−54.4%**。

0.47 元本身不贵（42.7 分钟），但它说明**模型调用不是时间瓶颈**；真正的账单大头是那三份
日志反复重发。按 glm-5.3-flash 的价格（未命中 0.8 元/M、命中 0.23 元/M、输出 2.8 元/M）
反推：`0.47 / 0.8 ≈ 58.75 万` 输入 token，恰好等于 `146.9 万字符 / 2.5`——**与"全部未
命中缓存"的量级吻合**（若有大量思考/输出 token，这个推断要打折扣）。也就是说，本会话
很可能没有吃到 prompt cache。

主要嫌疑是注入顺序：`LLMNode` 把 `current_plan` 放在 messages **之前**，而本会话
`update_plan_step` 改了 12 次；每次改动都会让从该位置起的整个前缀失效。稳定前缀 +
动态提醒放尾部，可能比压缩本身更能降低成本（详见
[CONTEXT_COMPRESSION_SURVEY.md](CONTEXT_COMPRESSION_SURVEY.md) §5.7 的盈亏线）。
压缩工具输出仍是纯收益——它在内容进入 history **之前**发生，不破坏缓存。

## 8. 闸门行为

| # | step | 类型 | 内容 | 人工等待 |
| ---: | ---: | --- | --- | ---: |
| 1 | 8 | 审批 | `git --version` | 4.5 s |
| 2 | 15 | 审批 | `conda --version && conda info --envs` | 3.6 s |
| 3 | 22 | 审批 | `git clone https://github.com/index-tts/index-tts.git` | 6.9 s |
| 4 | 38 | 审批 | `nvidia-smi` | 7.0 s |
| 5 | 43 | 审批 | PowerShell 查显卡型号 | 3.4 s |
| 6 | 50 | 审批 | `ffmpeg -version` | 4.2 s |
| 7 | 55 | 审批 | `conda create -y -n indextts2 python=3.11` | 5.9 s |
| 8 | 56 | 审批 | PowerShell 查驱动版本 | 3.7 s |
| 9 | 63 | 审批 | `pip install torch==2.8.* torchaudio==2.8.*`（cu128） | 16.9 s |
| 10 | 68 | 审批 | `pip install -e .[webui]` | **56.9 s** |
| 11 | 74 | 审批 | `conda run ...` GPU 实算验证 | **52.2 s** |
| 12 | 79 | 审批 | `conda run ...` 核心依赖导入 | 8.7 s |
| 13 | 83 | 审批 | `conda run ...` `infer_v2` 导入 | 9.5 s |
| 14 | 88 | 审批 | `conda install -y -n indextts2 -c conda-forge ffmpeg` | 14.1 s |
| 15 | 92 | 审批 | 直接调 `ffmpeg.exe -version`（失败） | 4.0 s |
| 16 | 93 | 审批 | PowerShell `Get-PSDrive` | 3.0 s |
| 17 | 97 | 审批 | `conda run -n indextts2 ffmpeg -version` | 3.1 s |
| 18 | 101 | **提问** | 「下载哪个模型权重、从哪个源下载？」 | **27.7 s** |
| 19 | 105 | 审批 | 双版本导入 + `indextts2 --help` | **36.9 s** |

观察：

- **18/18 通过，零拒绝**。这个样本里审批闸门没有起到"拦截错误"的作用，只起到了
  "确认"作用；错误是模型在工具结果里自己发现并纠正的（§9）。
- **一个 AI 批里的多条命令 = 多个中断**。step 55/56 是同一批里的 `conda create` 与
  PowerShell 查询；step 92/93 是同一批里的 ffmpeg 验证与磁盘查询。设计上"恢复前每个
  tool_call 各自 `interrupt()` 一次"，所以批处理并不减少审批次数。
- 免审面在这次会话里**一次都没用上**：`command_policy.json` 只放行 git 只读子命令，
  而 `git --version` 不匹配 `status|branch|diff|log|fetch`，仍要审批。
- `ask_user` 的表现符合设计：有 `why`、4 个选项、单选；回答写入 `ask_answers`，
  下一轮由 `QueueNode` 清空，终态 `{}`。

## 9. 失败与自愈

| # | 工具调用 | 现象 | 可能原因 | 恢复 | 额外成本 |
| ---: | --- | --- | --- | --- | --- |
| 1 | `nvidia-smi` | exit 255，`Failed to initialize NVML: Unknown Error` | WSL/驱动侧 NVML 不可用 | 改 PowerShell `Get-CimInstance Win32_VideoController` | 1 次调用 |
| 2 | `search_content(path="index-tts/README.md")` | `路径不是目录或不存在` | 该工具只收目录 | 改 `read_file` 分段读 | 1 次调用 |
| 3 | `ffmpeg -version` | `'ffmpeg' is not recognized` | 系统未安装 | 后续 `conda install ffmpeg` | 无（本就计划装） |
| 4 | `...\Scripts\ffmpeg.exe -version \| findstr ...` | exit 255，(无输出) | 直接调 exe 可能缺 conda 环境的 DLL/PATH | 改 `conda run -n indextts2 ffmpeg -version` | 1 次调用 |

四次都在同一回合内自愈，没有悬空 tool_call，也没有把错误当结论。第 4 次的教训值得记：
**conda 环境里的二进制优先用 `conda run` 调**，直接给绝对路径不保证环境变量/DLL 搜索路径。

## 10. 框架层发现与建议

按紧迫度排序（2026-10-18 用户反馈成本与闲置证据后调整：F1/F2 从"重要"升为 **P0**）。

### F1（P0）单轮长任务没有中间收口

- 证据：139,380 字符 > 60,000 预算；`compact_node` 被路由到但返回 `{}`；`notes` 未生成。
- 影响：工具循环越长，每次 LLM 调用和每个 checkpoint 都在为全部历史付费。本会话
  累计 prompt **1,469,372 字符 = 终态的 10.6 倍**（§7.5）。
- 建议：按 `docs/TODO.md`「运行中 compact」落地；本样例作为回归基线——重放后应看到
  工具循环中途出现折叠，且最终 `messages` 不再线性增长到 139k。

### F2（P0）工具输出没有上限，进度条原样进上下文

- 证据：`run_command` 结果 112,113 字符，其中三份安装日志 105,169（93.8%）；
  这些日志里大量是 `Collecting… / Downloading… / | |` 进度字符。按"每次调用重发"计，
  它们占累计 prompt 的 **71.3%**；截到 2k/8k 字符可分别省 **67.1% / 54.4%**（§7.5）。
- 影响：模型只需要最后的 `Successfully installed …`，却为整段输出反复付费；这也是 F1 的
  主要触发源。
- 建议（任选其一或组合）：
  1. 在 `mcp_service/terminal.py::_render_finished` 或
     `app/platform/tool_results.py::format_tool_result` 做**头尾截断**（如各留 8k 字符，
     中间标注省略量）；
  2. 长输出落盘（工作区 `.theta/logs/`），回执只带尾部摘要 + 文件路径，模型需要时用
     `read_file` 分段读；
  3. 至少剥掉 `\r` 进度回显与连续空行。

### F3（P1）审批粒度与免审面

- 证据：18 次 `run_command` = 18 次中断，其中 **13 次是环境探测或验证**
  （7 次安装前探测 + 6 次安装后验证，§5.4），占审批总数的 **72.2%**、人工等待的
  **52.8%**，但只占实际执行时间约 **3.7%**。
- 现状：`app/agent/command_policy.json` 只免审 git 只读子命令；无子命令的命令
  （`nvidia-smi`、`ffmpeg -version`）还缺"子命令无所谓"的表项（TODO 已记）。
- 建议：免审面只能作为兜底——若要开口子，优先给**精确匹配的只读命令**（如 `conda info`、
  `conda --version`、`nvidia-smi`、`ffmpeg -version`），**不要**放行整个 `powershell`
  家族（它等价于任意命令执行）。更根本的方向是 F7：让这些探测**根本不需要发生**。

### F4（P2）`search_content` 不接受文件路径

- 证据：模型自然地传了 `README.md`，收到"路径不是目录或不存在"。
- 建议：在 `mcp_service/file_io.py` 把"文件路径"规范化为"在单文件内搜索"，或把错误
  文案改成"请传目录，或改用 read_file"（当前文案已可读，成本仅一次往返）。

### F5（观察）checkpoint 落盘随上下文放大

- 证据：9.64 MB / 121 checkpoint，blob 内嵌完整 `channel_values`。
- 这是 LangGraph `SqliteSaver` 的机制，不是 Theta 的代码。若将来要为长回合做预算，
  除了 token，也应把 checkpoint 体积纳入估算。

### F6（P0）长命令阻塞整个回合，工具批内又是串行

- 证据：四条安装 1596.2 s（**62.2% 墙钟**）没有任何并发工作；`ToolNode.__call__` 顺序
  `await`；模型用 `run_command(timeout=1800)` 而非 `start_process`。
- 影响：用户的等待时间与 agent 的空闲时间完全重叠；本可并行的权重下载被推迟到会话之外。
- 建议：
  1. **行为/提示层**：预计 > 60 s 的命令优先 `start_process`，先做独立工作再
     `process_wait`；`run_command` 的 docstring 已有这个方向，但没有在长安装场景被执行；
  2. **流程层**：需要人拍板的问题（版本、下载源、是否下载）前置到长命令之前；
  3. **架构层**（需单独评估）：让 `ToolNode` 对已批准调用并发执行——但写同一 conda/pip
     环境的命令并发不安全，必须按资源分组，不能一刀切 `gather`。

### F7（P0，方向已定）环境事实应在会话开始注入——长期记忆分层化的①设备层

- 本会话证据：7 次安装前探测 = 7 次审批、约 28 s 执行、29.4 s 人工等待；问的全是
  **慢变、跨工作区通用**的事实（git / conda / OS / GPU / 系统级 ffmpeg）。
- 现状：长期记忆只有一层——`resource/<ws_key>/memory/memory.md`，按工作区私有。本次开场
  是空的，所以这些事实只能现场探；同一台机器上每开一个新工作区都要重探一遍。
- 方向（对应 `docs/TODO.md`「长期记忆分层化」）：
  - **① 设备层**：OS/shell、python/git/conda 的路径与版本、GPU 型号/驱动、包管理器、
    系统级 ffmpeg、已知工具坑（如"conda 环境内二进制要 `conda run`"）；
  - **② 用户层**：跨工作区偏好（是否单列待拍）；
  - **③ 工作区层**：现状不变，放工作区特有环境（`indextts2` 环境名/路径、torch
    2.8.0+cu128、`pip install -e .[webui]`、运行命令）；
  - `AGENT.md` 是**另一条轴**（随仓库走），不并入分层。
- **易变事实不进层**：磁盘剩余空间、`conda env list`、当前包版本快照——继续按需探测。
  层里只写"上次验证"的快照，并带环境指纹/日期；与命令结果冲突时以命令为准并更新。
- 注入方式：作为独立 `SystemMessage`，排在基础提示之后、项目画像/工作区记忆之前；与现有
  `LLMNode` 的 profile/memory 注入点同构（每轮重建、profile 用 memo）。① 只有十几行，
  不需要额外的"会话开始"钩子——第一次 LLM 调用自然就带上了。
- 推荐写法：**(c) 混合**——host 在建会话时按固定白名单只读探测客观版本（零模型成本、
  零审批），agent 把主观结论与坑写进 ③；`write_memory` 加 `scope` 枚举而不是路径，保持
  "模型碰不到路径"的安全不变量。细节见 TODO 条的 8 个待拍点。
- 边界：它**不能**消除安装后的 6 次运行验证——那验证的是"这一次真的能跑"；分层记忆省的
  是探测与决策，不是验证。

### 正面结论

以下通道在本样例中按设计工作，可视为回归参考：

- `ask_user` 在 `ReviewNode` 挂起、回答两段式、下一轮清空；
- 计划工具链式更新（同回合多次 `update_plan_step` 顺序生效）与 `clear_plan`；
- `write_memory` 落 `resource/<ws_key>/memory/memory.md`，回执与文件内容一致；
- 审批拒绝路径未触发（本样例没有拒绝），不属于覆盖范围；
- 回合结束状态干净，无悬空 tool_call。

## 11. 模型行为评价

**做得好的**

- 先建计划再动手，6 步与用户意图一一对应；
- 并行化合理：平均每个 AI 批 2.0 个工具调用（44/22），把互不依赖的探测/安装放在同一批；
- 遇到歧义先问：IndexTTS-2.5 是仓库默认版本，与用户说的 "IndexTTS2" 不是同一个权重包，
  模型没有猜，而是给出 4 个可执行选项；
- 验证分层：GPU 实算 → 核心依赖导入 → 推理模块导入 → CLI 入口，逐层加深；
- 收尾诚实：明确写出"权重未下载，还不能真实语音合成"，并给出下一步命令；
- 把环境布局与遗留决策写进长期记忆（`m1`/`m2`），分类正确、追加而非覆写，下次会话可直接
  接续；两条合计约 1.1k 字符，成本可忽略。

**可改进**

- 计划工具占了 27% 的调用次数（12/44）。每次 `update_plan_step` 都回显完整计划快照，
  信息增量低；可考虑"一次迁移多个步骤"或在回执里只回差异。
- 一次参数误用（`search_content` 传文件）说明工具边界还可以更宽容或更直白。
- 记忆漏掉了最有复用价值的操作教训（`conda run` 调环境内二进制），下次可能重复踩坑；
  这类"失败 → 恢复"的结论应在验证通过时顺手写入。
- 单次 188.5 s 的模型延迟无法从本样本归因；若复现，应记录 provider 侧指标。

## 12. 复现方法

只读解码，不需要启动应用：

```python
from langgraph.checkpoint.sqlite import SqliteSaver

db = r"resource/E-code-python-IndexTTS2_1732f309/sessions/f2158119aa0b4b26ae9ddc42295e347f/agent.db"
tid = "f2158119aa0b4b26ae9ddc42295e347f"
with SqliteSaver.from_conn_string(db) as saver:
    cfg = {"configurable": {"thread_id": tid}}
    state = saver.get_tuple(cfg).checkpoint["channel_values"]
    for m in state["messages"]:
        print(m.type, getattr(m, "name", ""), len(str(m.content)))
```

时间归因的要点：把 `checkpoints` 按 `metadata.step` 排序，`ts` 取
`checkpoint["ts"]`，相邻差即 superstep 耗时；节点类型由该 `checkpoint_id` 在
`writes` 表里的 channel 推断（规则见 §3）。

## 附录 A：工具调用流水（44 条）

| # | 工具 | 参数（截断） | 结果（首行） |
|---|---|---|---|
| 1 | `create_plan` | {"steps": ["查看工作区现状，确认目录内容与 git 可用性", "检查 conda 是否可用及版本信息", "克隆 IndexTTS2 仓库到工作区", "用 conda 新建 indextts2 环境（Py… | 已生成计划（6 步） - [ ] 1. 查看工作区现状，确认目录内容与 git 可用性 - [ ] 2. 检查 conda 是否可用及版本信息 - [ ] 3. 克隆 IndexT… |
| 2 | `update_plan_step` | {"step_id": "1", "status": "in_progress"} | 步骤 1 已标记为 in_progress - [ ] 1. 查看工作区现状，确认目录内容与 git 可用性 - [ ] 2. 检查 conda 是否可用及版本信息 - [ ] 3… |
| 3 | `list_dir` | {"path": "."} | 目录: E:\code\python\IndexTTS2 (空目录) |
| 4 | `run_command` | {"command": "git --version", "description": "检查 git 是否已安装并查看版本"} | exit_code: 0 --- 输出 --- git version 2.51.1.windows.1 |
| 5 | `update_plan_step` | {"step_id": "1", "status": "done"} | 步骤 1 已标记为 done - [x] 1. 查看工作区现状，确认目录内容与 git 可用性 - [ ] 2. 检查 conda 是否可用及版本信息 - [ ] 3. 克隆 In… |
| 6 | `update_plan_step` | {"step_id": "2", "status": "in_progress"} | 步骤 2 已标记为 in_progress - [x] 1. 查看工作区现状，确认目录内容与 git 可用性 - [ ] 2. 检查 conda 是否可用及版本信息 - [ ] 3… |
| 7 | `run_command` | {"command": "conda --version && conda info --envs", "description": "检查 conda 版本及现有环境列表"} | exit_code: 0 --- 输出 --- conda 26.1.1 # conda environments: # # * -> active # + -> frozen b… |
| 8 | `update_plan_step` | {"step_id": "2", "status": "done"} | 步骤 2 已标记为 done - [x] 1. 查看工作区现状，确认目录内容与 git 可用性 - [x] 2. 检查 conda 是否可用及版本信息 - [ ] 3. 克隆 In… |
| 9 | `update_plan_step` | {"step_id": "3", "status": "in_progress"} | 步骤 3 已标记为 in_progress - [x] 1. 查看工作区现状，确认目录内容与 git 可用性 - [x] 2. 检查 conda 是否可用及版本信息 - [ ] 3… |
| 10 | `run_command` | {"command": "git clone https://github.com/index-tts/index-tts.git", "description": "从 GitHub 克隆 IndexTTS 官方仓库到… | exit_code: 0 --- 输出 --- Cloning into 'index-tts'... |
| 11 | `update_plan_step` | {"step_id": "3", "status": "done"} | 步骤 3 已标记为 done - [x] 1. 查看工作区现状，确认目录内容与 git 可用性 - [x] 2. 检查 conda 是否可用及版本信息 - [x] 3. 克隆 In… |
| 12 | `list_dir` | {"path": "index-tts"} | 目录: E:\code\python\IndexTTS2\index-tts [dir] .git [dir] .github [dir] archive [dir] assets… |
| 13 | `read_file` | {"path": "index-tts/.python-version"} | 3.11.13 |
| 14 | `read_file` | {"path": "index-tts/pyproject.toml"} | [project] name = "indextts" version = "2.0.0" description = "IndexTTS2: A Breakthrough in … |
| 15 | `run_command` | {"command": "nvidia-smi", "description": "检查本机是否有 NVIDIA GPU 及驱动 CUDA 版本"} | exit_code: 255 --- 输出 --- Failed to initialize NVML: Unknown Error |
| 16 | `search_content` | {"context_lines": 3, "path": "index-tts/README.md", "query": "install"} | 路径不是目录或不存在: E:\code\python\IndexTTS2\index-tts\README.md |
| 17 | `run_command` | {"command": "powershell -Command \"Get-CimInstance Win32_VideoController ｜ Select-Object Name, AdapterRAM ｜ Fo… | exit_code: 0 --- 输出 --- Name : NVIDIA GeForce RTX 4060 Laptop GPU AdapterRAM : 4293918720 … |
| 18 | `read_file` | {"end_line": 120, "path": "index-tts/README.md", "start_line": 1} | [index-tts/README.md 第 1-120 行 / 共 556 行] <div align="center"> <picture> <source media="(p… |
| 19 | `read_file` | {"end_line": 250, "path": "index-tts/README.md", "start_line": 120} | [index-tts/README.md 第 120-250 行 / 共 556 行] Download the required models via [uv tool](htt… |
| 20 | `list_dir` | {"path": "index-tts/checkpoints"} | 目录: E:\code\python\IndexTTS2\index-tts\checkpoints [file] pinyin.vocab |
| 21 | `run_command` | {"command": "ffmpeg -version", "description": "检查系统是否已安装 ffmpeg（IndexTTS 音频处理依赖）"} | exit_code: 1 --- 输出 --- 'ffmpeg' is not recognized as an internal or external command, ope… |
| 22 | `update_plan_step` | {"step_id": "4", "status": "in_progress"} | 步骤 4 已标记为 in_progress - [x] 1. 查看工作区现状，确认目录内容与 git 可用性 - [x] 2. 检查 conda 是否可用及版本信息 - [x] 3… |
| 23 | `run_command` | {"command": "conda create -y -n indextts2 python=3.11", "description": "用 conda 新建名为 indextts2 的 Python 3.11 环… | exit_code: 0 --- 输出 --- 3 channel Terms of Service accepted Retrieving notices: - \ ｜ … |
| 24 | `run_command` | {"command": "powershell -Command \"Get-CimInstance Win32_VideoController ｜ Where-Object {$_.Name -like '*NVIDI… | exit_code: 0 --- 输出 --- Name : NVIDIA GeForce RTX 4060 Laptop GPU DriverVersion : 32.0.16.… |
| 25 | `update_plan_step` | {"step_id": "4", "status": "done"} | 步骤 4 已标记为 done - [x] 1. 查看工作区现状，确认目录内容与 git 可用性 - [x] 2. 检查 conda 是否可用及版本信息 - [x] 3. 克隆 In… |
| 26 | `update_plan_step` | {"step_id": "5", "status": "in_progress"} | 步骤 5 已标记为 in_progress - [x] 1. 查看工作区现状，确认目录内容与 git 可用性 - [x] 2. 检查 conda 是否可用及版本信息 - [x] 3… |
| 27 | `run_command` | {"command": "D:\\Miniconda\\envs\\indextts2\\python.exe -m pip install torch==2.8.* torchaudio==2.8.* --index-… | exit_code: 0 --- 输出 --- Looking in indexes: https://download.pytorch.org/whl/cu128 Collect… |
| 28 | `run_command` | {"command": "D:\\Miniconda\\envs\\indextts2\\python.exe -m pip install -e .[webui] -i https://pypi.tuna.tsingh… | exit_code: 0 --- 输出 --- Looking in indexes: https://pypi.tuna.tsinghua.edu.cn/simple Obtai… |
| 29 | `update_plan_step` | {"step_id": "5", "status": "done"} | 步骤 5 已标记为 done - [x] 1. 查看工作区现状，确认目录内容与 git 可用性 - [x] 2. 检查 conda 是否可用及版本信息 - [x] 3. 克隆 In… |
| 30 | `update_plan_step` | {"step_id": "6", "status": "in_progress"} | 步骤 6 已标记为 in_progress - [x] 1. 查看工作区现状，确认目录内容与 git 可用性 - [x] 2. 检查 conda 是否可用及版本信息 - [x] 3… |
| 31 | `run_command` | {"command": "conda run -n indextts2 python -c \"import torch; print('torch', torch.__version__); print('cuda a… | exit_code: 0 --- 输出 --- torch 2.8.0+cu128 cuda available: True device: NVIDIA GeForce RTX … |
| 32 | `run_command` | {"command": "conda run -n indextts2 python -c \"import transformers, librosa, gradio, accelerate, soundfile, n… | exit_code: 0 --- 输出 --- transformers 4.52.1 gradio 5.45.0 numpy 2.2.6 indextts package OK:… |
| 33 | `run_command` | {"command": "conda run -n indextts2 python -c \"from indextts.infer_v2 import IndexTTS2; print('infer_v2 impor… | exit_code: 0 --- 输出 --- infer_v2 import OK |
| 34 | `search_content` | {"context_lines": 2, "path": "index-tts", "query": "ffmpeg"} | 共命中 17 条（含每处前后 2 行）： index-tts\pyproject.toml:34: "descript-audiotools==0.7.2", index-tts\… |
| 35 | `run_command` | {"command": "conda install -y -n indextts2 -c conda-forge ffmpeg", "description": "在 indextts2 conda 环境内安装 ffm… | exit_code: 0 --- 输出 --- 3 channel Terms of Service accepted Channels: - conda-forge - http… |
| 36 | `run_command` | {"command": "D:\\Miniconda\\envs\\indextts2\\Scripts\\ffmpeg.exe -version 2>&1 ｜ findstr /C:\"ffmpeg version\"… | exit_code: 255 (无输出) |
| 37 | `run_command` | {"command": "powershell -Command \"Get-PSDrive -PSProvider FileSystem ｜ Select-Object Name, @{N='FreeGB';E={[m… | exit_code: 0 --- 输出 --- Name FreeGB UsedGB ---- ------ ------ C 39.1 163.2 D 100.9 171.4 E… |
| 38 | `run_command` | {"command": "conda run -n indextts2 ffmpeg -version", "description": "通过 conda 环境直接运行 ffmpeg -version 验证其可用性"} | exit_code: 0 --- 输出 --- ffmpeg version 9.0.2 Copyright (c) 2000-2026 the FFmpeg developers… |
| 39 | `ask_user` | {"options": ["IndexTTS-2.5 ← ModelScope 下载（推荐：仓库当前默认版本，WebUI 开箱即用；ModelScope 国内速度快）", "IndexTTS-2 ← ModelScope… | [user_answer] 用户选择了：4. 暂不下载权重，只完成导入级验证（环境已就绪，模型可随时再下） |
| 40 | `run_command` | {"command": "conda run -n indextts2 python -c \"from indextts.infer_v2_5 import IndexTTS2 as T25; from indextt… | exit_code: 0 --- 输出 --- infer_v2 & infer_v2_5 import OK usage: indextts2 [-h] {init,config… |
| 41 | `update_plan_step` | {"step_id": "6", "status": "done"} | 步骤 6 已标记为 done - [x] 1. 查看工作区现状，确认目录内容与 git 可用性 - [x] 2. 检查 conda 是否可用及版本信息 - [x] 3. 克隆 In… |
| 42 | `write_memory` | {"content": "IndexTTS2 环境：仓库在 E:\\code\\python\\IndexTTS2\\index-tts（可编辑安装），conda 环境 \"indextts2\"（D:\\Minicon… | 已写入 [m1] project-fact · 2026-10-18 IndexTTS2 环境：仓库在 E:\code\python\IndexTTS2\index-tts（可编辑… |
| 43 | `write_memory` | {"content": "模型权重尚未下载（用户 2026-10 拍板暂缓）：完整推理前需下载权重，IndexTTS-2.5 → checkpoints/（默认版），IndexTTS-2 → checkpoints_2/… | 已写入 [m2] decision · 2026-10-18 模型权重尚未下载（用户 2026-10 拍板暂缓）：完整推理前需下载权重，IndexTTS-2.5 → checkpo… |
| 44 | `clear_plan` | {} | 已清空全部计划 |

## 附录 B：AI 正文（23 条，合计 3,132 字符）

> 正文为原文，仅把 content 原样列出；标题后的工具名是该条 AI 消息发起的调用。

1. **AI #1** → `create_plan`

   > 我来帮你完成这个任务。先创建一个执行计划，然后开始动手。

2. **AI #2** → `update_plan_step`, `list_dir`, `run_command`

   > 计划已建立。现在开始第 1 步：查看工作区现状。

3. **AI #3** → `update_plan_step`, `update_plan_step`, `run_command`

   > 工作区是空目录，git 2.51.1 可用。接下来检查 conda 环境。

4. **AI #4** → `update_plan_step`, `update_plan_step`, `run_command`

   > Conda 26.1.1 可用（环境存放在 D:\Miniconda），没有已存在的 indextts2 环境，后续直接新建。现在克隆 IndexTTS2 仓库到工作区。

5. **AI #5** → `update_plan_step`, `list_dir`

   > 克隆完成。现在查看项目结构，确认 Python 版本要求和依赖清单。

6. **AI #6** → `read_file`, `read_file`

   > 仓库已克隆。现在读取 README 和 Python 版本要求，了解安装说明。

7. **AI #7** → `run_command`, `search_content`

   > 项目使用 pyproject.toml 管理依赖（无 requirements.txt），要求 Python >=3.10,<3.12（推荐 3.11.13），torch 2.8 + CUDA 12.8。现在读 README 的安装说明，并检查本机 GPU 情况以决定 torch 的安装方式。

8. **AI #8** → `run_command`, `read_file`

   > nvidia-smi 初始化失败，需要换方式确认 GPU；search_content 需要目录参数。换用别的方式查询。

9. **AI #9** → `read_file`, `list_dir`, `run_command`

   > 本机有 NVIDIA RTX 4060 Laptop GPU（nvidia-smi 报 NVML 错误，稍后用 torch 实测验证）。README 指明用 uv 安装，但用户要求用 conda 管理，我将用 conda 建 Python 3.11.13 环境并按 pyproject.toml 的依赖规格安装。先看模型下载部分和 ffmpeg 情况。

10. **AI #10** → `update_plan_step`, `run_command`, `run_command`

   > 信息已齐全。关键发现：
   > - 项目用 pyproject.toml 管理依赖，核心依赖 torch 2.8 + CUDA 12.8，Python 要求 >=3.10,<3.12（推荐 3.11.13）
   > - 本机有 RTX 4060 Laptop GPU，但 **ffmpeg 未安装**（需要补）
   > - 模型权重未下载（checkpoints 只有 pinyin.vocab），这是运行前的大头
   >
   > 现在创建 conda 环境（Python 3.11），同时查一下 NVIDIA 驱动版本——cu128 的 torch 轮子要求较新驱动，需要据此决定装哪个 CUDA 变体。

11. **AI #11** → `update_plan_step`, `update_plan_step`, `run_command`

   > Conda 环境 `indextts2`（Python 3.11.17）已创建成功。本机 NVIDIA 驱动版本较新（32.0.16.1088，按官方映射规则即 610.88，完全支持 CUDA 12.8 轮子）。
   >
   > 现在进入第 5 步安装依赖。项目无 requirements.txt，依赖由 pyproject.toml 锁定（torch 2.8 + CUDA 12.8）。Windows 上 PyPI 默认的 torch 是 CPU 版，所以先从 PyTorch 官方 cu128 源装 GPU 版 torch，再装其余依赖。

12. **AI #12** → `run_command`

   > PyTorch 2.8.0+cu128（GPU 版）安装成功。现在安装项目其余依赖（含 webui 的 gradio），使用清华镜像加速，已装好的 torch 不会被重装。

13. **AI #13** → `update_plan_step`, `update_plan_step`, `run_command`

   > 全部依赖安装成功（包括 gradio 5.45.0、indextts 2.0.0 可编辑安装，torch 2.8.0+cu128 保持不变）。现在进入第 6 步验证：先验证 torch CUDA 可用性和核心包导入。

14. **AI #14** → `run_command`

   > GPU 计算正常（torch 2.8.0+cu128 识别到 RTX 4060）。继续验证核心依赖链（transformers、librosa、gradio、indextts 包本身）能否导入。

15. **AI #15** → `run_command`, `search_content`

   > 核心依赖导入正常，indextts 包已可编辑安装。继续做更深入的冒烟测试：导入推理模块、检查 CLI 入口，并确认 ffmpeg 二进制的必要性（系统还没装 ffmpeg，之前检查过）。

16. **AI #16** → `run_command`

   > 推理模块导入成功。发现两点需要处理：
   > 1. ffmpeg 二进制缺失（系统级，推理音频处理会用到）——用 conda 装进环境内
   > 2. **模型权重未下载**（checkpoints 只有 pinyin.vocab）——完整运行必须下载，IndexTTS-2 权重约 10GB+
   >
   > 先装 ffmpeg：

17. **AI #17** → `run_command`, `run_command`

   > ffmpeg 已装入环境。先验证 ffmpeg 可用并检查 E 盘剩余空间（模型权重很大，需要确认放得下）：

18. **AI #18** → `run_command`

   > E 盘有 162.8GB 空闲，模型权重放得下。ffmpeg 验证命令返回码异常，直接重测：

19. **AI #19** → `ask_user`

   > ffmpeg 9.0.2 正常。环境层面已全部就绪。
   >
   > 最后一个环节是模型权重下载——这是"确保能正确运行"的必要条件，但有两个只有你能定的问题：**下载哪个版本**（仓库默认已切换到 IndexTTS-2.5，而经典 IndexTTS-2 是独立权重包）以及**从哪个源下载**（权重约 10-20GB，下载源直接影响耗时）。猜错会白下十几个 GB，先问你一下：

20. **AI #20** → `run_command`

   > 明白，暂不下载权重，完成导入级验证后收尾。最后验证 CLI 入口和两个版本的推理模块都能正常工作：

21. **AI #21** → `update_plan_step`, `write_memory`, `write_memory`

   > 全部验证通过。收尾：标记计划完成、写入长期记忆（环境布局与遗留事项，下次会话直接可用）、清空计划。

22. **AI #22** → `clear_plan`

   > （空正文）

23. **AI #23**（最终答复）

   > ✅ 任务完成。以下是全部工作的总结：
   >
   > ## 已完成
   >
   > **1. 克隆仓库**
   > - IndexTTS2 官方仓库已克隆到 `E:\code\python\IndexTTS2\index-tts`（当前为 IndexTTS-2.5 代码版本）
   >
   > **2. Conda 环境搭建**（环境名 `indextts2`）
   > - Python 3.11.17（符合项目要求的 >=3.10,<3.12）
   > - PyTorch **2.8.0+cu128 GPU 版**（从 PyTorch 官方 CUDA 12.8 源安装，适配你的 RTX 4060）
   > - 项目全部依赖按 `pyproject.toml` 锁定版本安装（transformers 4.52.1、librosa、numba、accelerate 等），indextts 包本身以可编辑模式装入
   > - WebUI 支持（gradio 5.45.0）
   > - ffmpeg 9.0.2（装在环境内，不影响系统）
   >
   > **3. 运行验证**（全部通过）
   > - ✅ GPU 可用：`torch.cuda.is_available() = True`，识别到 RTX 4060 Laptop GPU，并完成了一次真实 GPU 矩阵运算
   > - ✅ 核心依赖导入：transformers / librosa / gradio / accelerate 均正常
   > - ✅ 推理模块：`indextts.infer_v2`（2.0 版）与 `indextts.infer_v2_5`（2.5 版）均能成功导入
   > - ✅ CLI 入口：`indextts2 --help` 正常输出
   >
   > ## 日常使用方式
   >
   > ```bash
   > conda activate indextts2
   > cd E:\code\python\IndexTTS2\index-tts
   >
   > # 启动 WebUI（需要权重就绪后）
   > python webui.py
   > ```
   >
   > ## ⚠️ 唯一遗留事项：模型权重未下载
   >
   > 按你的选择暂缓了权重下载（约 10–20GB），当前 `checkpoints/` 目录里只有词表文件，所以**还不能做真实语音合成**。环境本身已完全就绪，之后想跑通推理时随时告诉我，或自己执行：
   >
   > ```bash
   > conda run -n indextts2 indextts2 download
   > ```
   >
   > 这些信息我已写入长期记忆，下次会话可以直接接着干，不用重新摸底。
