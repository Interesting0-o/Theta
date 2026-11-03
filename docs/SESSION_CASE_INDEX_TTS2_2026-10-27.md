# 会话样例：IndexTTS2 多轮部署与 token 归因（2026-10-26/27）

> **性质**：真实会话的量化复盘 + token 归因实验，不是目标态设计。数据只读，不改仓库。
>
> **数据来源**：
> `resource/E-code-python-IndexTTS2_1732f309/sessions/0f9c120dea1442fc84e57fa329ee49b7/`
> 下的 `agent.db`（LangGraph checkpoint）与 `event_log.jsonl`。
>
> **一句话**：4 段真实任务 + 26 行粘贴，159 次真实模型调用、226 次工具调用、128 次审批；
> 活跃 1 小时 49 分，累计 **10.90M prompt token**（缓存命中 90.5%），费用 **2.06 元**。
> 本次实验的重点不是命中率，而是**这 10.90M 到底花在哪些消息/工具上**：61.4% 是普通工具
> 载荷（结果 + 调用参数），23.3% 是稳定前缀，13.1% 是计划/记忆/提问等状态通道。
>
> **与 2026-10-18 案例的关系**：`SESSION_CASE_INDEX_TTS2.md` 记录的是同一工作区的单轮长任务
> （44 次工具 / 19 次中断 / 139k 字符终态 / 0.47 元）；本篇记录的是**多轮、跨天、带折叠**的
> 会话，并第一次给出了逐消息、逐工具的 token 归因。两篇可对照阅读：前者证明"单轮无收口"，
> 本篇证明"轮末折叠已生效，但当前轮仍在重复付费"。

---

## 1. 收录理由

这次会话是 Theta 到目前为止信息量最大的一次真实运行：

- **覆盖了完整的多轮生命周期**：turn 末折叠、跨天恢复、`ask_user`、指令转向（steering）、
  多行粘贴、单次拒绝、长命令阻塞、WebUI 启动排障；
- **第一次有完整 token 账本**：每条 `AIMessage.response_metadata` 都带
  `prompt_tokens / prompt_cache_hit_tokens / prompt_cache_miss_tokens / completion_tokens /
  reasoning_tokens`，可以精确到每次调用；
- **第一次能回答"贵在哪"**：159 次调用 × 每次上下文，按消息类别、按工具、按消息寿命拆开；
- **给出了可复算的降本基线**：同一份 DB 可以在实现轮内移动窗口后重放，直接对比
  总 prompt、miss、每类消息和每个工具的累计 token。

---

## 2. 会话元数据

| 项 | 值 |
| --- | --- |
| 工作区 | `E:\code\python\IndexTTS2` |
| `ws_key` | `E-code-python-IndexTTS2_1732f309` |
| `thread_id` / `session_id` | `0f9c120dea1442fc84e57fa329ee49b7` |
| 模型 | `deepseek-flash` |
| 起止（UTC） | 2026-10-26 13:49:55 → 2026-10-27 02:58:28 |
| 墙钟 | 13 小时 08 分 33 秒（含 11 小时 17 分空闲） |
| 真实活跃时长 | **1 小时 49 分 14 秒** |
| 用户消息 | 29 条 = 3 条真实请求 + 26 行粘贴的终端日志 |
| 真实任务段 | 4 段（turn 1 / 2 / 3 / 最后的 WebUI 排障） |
| 真实模型调用 | **159 次**（另有 26 次空转收口：turn 1 末 1 次 + 粘贴 25 次，零模型调用） |
| 工具调用 | **226 次** |
| 人工闸门 | **128 次工具审批 + 1 次 `ask_user` = 129 次挂起**；127 通过、1 拒绝 |
| 失败调用 | 17 次（`run_command` 12 / `search_content` 3 / `process_wait` 1 / `start_process` 1） |
| 最终上下文 | 157 条消息、64,540 字符 |
| 上下文峰值 | **176,250 字符**（turn 3） |
| Token | prompt **10,904,561**；cache hit **9,870,976（90.5%）**；miss **1,033,585**；completion 81,167（其中 reasoning 39,531） |
| 费用 | **2.06 元**（用户提供） |
| checkpoint / writes | 986 / 3,357 |
| DB 体积 | 190 MB（checkpoint blob 合计 178 MB，平均 185 KB，最大 412 KB） |

---

## 3. 口径与方法

### 3.1 数据

1. **checkpoint**：用仓库 venv 的 `langgraph.checkpoint.sqlite.SqliteSaver` 读取；每条
   checkpoint 的 `channel_values["messages"]` 是该 superstep 起点的完整消息列表。
2. **时间**：`checkpoint["ts"]` 是 superstep 起点；相邻 checkpoint 的 `ts` 差 = 该
   superstep 的实际耗时。节点归属沿用 `SESSION_CASE_INDEX_TTS2.md` §3 的规则（按 `writes`
   表的 channel 推断）；相邻差 > 300 s 的窗口单独视为空闲，不计入活跃时长。
3. **tool 事件**：`event_log.jsonl` 每个 tool_call 一行，226 行；含
   `tool / target / status / error_type / approval / content_chars / ts`。
4. **token**：每条真实 `AIMessage` 的 `response_metadata.token_usage`。本会话 159 条真实
   AI 消息全部带 usage；另 26 次 `is_inject` 空转不产生 AI 消息。

### 3.2 Token 归因 `[估算]`

真实数据只有**每次调用**的 `prompt_tokens`，没有逐消息 token。归因方法：

