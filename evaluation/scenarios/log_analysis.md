# 典型场景：日志分析（`log_analysis`）

> 这是评估框架从"单点文件读写"升级到**典型编码任务**的第一个场景。它同时覆盖：
> 终端只读命令、少量文件写入、审批闸门、失败/空结果语义、以及一条可回放的轨迹。
> 任务定义在 `evaluation/tasks.py`，轨迹录在 `evaluation/fixtures/log_analysis.json`。

## 场景

工作区预置一份 1,200 行的 `logs/app.log`：

- 每 40 行一条 `ERROR`（共 30 条）；
- 每 17 行一条 `WARN`；
- 其余 `INFO`；
- **没有 `FATAL`**。

用户要求模型：

1. 统计总行数；
2. 统计 `ERROR` 行数；
3. 统计 `FATAL` 行数（没有就写 0）；
4. 找出最后 3 条 `ERROR` 的原文；
5. 把结果写入 `logs/summary.md`；
6. 日志较大，优先用终端只读命令统计，不要整份读入。

## 为什么选它

- **典型**：读日志 → 统计 → 定位少数关键行 → 写摘要，是编码 agent 最常见的任务形状之一；
- **有失败语义**：`grep -c FATAL` 在零命中时退出码为 1，但语义是"0 条"而不是"命令坏了"。
  这正是"终端运行错误识别"要区分的机械失败与业务空结果；
- **有审批**：`run_command` 与 `create_file` 都要过闸门，能测审批次数与拒绝路径；
- **有上下文压力**：1200 行日志若整份读入约几万字符，足够触发后续的输出整形/压缩评估；
- **安全**：只读命令白名单（`grep / wc / tail / head / cat / find / ls / stat` 等），
  重定向与 `rm / mv / cp / sudo` 一律拒绝；工作区是每任务独立的临时目录。

## 轨迹（层 A 回放）

`--record` 会把模型的每一次响应写进 `evaluation/fixtures/log_analysis.json`；层 A 回放时
模型换成脚本，图、MCP、工具、审批、检查仍然全真。回放断言：

| 断言 | 说明 |
| --- | --- |
| `file_exists(logs/summary.md)` | 交付物存在 |
| `file_contains(..., rid=1120, rid=1160, rid=1200)` | 最后三条 ERROR 按原文写入 |
| `file_matches(..., 总行数 1200)` | 总行数正确（格式宽松） |
| `file_matches(..., ERROR 30)` | ERROR 计数正确 |
| `file_matches(..., FATAL 0)` | 零命中的业务语义正确 |
| `tool_called(run_command)` | 场景要求用终端统计，而不是整份读文件 |
| `tool_call_count_between(run_command, 2, 5)` | 有失败恢复，但没有反复重试 |
| `failed_tool_seen(run_command)` | 本轨迹包含"Unix 工具缺失 → PowerShell"的恢复 |
| `approvals_between(1, 15)` | 审批次数在合理区间 |
| `terminated()` | 正常收尾 |

`EvalResult.trace` 还会记录每一轮的 `ai_tool_calls` / `tool_result`（工具名、参数、
状态、正文前 500 字符），后续可以在此基础上加"先 grep 后 tail""失败命令之后模型是否恢复"
这类轨迹断言。

## 已录轨迹（2026-10-18）

`--record` 的实际轨迹（5 轮、4 次审批、174 s）：

1. `list_dir(".")`；
2. `run_command: wc -l ...; head ...; tail ...` → **exit_code 1**（Windows cmd 没有
   Unix 工具）；
3. `run_command: powershell ... [System.IO.File]::ReadLines ...` → 成功；
4. `run_command: powershell ...` → 复核最后 3 条 ERROR；
5. `write_file logs/summary.md`；
6. 最终答复。

层 A 回放 **10/10，3.5 s**。这条轨迹本身就是"终端失败 → 恢复"的样本；
`EvalResult.trace` 会把每一步保留成结构化事件（`ai_tool_calls` / `tool_result`）。
`--trace` 可以在报告后打印它。

## 运行

```bash
# 录制轨迹（层 B，花 token，会走真实模型与真实工具）
python -m evaluation --record --task log_analysis

# 回放（层 A，零 token）
python -m evaluation --task log_analysis

# 回放并打印结构化轨迹
python -m evaluation --task log_analysis --trace
```

改动 prompt / 日志预置内容会让 fixture 指纹失效（SKIP），需要重新 `--record`。

## 安全边界

- 终端策略是 `policy.allow_readonly_terminal()`：只按"每段首个词"批准只读命令；
  含 `>` / `<` / 反引号 / `$(` / `sudo` / `rm` / `mv` / `cp` 的片段直接拒绝；
- 工作区由 `run_suite` 建在系统临时目录，任务结束（报告之后）统一清理；
- 联网工具仍被拒绝；不访问网络。
