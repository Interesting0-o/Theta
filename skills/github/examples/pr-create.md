# pr-create：一次完整的"分批提交 → 推送 → 开 PR"

> 真实会话复盘（2026-09-15，分支 `feature/rebuild`）。**已匿名化**：项目与仓库均为虚构指代——
> 项目曾叫 `orion`、现叫 `lyra`；仓库曾用名 `someone/orion-agent`、现名 `someone/lyra-agent`。
> 定位：SKILL.md §2 各条约束的**实证样本**——工具序列、审批落点、踩坑与自愈都如实保留，
> 以后走同一条路时对照着用。
>
> 本文**不随技能注入**（`read_body` 只读 SKILL.md）：它供维护者复盘对照；当工作区就是本仓库时，
> 模型也可以按需读它。

## 任务与起点

用户一句话："分批次按照主题提交当前工作区，然后提交一个pr到云端。"

起点状态：

- 工作树 **26 个文件未提交**，横跨三个主题（核心引擎修复批 / 项目更名 orion→lyra / 文档同步）；
- 分支 `feature/rebuild`，远端 origin 在 `.git/config` 里还叫旧名 `someone/orion-agent`；
- 同分支的旧 PR #2 已于两个月前合并关闭——**开 PR 前模型并不知道这件事**，是侦察出来的。

## 流程总览（审批 20 次，全部通过）

| 阶段 | 动作 | 审批 |
| --- | --- | --- |
| 1 规划 | `create_plan`（4 步：审查→分批提交→推送→开 PR） | 免审 |
| 2 审查 | `git diff` ×5 批按目录看完全部改动 + `read_file(.git/config)` 拿远程地址 | 免审 |
| 3 体检 | `run_command` 跑全套测试 → 全部通过（期间顺手处理了 `.venv` 损坏：一个 Linux 侧遗留的坏符号链接，与本技能无关，略） | 审 |
| 4 提交 | `git add` ×4 + `git commit` ×4，按主题切四个提交（见下） | 审 |
| 5 推送 | `git push <显式 URL> feature/rebuild` | 审 |
| 6 开 PR | `get_skill` → 侦察 → 合并性检查 → `github_pr_create`（307 → 改名重试）→ 验证 | `get_skill` 免审；`pr_create` 审 |
| 7 收尾 | `drop_skill` + `write_memory`（改名事实）+ `clear_plan` | 免审 |

主题切分（§2.2 第 2 条"一个提交一件事"）：`fix(engine)` 核心修复批（13 文件）→
`refactor` 项目更名（4）→ `docs`（9）→ `chore` 本地配置（1）。

## 开 PR 的关键序列

1. **`get_skill github`**：回执自带"凭证体检：GITHUB_TOKEN 可用（HTTP 200）"——按 §3，体检
   不用你主动去查，结论直接采信即可。
2. **先侦察再动手**（§2.2 第 11 条）：`git fetch`（拿远端最新）+ `github_repo_view`
   （确认默认分支是 `master`）+ `github_pr_view #2`——发现**同分支旧 PR 已合并关闭**，
   结论：同分支再次合并必须开**新 PR**。
3. **合并性检查**（§2.2 第 5 条——检查是必须的，"把 master 合进来"只在真分叉时才需要）：
   - `git log <branch>..origin/master` → master 只多两个 PR 合并提交；
   - `git merge-base` → 分叉点记为 `M`；
   - `git diff --stat origin/master M` → **空**：master 与分叉点树完全一致，分支严格领先
     （11 个提交）→ PR 可自动合并，无需先同步 master。
4. `github_pr_create`（base=master, head=`feature/rebuild`，旧仓库名）→ **失败，307**，见下一节。
5. 用新仓库名重试 → **新 PR 创建成功**，回执带"合并由人来做——把这个链接给用户，别自己去合"。
6. `github_pr_view` 复核：open、可自动合并 True、改动 59 个文件 → 链接交给用户
   （§2.2 第 9 条：不催合并）。
7. 收尾：`drop_skill` 卸载 → `write_memory` 把改名事实写进工作区长期记忆 → `clear_plan`。

## 踩坑实录：307 Moved Permanently

- **现象**：`github_pr_create` 用 `.git/config` 里的旧名 `someone/orion-agent` 调用，GitHub 返回
  307；回执 `[upstream_error]` 里带着 GitHub 自己给的提示（重定向后的目标 url）。
- **根因**：仓库已改名 `orion-agent → lyra-agent`。**读工具自动跟随重定向**（`repo_view`
  传旧名拿到的已经是新名），**写工具不跟随**——同名调用"读得通、写不通"，极具迷惑性。
- **处置**：`repo_view`（新名）确认可达 → 用新名重试 `github_pr_create` → 成功。两轮内自愈。
- **沉淀**：改名事实写入长期记忆（m1）："本地 `.git/config` 的 origin URL 仍指向旧地址
  （git 层 push 会跟随重定向成功），但 GitHub API 写调用必须用新名，否则 307。"

## 约束执行对照表

| SKILL.md 条款 | 这次会话中的体现 |
| --- | --- |
| §2.1 合并不是你的动作 | 回执明示"合并由人来做"，最终回复只给链接与"可自动合并"状态 |
| §2.2 第 4 条 地址不凭记忆拼 | `git push` 的 URL 取自 `.git/config` 的 origin（用户配置过的既有 remote） |
| §2.2 第 5 条 开 PR 前先对齐主分支 | 完整走了"fetch → 对比 → 树一致 → 免合入"的检查，而不是直接开 PR |
| §2.2 第 6 条 描述三件事 | PR 正文齐备：动机（分主题列改动）/ 验证（全套测试结果）/ 给 reviewer 的遗留说明 |
| §2.2 第 9 条 不催合并 | 结束语只有 PR 链接，合并决策留给用户 |
| §3 凭证 | 全程采信体检结论，没有打印或引用 token |

> 复盘数据来源：该会话的 checkpoint（`resource/<ws_key>/sessions/<sid>/agent.db`）。