1. 对每次真实 LLM 调用，取该 checkpoint 的完整消息列表；
2. 每次调用固定扣掉 **16,000 token** 的稳定前缀（系统提示 + 工具 schema + 工作区/画像）。
   这个数来自 turn 1 缓存被打碎后的 cached 下限（16,000），以及首轮
   `prompt_tokens=16,790` 且 Human 仅 73 字符的实测；
3. 剩余 token 按各消息的字符占比分摊到消息类别；
4. `AIMessage` 的 tool_calls 参数按 `json.dumps(args) + name + id` 计入"工具调用参数"；
   `ToolMessage` 按 `content + name + tool_call_id` 计入"工具结果"；
5. `HumanMessage` 在 `---\n<RUNTIME_STATE` 处切开，用户正文与运行状态分开计。

**口径限制**：字符占比不是 token 占比，中英文/日志的 tokenizer 密度不同；稳定前缀 16k
也是实测近似值。因此**分项绝对值有 ±10% 左右的误差，但相对排序和"大头"判断是可靠的**。
所有分项之和被强制等于 10,904,561。

### 3.3 寿命乘数

一条消息的"寿命乘数" = 它出现在多少次真实 LLM 调用里。累计 token ≈ 单条大小 × 寿命。
这比"单次输出大小"更能解释成本：本次一条 4,023 字符的 `process_wait` 超时消息活了
**125 次调用**，累计 231.6k token；一条 1,292 字符的 `read_memory` 回执活了 **158 次**。

---

## 4. 任务线

| 段 | 时间 / 活跃 | 模型调用 | 工具调用 | 审批 | 峰值上下文 | prompt token | 做了什么 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| turn 1 | 13:49–14:30 / **40m27s** | 34 | 47 | 27 | 77,472 | 1.14M | 克隆仓库、`uv sync`、开始下载 IndexTTS-2.5 权重 |
| turn 2 | 14:30–15:02 / **32m28s** | 43 | 66 | 42 | 112,070 | 3.06M | 停下载、把 C 盘 Python 缓存/ffmpeg/Temp 迁到 D 盘 |
| turn 3 | 02:19–02:49 / **29m44s** | 58 | 84 | 46 | **176,250** | **5.46M** | 补齐辅助模型 + 示例音频、跑通最小推理、更新上手指南 |
| 粘贴段 | 02:51:53 / **0.7s** | **0** | 0 | 0 | 156,099 → 35,206 | 0 | 26 行终端日志被拆成 26 条消息；前 25 条空转收口 |
| turn 29 | 02:51–02:58 / **6m34s** | 24 | 29 | 14 | 64,540 | 1.24M | 定位 Gradio 502，给 `webui.py` 打 NO_PROXY 补丁并验证 |

### 4.1 四段任务的关键情节

- **turn 1 没有最终答复**：用户在 14:30:23 发「先不着急下模型了」，当时 agent 正卡在一条
  5 分钟的 `process_wait` 上。工具返回后，`llm_node` 在入口 peek 到信箱有货，直接空转
  收口（step 188 的 `is_inject`），turn 1 结束、turn 2 以这条消息开局。**steering 正常，
  但时延等于在途批次。**
- **turn 2 的 `ask_user` 避免了错误范围的迁移**：模型给出 3 个迁移范围选项，用户选了第 3 个
  （连 Temp 一起处理）；回答写入 `ask_answers`，下一轮由 `QueueNode` 清空。
- **turn 3 是成本大头**：58 次模型调用、84 次工具、上下文涨到 176k 字符，单轮 prompt
  5.46M token，占全会话 50%。
- **26 行粘贴 = 25 次零模型调用**：每一行都成了一个用户消息/turn；前 25 个 turn 在
  `llm_node` 入口看到信箱还有后续行，直接收口，只有最后一行真正调用模型。副作用：第一行
  触发了 turn 3 的轮末折叠，把上下文从 156k 降到 35k，让最后的排障轮轻装开局。
- **最后的排障准确**：Gradio `launch()` 的 localhost 自检被 Windows 系统代理（7892）
  拦成 502；模型查了注册表、`urllib.getproxies()`、httpx 路由表，确认 Python 不读
  `ProxyOverride`，然后在 `webui.py` 顶部幂等补上
  `NO_PROXY=localhost,127.0.0.1,::1`，用无 NO_PROXY 的环境启动并验证
  `/gradio_api/startup-events` 返回 200。

---

## 5. 工具与审批

### 5.1 工具计数

| 工具 | 次数 | 单次输出合计（字符） | 最大单条 | 说明 |
| --- | ---: | ---: | ---: | --- |
| `run_command` | **94** | 66,749 | 6,426 | 环境探测、克隆、uv/pip、迁移、下载、验证 |
| `update_plan_step` | 28 | 10,801 | 469 | 每一步 in_progress + done |
| `read_file` | 24 | 105,503 | 11,008 | 最大单条 11k |
| `edit_file` | 20 | 1,124 | 66 | 大部分是上手指南与 5 个自检脚本 |
| `search_content` | 11 | 35,418 | 17,229 | 最大单条 17k |
| `write_memory` | 8 | 3,635 | 617 | m1/m2/m4/m5/m6 的多轮覆写与追加 |
| `start_process` | 5 | 15,600 | 7,066 | 下载、WebUI 常驻 |
| `process_read` | 5 | 1,287 | 501 | 进程增量输出 |
| `process_wait` | 4 | 8,649 | 4,025 | 超时/等待回执 |
| `process_kill` | 4 | 471 | 141 | 终止进程 |
| `create_plan` | 3 | 1,022 | 458 | 计划生命周期 |
| `process_list` | 3 | 27 | 9 | 进程列表 |
| `clear_plan` | 2 | 14 | 7 | 清空计划 |
| `read_memory` | 1 | 1,292 | 1,292 | 开场读记忆 |
| `ask_user` | 1 | 64 | 64 | 迁移范围 |
| `create_file` | 1 | 17 | 17 | 创建 `上手指南.md` |
| `get_directory_tree` | 1 | 2,952 | 2,952 | 仓库结构 |
| `glob` | 1 | 58 | 58 | 文件定位 |
| `list_dir` | 5 | 1,222 | 531 | 目录列表 |
| `write_file` | 5 | 247 | 52 | 5 个自检脚本 |

### 5.2 审批与人工等待

- 128 次工具审批中 **127 通过、1 拒绝**；唯一拒绝是
  `netstat -ano | findstr ":7860"`，模型没有重试，改用 PowerShell 查进程。
- 人工等待合计 **2,721.6 秒 = 45 分 22 秒**，占活跃时间的 **41.5%**。
- 中位数 **5.0 秒**；63 次 ≤5 s、35 次 5–30 s、**31 次 >30 s**；最长 **253.6 秒**。
- 按 turn 分：turn 1 审批 27 次、turn 2 41 次 + 1 次提问、turn 3 46 次、最后 13 次 + 1 次拒绝。

`ask_user` 的参数与回答：

> **why**：C 盘上除本次任务产生的下载物外，还有历史遗留的 pip 缓存（4.3 GB）和临时目录
> （1.8 GB），迁移范围不同工作量与风险差别较大，需要你定范围。
>
> **question**：要把 C 盘哪些下载/缓存内容迁到 D 盘？
>
> **用户选择**：3. 连临时目录 Temp（1.8G）也一起处理：迁上面全部，并清空可安全删除的临时文件。

### 5.3 全部非 ok 事件（17 failed + 1 denied）

| 时间 (UTC) | 工具 | 状态 | 命令/参数（截断） |
| --- | --- | --- | --- |
| 2026-10-26T13:54:19 | `run_command` | failed | `git --version; uv --version; nvidia-smi; python --version; echo "--- disk ---"; wmic logicaldisk get name,freespace,size 2>nul` |
| 2026-10-26T13:54:25 | `run_command` | failed | `pwd && echo "shell ok"` |
| 2026-10-26T13:54:32 | `run_command` | failed | `nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv & echo --- & wmic logicaldisk get caption,freespace,size` |
| 2026-10-26T13:57:15 | `run_command` | failed | `where nvidia-smi & nvidia-smi & echo === PROXY TEST === & curl -x http://127.0.0.1:7892 ...` |
| 2026-10-26T14:09:16 | `run_command` | failed | `cd index-tts & where ffmpeg & ffmpeg -version 2>&1 \| findstr /i "ffmpeg version" & echo === GPU CHECK === & uv run tools/gpu_check.py 2>&1` |
| 2026-10-26T14:24:17 | `run_command` | failed | `cd index-tts\.venv\Scripts && dir /b modelscope* hf* 2>nul & ...` |
| 2026-10-26T14:33:27 | `run_command` | failed | `robocopy "C:\Users\25972\AppData\Local\uv\cache" "D:\DevCache\uv\cache" /E /MOVE /MT:16 ...` |
| 2026-10-26T14:46:41 | `run_command` | failed | `set "UV_TOOL_DIR=..." & set "UV_CACHE_DIR=..." & uv tool list & ...` |
| 2026-10-26T14:54:07 | `run_command` | failed | `modelscope --version 2>&1 & echo --- & ms --version 2>&1 & ...` |
| 2026-10-27T02:20:01 | `search_content` | failed | `path=index-tts/webui.py`（传文件路径） |
| 2026-10-27T02:20:08 | `search_content` | failed | `path=index-tts/indextts/infer_v2_5.py`（传文件路径） |
| 2026-10-27T02:25:33 | `process_wait` | failed | `process_id=p2`（等待失败） |
| 2026-10-27T02:25:50 | `run_command` | failed | `nslookup hf-mirror.com ... & nslookup huggingface.co ... & nslookup www.modelscope.cn ...` |
| 2026-10-27T02:34:52 | `run_command` | failed | `ffprobe.exe -v error -show_entries ... out\smoke_zh.wav` |
| 2026-10-27T02:39:05 | `start_process` | failed | `cd /d ... & set "HTTP_PROXY=" & set "HTTPS_PROXY=" & uv run webui.py --port 7860`（502 启动崩溃） |
| 2026-10-27T02:52:10 | `run_command` | **denied** | `netstat -ano \| findstr ":7860"`（人工拒绝） |
| 2026-10-27T02:52:55 | `run_command` | failed | `set HTTP_PROXY & set HTTPS_PROXY & set NO_PROXY`（未定义变量导致 exit 1） |
| 2026-10-27T02:56:51 | `search_content` | failed | `path=上手指南.md`（传文件路径） |

失败分类：

- **Windows cmd 语法**：`;`、`&&`、`pwd` 在 cmd 里不可用；模型后续改用 `&`、`cd /d`。
- **非零退出码但命令成功**：`robocopy`（1/3 是成功）、`findstr` 无匹配、`set` 未定义变量
  都被统一判成 `tool_error`。
- **工具边界**：`search_content` 3 次全部是传文件路径，与 2026-10-18 会话同构。
- **真实环境问题**：`nvidia-smi` NVML 失败、ffmpeg 未装、`hf-mirror.com` 解析到 `0.0.0.0`、
  WebUI 502；模型都自愈了。

---

## 6. 时间画像

| 节点 | 耗时 | 占活跃 |
| --- | ---: | ---: |
| 工具执行（下载、安装、迁移、验证） | **3,270.9 s（54m31s）** | 49.9% |
| 人工审批等待 | **2,721.6 s（45m22s）** | 41.5% |
| 模型推理 | **561.0 s（9m21s）** | 8.6% |
| 合计 | 6,553.9 s（1h49m14s） | 100% |

模型不是瓶颈；人的审批和机器的下载/迁移占掉了九成时间。审批量从 2026-10-18 案例的
19 次涨到 128 次，人工等待的绝对成本显著放大。

---

## 7. 上下文与折叠

### 7.1 每轮上下文

| 轮 | 峰值 chars | 轮末 chars | 发生了什么 |
| --- | ---: | ---: | --- |
| turn 1 | 77,472 | 77,472 | 单轮，无可折块 |
| turn 2 | 112,070 | 44,346 | 轮末折叠 turn 1 |
| turn 3 | **176,250** | 156,037 | 轮末只折掉 turn 2；**当前轮 58 个工具结果全部留在上下文里** |
| 粘贴第 1 行 | 156,099 | 35,206 | 新 Human 出现后，turn 3 终于被折掉 |
| turn 29 | 64,540 | 64,540 | 从 35k 轻装开局，涨到 64.5k |

**轮末折叠确实在工作**：3 次有效折叠把 226 条工具结果中的 **154 条**折掉，最终只剩
72 条 ToolMessage。但它只折"最后一条 HumanMessage 之前"的旧轮；**当前轮无论多长都不折**。
turn 3 因此独自背了 176k 字符、58 次模型调用、5.46M prompt token。

29 次 `compact_node` 中：

- **3 次有效**：turn 2 末折 turn 1（摘要 1）、turn 3 末折 turn 2（摘要 2）、粘贴第 1 行折
  turn 3（摘要 3）；
- **26 次空操作**：turn 1 末（只有一条 Human，无可折区）+ 25 个粘贴空转 turn。

### 7.2 最终上下文构成

| 来源 | 字符 | 占比 |
| --- | ---: | ---: |
| ToolMessage（72 条） | 42,930 | 66.5% |
| HumanMessage（29 条，含 26 行粘贴） | 10,578 | 16.4% |
| AIMessage（53 条） | 6,640 | 10.3% |
| SystemMessage（3 条折叠摘要） | 4,392 | 6.8% |
| **合计** | **64,540** | 100% |

其中计划类回执（28 条 `update_plan_step` + 3 条 `create_plan` + 2 条 `clear_plan`）合计
**11,837 字符，占最终上下文的 18.3%**；折叠摘要只有 40 / 45 / 72 行，内容是
`工具调用: xxx | 状态: ok`，没有命令、路径、URL、退出码或错误详情。

---

## 8. Token 归因（核心实验）

### 8.1 按消息大类

| 大类 | token | 占比 |
| --- | ---: | ---: |
| **稳定前缀**（系统提示 + 工具 schema + 工作区/画像） | 2.544M | **23.3%** |
| **普通工具结果**（ToolMessage，不含编排工具） | 5.095M | **46.7%** |
| **普通工具调用参数**（AIMessage 的 tool_calls args） | 1.600M | **14.7%** |
| **状态通道**（plan / memory / ask 回执 + RUNTIME_STATE） | 1.430M | **13.1%** |
| AI 正文 | 0.117M | 1.1% |
| 折叠摘要 | 0.085M | 0.8% |
| 用户正文（不含 RUNTIME_STATE） | 0.034M | **0.3%** |
| **合计** | **10.90M** | 100% |

结论：

- **普通工具结果 + 普通工具参数 = 61.4%**；若把状态通道（plan/memory 回执）也算进工具载荷，
  合计 **74.5%**。真正贵的是工具"说"和"被重放"。
- **稳定前缀 23.3%**：每次调用固定 16k，159 次 = 2.54M。`SYSTEM_PROMPT` 本身 12,233 字符
  （约 6k token），其余大头是工具 schema。
- **状态通道 13.1%** 比 `search_content`（5.7%）还大，接近 `read_file`（18.1%）的 2/3。
  它只有 3 个 plan、8 条 memory、1 次 ask，外加 RUNTIME_STATE——体积小，但寿命极长。
- **用户真正写的字只占 0.3%**。

### 8.2 按工具（结果 + 参数）

| 工具 | 调用数 | 单次输出合计 | 累计正文 chars | 寿命乘数* | 累计 token（结果+参数） | 占比 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `run_command` | 94 | 66,749 | 3,707,550 | 55.5× | **2.869M** | **26.3%** |
| `read_file` | 24 | 105,503 | 4,205,321 | 39.9× | **1.972M** | **18.1%** |
| `update_plan_step` | 28 | 10,801 | 1,088,143 | **100.7×** | 0.656M | 6.0% |
| `search_content` | 11 | 35,418 | 1,302,479 | 36.8× | 0.619M | 5.7% |
| `process_wait` | 4 | 8,649 | 719,400 | 83.2× | 0.341M | 3.1% |
| `start_process` | 5 | 15,600 | 675,209 | 43.3× | 0.335M | 3.1% |
| `write_file` | 5 | 247 | 9,796 | 39.7× | 0.182M | 1.7% |
| `write_memory` | 8 | 3,635 | 177,111 | 48.7× | 0.176M | 1.6% |
| `create_plan` | 3 | 1,022 | 122,523 | **119.9×** | 0.120M | 1.1% |
| `create_file` | 1 | 17 | 1,764 | 103.8× | 0.116M | 1.1% |
| `read_memory` | 1 | 1,292 | 205,874 | **159.3×** | 0.104M | 1.0% |
| `edit_file` | 20 | 1,124 | 20,262 | 18.0× | 0.083M | 0.8% |
| `get_directory_tree` | 1 | 2,952 | 169,290 | 57.3× | 0.078M | 0.7% |
| `list_dir` | 5 | 1,222 | 72,446 | 59.3× | 0.044M | 0.4% |
| `ask_user` | 1 | 64 | 8,784 | 137.3× | 0.030M | 0.3% |
| `process_read` | 5 | 1,287 | 50,539 | 39.3× | 0.030M | 0.3% |
| `process_kill` | 4 | 471 | 25,905 | 55.0× | 0.019M | 0.2% |
| `clear_plan` | 2 | 14 | 1,836 | 131.1× | 0.005M | 0.0% |
| `glob` | 1 | 58 | 2,232 | 38.5× | 0.003M | 0.0% |
| `process_list` | 3 | 27 | 1,323 | 49.0× | 0.003M | 0.0% |

\* 寿命乘数 = 累计正文 chars / 单次输出合计；表示"同一条输出正文被重放了多少倍"。
token 列按消息实际成本（content + name + tool_call_id + tool_calls 参数）归因，因此与
正文乘数不完全成比例。`write_file` / `create_file` / `edit_file` 的 token 主要来自
AI tool_calls 参数，而不是结果。

**Top 4 工具 + 稳定前缀 = 79.4%**；Top 6 = 85.7%。

### 8.3 状态通道明细

| 项 | token | 占比 | 说明 |
| --- | ---: | ---: | --- |
| `update_plan_step`（结果 + 参数） | 0.656M | 6.0% | 28 条回执，每条回显完整计划快照，寿命 100× |
| `write_memory`（结果 + 参数） | 0.176M | 1.6% | 8 条，回执带完整正文 |
| `create_plan`（结果 + 参数） | 0.120M | 1.1% | 3 条，寿命 120× |
| `read_memory`（结果 + 参数） | 0.104M | 1.0% | 1 条，寿命 159×；内容与 RUNTIME_STATE 重复 |
| `ask_user`（结果 + 参数） | 0.030M | 0.3% | 1 条，寿命 137× |
| `clear_plan`（结果 + 参数） | 0.005M | 0.0% | 2 条 |
| **RUNTIME_STATE**（Human 内联块） | 0.339M | 3.1% | 计划/记忆/技能状态，每次调用重发 |
| **合计** | **1.430M** | **13.1%** | — |

状态通道的特点是**单条很小、寿命极长**：编排工具被 `CompactNode` 刻意排除在折叠之外，
因此从产生到会话结束一直存在。`read_memory` 只读了一次 1,292 字符，却出现在 158 次调用里。

### 8.4 Top 消息（按累计 token）

| 累计 token | 出现次数 | 字符 | 类型 | 工具/名称 | 预览 |
| ---: | ---: | ---: | --- | --- | --- |
| 263.5k | 54 | 11,008 | ToolMessage | `read_file` | `model_download.py` 全文（第一次） |
| 235.2k | 65 | 7,846 | ToolMessage | `read_file` | `README.md` 第 26–200 行 |
| 231.6k | 125 | 4,023 | ToolMessage | `process_wait` | `p25` 超时：仍在运行 + 完整命令 |
| 222.0k | 45 | 11,008 | ToolMessage | `read_file` | `model_download.py` 全文（第二次） |
| 195.4k | 56 | 7,870 | ToolMessage | `search_content` | "命中过多，仅显示前 80 条" |
| 184.6k | 24 | 17,229 | ToolMessage | `search_content` | 前 30 条命中 + 上下文 3 行 |
| 180.8k | 56 | 7,066 | ToolMessage | `start_process` | `p18` 常驻：完整命令 + 状态 |
| 171.8k | 61 | 6,118 | ToolMessage | `run_command` | cpython 下载日志 |
| 169.8k | 94 | 4,026 | ToolMessage | `run_command` | robocopy 错误 5 |
| 146.8k | 67 | 4,721 | ToolMessage | `read_file` | `pyproject.toml` |
| 127.7k | 159 | 1,695 | HumanMessage | — | turn 1 用户请求 + 旧 RUNTIME_STATE |
| 114.9k | 62 | 4,023 | ToolMessage | `read_file` | `README2.5_ZH.md` 第 120–300 行 |
| 111.9k | 31 | 8,101 | ToolMessage | `read_file` | `infer_v2_5.py` 第 100–260 行 |
| 110.2k | 55 | 4,390 | ToolMessage | `read_file` | `webui.py` 第 1–100 行 |
| 109.4k | 45 | 5,425 | ToolMessage | `read_file` | `examples_downloader.py`（第一次） |
| 105.5k | 125 | 1,825 | HumanMessage | — | turn 2 请求 + 旧 RUNTIME_STATE |
| 97.0k | 158 | 1,292 | ToolMessage | `read_memory` | 开场 4 条记忆全文 |
| 93.1k | 51 | 4,110 | ToolMessage | `search_content` | 13 条命中 + 上下文 2 行 |
| 88.8k | 82 | 2,319 | AIMessage | — | turn 2 最终答复 |
| 87.0k | 36 | 5,425 | ToolMessage | `read_file` | `examples_downloader.py`（第二次） |
| 85.9k | 48 | 4,023 | ToolMessage | `run_command` | `p2` 超时：一次性转受管 |
| 85.2k | 47 | 4,023 | ToolMessage | `start_process` | `p25` 常驻 |
| 84.3k | 82 | 2,445 | HumanMessage | — | turn 3 请求 + 旧 RUNTIME_STATE |
| 84.2k | 47 | 4,025 | ToolMessage | `process_wait` | ffmpeg 下载进度 + exit 1 |
| 83.5k | 47 | 3,993 | ToolMessage | `search_content` | 5 条命中 + 上下文 4 行 |

两个非常具体的浪费：

1. **同一个 11k 的 `model_download.py` 读了两次**（turn 1 和 turn 3），合计
   **485.5k token = 4.5% 的总量**。turn 1 的内容被折叠后，turn 3 只能重读。
2. **一条 4k 的 `process_wait` 超时消息活了 125 次调用 = 231.6k token（2.1%）**。
   它只是"进程还在跑"的状态，却带着完整命令行和全部输出。

---

## 9. 缓存行为（记录用）

> 这一节记录实测，不作为主要优化目标。按本会话的负载，**90.5% 已接近上限**；
> 99% 不是合理目标（见 9.3）。

### 9.1 总览

| 轮 | prompt | cached | miss | 命中率 | completion |
| --- | ---: | ---: | ---: | ---: | ---: |
| turn 1 | 1,140,136 | 1,040,000 | 100,136 | 91.2% | 12,477 |
| turn 2 | 3,064,469 | 2,718,592 | 345,877 | 88.7% | 27,163 |
| turn 3 | 5,463,746 | 4,923,136 | 540,610 | 90.1% | 30,066 |
| turn 29 | 1,236,210 | 1,189,248 | 46,962 | 96.2% | 11,461 |
| **合计** | **10,904,561** | **9,870,976** | **1,033,585** | **90.5%** | **81,167** |

### 9.2 动态状态原地改写导致 53% 的 miss 是重复失效

`LLMNode` 的 `<RUNTIME_STATE>` 内联在**最后一条 HumanMessage** 里；当模型调用
`update_plan_step` / `create_plan` / `clear_plan` / `write_memory` 后，实现会把新状态
**原地写回那条 HumanMessage**。这条消息位于当前轮开头，一旦变化，它后面的整个当前轮历史
都会失去前缀缓存。

turn 3 的 HumanMessage 内容在轮内确实在变化：

| step | 字符数 | sha1 前缀 |
| --- | ---: | --- |
| 486 | 1,807 | `d4de59cba90a` |
| 500 | 2,131 | `20089549d190` |
| 610 | 2,131 | `b0719329f3c2` |
| 750 | 2,769 | `fdb4f4ebe69d` |

按 `miss_i - max(0, prompt_i - prompt_{i-1})` 估算，**19 次缓存失效事件、合计
544,886 token 的 miss 是"重复失效"，占全部 miss 的 52.7%**。每一次都紧随
`update_plan_step` / `create_plan` / `clear_plan` / `write_memory`：

| 轮 / step | 失效前工具 | prompt | cached | miss | 新增 token | 额外 miss |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| turn3 / 752 | `update_plan_step` | 129,434 | 51,200 | 78,234 | 259 | **77,975** |
| turn3 / 740 | `write_memory` ×2 | 128,548 | 51,456 | 77,092 | 1,555 | **75,537** |
| turn3 / 703 | `update_plan_step` ×2 | 117,898 | 51,200 | 66,698 | 1,225 | **65,473** |
| turn3 / 606 | `update_plan_step` ×3 | 96,219 | 51,072 | 45,147 | 1,662 | **43,485** |
| turn2 / 436 | `clear_plan` | 91,185 | 49,792 | 41,393 | 0 | **41,393** |
| turn2 / 412 | `update_plan_step` ×2 | 88,694 | 50,048 | 38,646 | 3,567 | **35,079** |
| turn1 / 172 | `update_plan_step` ×2 | 49,306 | 16,000 | 33,306 | 853 | **32,453** |

### 9.3 每个新 turn 的第一次调用只能命中稳定前缀

| 轮 | 第一次调用 prompt | cached | miss |
| --- | ---: | ---: | ---: |
| turn 2 | 50,823 | 16,768 | 34,055 |
| turn 3 | 52,040 | 16,768 | 35,272 |
| turn 29 | 40,616 | 22,656 | 17,960 |

这是折叠的固有代价（历史确实被改写）。**命中率上限**：

```text
命中率 = 1 − 新内容 token / 总 prompt token
```

本会话新内容约 0.30–0.35M token，在总 prompt 10.9M 的前提下，上限约 **96–97%**。
要做到 99%，要么总 prompt 做到 3000 万以上（更贵），要么把新内容压到 10 万 token 以内。
**因此不要用 99% 作为 KPI；盯总 prompt 和 miss 两个数。**

### 9.4 账单拆解 `[估算]`

按本次账单反推的等效单价约为 **命中 0.1 / 未命中 1 / 输出 2 元每 M**
（估算 2.18 元 vs 实测 2.06 元）：

| 项 | token | 估算费用 | 占比 |
| --- | ---: | ---: | ---: |
| 缓存重读 | 9.87M | 0.99 元 | 45% |
| 未命中 | 1.03M | 1.03 元 | 47% |
| 输出（含 reasoning） | 81k | 0.16 元 | 7% |
| **合计** | — | **约 2.18 元** | 100% |

**一半是重读缓存，一半是 miss。** 命中率只影响后半；前半由总 token 量决定。

---

## 10. 发现与建议

### P0-1 轮内移动窗口：单点最大收益

当前 `CompressNode` 只做当前轮同一文件重复 `read_file` 的指针化；对 `run_command` 的
下载/安装日志、`search_content` 的大命中、`process_*` 的长回执都没有动作。建议：

- 工作集 = 原始 Human 请求 + `current_plan` + **最近 10–15 个工具结果** + 所有失败/被拒结果；
- 超出预算时，把已消费的旧成功 ToolMessage 的 `content` 换成一行指针
  （`…[已压缩] run_command exit=0 · evt_xxx · read_event_log 可取回`）；
- **只改 content，不删消息**，保持 `tool_call_id` 配对；
- 不压最新一批；用 delta/滞回触发，不要每拍都扫。

按"每条工具结果最多活 15 次调用"估算：

| 工具 | 当前累计字符 | 15 次上限后 | 节省 |
| --- | ---: | ---: | ---: |
| `read_file` | 4.20M | 1.44M | **65.6%** |
| `run_command` | 3.65M | 1.00M | **72.6%** |
| `search_content` | 1.30M | 0.53M | 59.4% |
| `update_plan_step` | 1.05M | 0.16M | **84.5%** |
| `process_wait` | 0.72M | 0.13M | **81.9%** |
| `start_process` | 0.67M | 0.23M | 65.2% |
| `read_memory` | 0.20M | 0.02M | **90.5%** |

> 本表按"消息级正文 + 出现次数"统计，与 §8.2 的"调用级正文"口径差约 2%，
> 不影响比例判断。全部工具结果合计可省 **~2.5–4.0M**（未计模型因看不到而重取的成本）。

### P0-2 状态通道瘦身：约 1.4M → 0.4M

| 项 | 当前 | 做法 | 节省 |
| --- | ---: | --- | ---: |
| `update_plan_step` 回执 | 0.656M | 只回"步骤 N → 状态"，不回完整计划快照（`current_plan` 已每轮注入） | ~0.59M |
| `read_memory` 回执 | 0.104M | 记忆已在 RUNTIME_STATE 注入；回执改成一行"已加载 N 条" | ~0.10M |
| `write_memory` 回执 | 0.176M | 回执只留 `已写入 [mN]`，正文在 memory.md / 注入块里 | ~0.12M |
| 旧 Human 的 RUNTIME_STATE | 0.339M | 每轮只保留最后一条 Human 的状态；旧 Human 只留用户原文 | ~0.24M |
| `create_plan` / `clear_plan` / `ask_user` | 0.155M | 回执同样只留差异/一行 | ~0.10M |

### P1 工具输出上限

- `read_file`：默认上限 6k 字符（现在 11k）；大文件强制行区间。
- `search_content`：总输出上限 6k、最多 20 条命中（现在有 17k / 80 条）。
- `process_wait` / `start_process`：回执压到 300–500 字符（命令 + 最近几行），不要完整
  命令行和全部输出。`p25` + `p18` 可省约 **350k**。
- `run_command`：长命令/复合命令优先落成 `.ps1`/`.bat` 一次执行；历史里留下短命令，
  参数成本从 1.11M 往下掉。
- 终端结果已经截断，但 4–6k 的下载日志仍会活很久；移动窗口会解决大部分。

### P1 稳定前缀与调用数

- 稳定前缀 16k × 159 = 2.54M；调用数降到 130 省 0.48M；schema/提示词从 16k 降到 10k
  省 0.95M；两者叠加约省 1.2M。
- 104 次单工具调用消耗 7.14M prompt（65%）；把相互独立的探测/读取合并成批，目标
  159 → 120–130 次，省 **1.0–1.5M**。

### P2 其他

- **多行粘贴被拆成 N 个 turn**：26 行日志 → 26 条 HumanMessage、26 个 turn；前 25 个
  零模型调用（steering 设计），但用户贴的 traceback 只到 `raise Exception(`，异常正文
  没有进来，模型明确说"你贴的堆栈被截断"。建议 TUI 支持 bracketed paste / 合并连续行。
- **终端退出码语义**：`robocopy`（1/3 是成功）、`findstr` 无匹配、`set` 未定义变量都被
  判成 `tool_error`；建议区分"非零退出但命令成功"。
- **`search_content` 拒收文件路径**：3 次失败全是这个问题，与 2026-10-18 案例同构；
  建议接受文件路径或把错误文案改成"请传目录"。
- **steering 时延被阻塞工具放大**：turn 1 的"先不着急下模型了"在 14:30:23 生效，而它
  可能早在 14:25:23 就已输入；长 `process_wait` 期间也检查信箱，或提示模型用
  `start_process` + 短 `process_wait` 分片。

### 目标

| 指标 | 现在 | 目标 |
| --- | ---: | ---: |
| 单次部署 prompt | 10.90M | **3–5M** |
| 单次平均上下文 | 68.6k | **30–40k** |
| 单次调用数 | 159 | **120–140** |
| miss token | 1.03M | **0.25–0.35M** |
| 费用（按当前价目估算） | **2.06 元** | **0.6–0.9 元** |

---

## 11. 复现方法

只读解码，不需要启动应用。会话目录在 `resource/` 下（运行时生成、gitignore），
以下脚本用仓库 venv 的 Python 执行：

```python
from langgraph.checkpoint.sqlite import SqliteSaver

db = r"E:\code\python\CodingAgent\resource\E-code-python-IndexTTS2_1732f309\sessions\0f9c120dea1442fc84e57fa329ee49b7\agent.db"
tid = "0f9c120dea1442fc84e57fa329ee49b7"

with SqliteSaver.from_conn_string(db) as saver:
    state = saver.get_tuple({"configurable": {"thread_id": tid}}).checkpoint["channel_values"]
    msgs = state["messages"]
    for m in msgs:
        print(type(m).__name__, getattr(m, "name", ""), len(str(m.content)))
        if type(m).__name__ == "AIMessage":
            print(m.response_metadata.get("token_usage"))
```

token 归因的步骤：

1. 从 `checkpoints` 表按 `metadata.step` 取每次 LLM 调用的 checkpoint；
2. 用 `JsonPlusSerializer().loads_typed(("msgpack", checkpoint_blob))` 解出
   `channel_values["messages"]`；
3. 从 `AIMessage.response_metadata.token_usage` 取 `prompt_tokens`；
4. 扣除 16,000 稳定前缀，按消息字符占比分摊；
5. 工具事件与工具名从 `event_log.jsonl` / `ToolMessage.name` 取；
6. 寿命 = 该消息在多少次 LLM 调用的 checkpoint 中出现。

时间归因：把 `checkpoints` 按 `metadata.step` 排序，`ts` 取 `checkpoint["ts"]`，
相邻差即 superstep 耗时；节点类型由该 `checkpoint_id` 在 `writes` 表里的 channel 推断
（规则见 `SESSION_CASE_INDEX_TTS2.md` §3）。

---

## 附录 A：每轮 token

| 轮 | prompt | cached | miss | 命中率 | completion | reasoning |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| turn 1 | 1,140,136 | 1,040,000 | 100,136 | 91.2% | 12,477 | 7,430 |
| turn 2 | 3,064,469 | 2,718,592 | 345,877 | 88.7% | 27,163 | 13,197 |
| turn 3 | 5,463,746 | 4,923,136 | 540,610 | 90.1% | 30,066 | 12,528 |
| turn 29 | 1,236,210 | 1,189,248 | 46,962 | 96.2% | 11,461 | 6,376 |
| **合计** | **10,904,561** | **9,870,976** | **1,033,585** | **90.5%** | **81,167** | **39,531** |

---

## 附录 B：调用分布

| 单次 prompt | 调用数 |
| --- | ---: |
| < 20k | 7 |
| 20–40k | 14 |
| 40–60k | 48 |
| 60–80k | 42 |
| 80–100k | 24 |
| ≥ 100k | 24 |

159 次真实调用中，**138 次（86.8%）带着 ≥40k token，90 次（56.6%）≥60k，24 次 ≥100k**。
单次工具调用 104 次
（65% 的调用），多工具调用 52 次。

---

## 附录 C：与 2026-10-18 会话对照

| 指标 | 2026-10-18（`f215…`） | 本次（`0f9c…`） |
| --- | --- | --- |
| 模型 | `glm-5.3-flash` | `deepseek-flash` |
| 用户轮次 | 1 | 4 段真实 + 26 行粘贴 |
| 真实模型调用 | 23 | **159** |
| 工具调用 | 44 | **226** |
| 审批 | 19 | **128** |
| 最终上下文 | 139,380 字符 | 64,540 字符 |
| 上下文峰值 | 139,380 | **176,250** |
| 累计 prompt | 1.47M 字符（无 token 记录） | **10.90M token** |
| 缓存命中 | 未观测（反推接近 0） | **90.5%** |
| 费用 | 0.47 元 | **2.06 元** |
| 折叠 | 单轮，`compact_node` 空操作 | **3 次有效折叠，折掉 154 条工具结果** |
| 最大单条工具输出 | 41,609 字符 | **6,426 字符**（终端截断生效） |

两次的模型和任务规模不同，费用不能直接比；但可以确定的是：**写入侧截断已见效，缓存也已
生效；下一次收益最大的一刀不是"再压缩"，而是"把当前轮已消费的工具结果遮蔽掉"和
"把状态通道的回执变薄"。**

---

## 相关背景

- `docs/SESSION_CASE_INDEX_TTS2.md`：2026-10-18 单轮长任务案例（同一工作区）。
- `docs/CONTEXT_COMPRESSION_SURVEY.md`：分级压缩的外部实践与当前缺口。
- `docs/CONTEXT_ENGINEERING.md`：消息留存 / 折叠 / notes 的设计目标。
- `docs/TODO.md`「运行中 compact」「上下文前缀缓存 + 低频 checkpoint」条目。
- 代码落点：`app/agent/nodes.py::LLMNode`、`CompressNode`、`CompactNode`；
  `app/agent/prompt.py` 的 `RUNTIME_STATE_*`；`app/resource/event_log.py`；
  `mcp_service/terminal.py::_compress_output`；`mcp_service/file_io.py` 的
  `read_file` / `search_content`。
