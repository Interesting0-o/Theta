---
name: github
description: GitHub 平台操作（PR / issue / 分支 / 不克隆读远端代码 / 搜仓库）。当任务涉及 GitHub 上的仓库——查看 issue、开或审 PR、推送本地提交、搜项目——时使用。
---

# GitHub Skill

> **本文件是"这个领域"的单一来源**：能力清单 + 软约束全文 + 硬约束的声明。
> 由 **host 侧**（`get_skill`）读取并注入系统提示；**`server.py` 不读它，也不负责提供约束文本**
> （见 [[docs/SKILL_DESIGN]] §2.2 的不变量）。**不要在代码里另抄一份约束文本。**
>
> 状态：**只读子集已落地**（2026-09-12）——能力型通道已通（[[docs/SKILL_DESIGN]] §11/§13）：
> `get_skill("github")` 会读本文注入系统提示，**并把 `server.py` 拉起来**、把 §1.1 里标 ✅ 的工具
> 加进本会话；`drop_skill` 卸下时一并关掉。
>
> **尚未实现**：§1.2 的**写操作全部**（开 PR / 推送 / 合并 / 评论），以及 §1.1 里标 ⏳ 的几个只读
> 工具（issue/PR 列表、diff、files、branch、release）。加载后若发现某个工具不在可用工具表里，
> 就是它还没实现——**不要照着本文的工具名硬调**。
>
> **阅读顺序**：§2.1 硬约束是**结构性**的（不是你该不该遵守的问题，是系统根本不给通路），
> §2.2 软约束是你**应当遵守**的行动指导。两者都不是建议。

---

## 1. 能力清单

### 1.1 只读（免审批）

| 工具 | 作用 | 状态 |
| --- | --- | --- |
| `github_auth_status` | 确认当前 token 身份与可用配额（诊断"为什么没权限"先调它） | ✅ |
| `github_repo_view` | 仓库元信息：描述 / 默认分支 / 语言 / star / 开放 issue 数 | ✅ |
| `github_tree` | 列**远端**目录树（`ref` 可指分支/tag/commit）——**不克隆** | ✅ |
| `github_file_read` | 读**远端**单个文件（或列目录）——**不克隆** | ✅ |
| `github_search_repos` | 按关键词搜仓库 | ✅ |
| `github_search_code` | 搜代码（可限 `repo` / `language`） | ✅ |
| `github_issue_view` | 读单个 issue（含正文 / 标签 / 评论数） | ✅ |
| `github_pr_view` | 读单个 PR（状态 / 分支 / 增删行数 / 正文） | ✅ |
| `github_issue_list` | 列 issue | ⏳ 未实现 |
| `github_pr_list` | 列 PR | ⏳ 未实现 |
| `github_pr_diff` / `github_pr_files` | 读 PR 的 diff / 改动文件清单 | ⏳ 未实现 |
| `github_branch_list` | 列**远端**分支 | ⏳ 未实现 |
| `github_release_list` | 列 release / 版本（"这个库现在什么版本"） | ⏳ 未实现 |

> **`github_tree` + `github_file_read` 是"不克隆读代码"的主力**：只想看一个文件、一层目录时用它们，
> 不要为了看一眼去 `git clone` 整个仓库（慢、占工作区、还可能触发审批）。

### 1.2 写 / 远端改动（需人工审批）

| 工具 | 作用 | 审批 |
| --- | --- | --- |
| `github_branch_create` | 在远端创建分支 | 需审 |
| `github_push` | 把**本地已提交**的改动推到远端分支 | 需审（且见 §2.1 的结构性拒绝） |
| `github_pr_create` | 开 PR | 需审 |
| `github_pr_review` | 提交评审意见 | 需审（且见 §2.1 的事件白名单） |
| `github_pr_merge` | 合并 PR | 需审——**合并权留给人** |
| `github_issue_comment` | 在 issue / PR 下留言 | 需审 |

### 1.3 明确**不做**（归别的工具，别重复实现）

- **本地 git 操作**（`status` / `diff` / `log` / `add` / `commit` / 切分支）→ 已有 `mcp_service/git.py`。
  GitHub skill **只管平台侧**；"本地提交"永远走 `git_commit`，本 skill 不重复造。
- **克隆仓库** → 用 `run_command` 的 `git clone`，或直接用 §1.1 的远端读工具（**优先后者**）。
- **任意 HTTP 打 GitHub API** → 用上面的工具；不要 `run_command` + `curl` 绕过去（那样没有审批粒度、
  错误语义也丢了）。

---

## 2. 约束

### 2.1 硬约束（**结构性**——系统不给通路，不依赖你自觉）

| 约束 | 落点 | 说明 |
| --- | --- | --- |
| **不许直接推受保护分支**（`main` / `master` / 仓库配置的 protected branches） | **工具内直接拒绝** | `github_push` 判定目标是受保护分支即拒绝，**不进入审批**（同 `mcp_service/terminal.py::_deny_sudo` 的先例）。改动进主干**只能**经"开分支 → 推分支 → 开 PR → 人合并" |
| **合并 PR 必须人批** | `tool.json`: `github_pr_merge: need_review: true` | 模型可以**建议**合并，但按下合并键的必须是人 |
| **你不得"批准"PR** | **工具内事件白名单** | `github_pr_review` 只接受 `COMMENT` / `REQUEST_CHANGES`；`APPROVE` 被直接拒绝——"批准"与"合并"一样是人的权力，不是你的 |
| **推送 / 建分支 / 开 PR / 留言需人批** | `tool.json` 各登记 `need_review: true` | 见 §3 登记表 |

> **命令含 `sudo` 的先例说明这条怎么落地**：那是"结构性拒绝"——工具在动手前直接 `raise`，
> 连问都不问人。凡属"这个领域一律不该发生"的，都照此办理；凡属"可能对但需人点头"的，走审批闸门。

### 2.2 软约束（行动指导——应当遵守）

**分支与提交**

1. **先开分支，再动手。** 任何要落地的改动，第一步是 `github_branch_create` 开一个语义化名字的
   分支（如 `fix/issue-42-null-deref`），**不要**在 `main` 上直接改。
2. **小步提交，一个提交一件事。** 本地 `git_commit` 的粒度按"一个可独立解释的改动"切，别把
   三件不相关的事塞进一个提交。
3. **提交信息写"为什么"，不写"改了什么"。** 改了什么 diff 里看得到，为什么改只有你知道。
4. **推送前先确认推的是哪个分支。** `github_push` 的 `branch` 参数要与你在本地提交的分支一致；
   推错分支（尤其误推主干）是最常见的自伤。

**PR**

5. **PR 描述必须写清三件事**：① 改动的动机（解决什么问题）；② 怎么验证的（跑了什么命令、结果如何）；
   ③ 遗留事项 / 需要 reviewer 特别看的地方。**空描述的 PR 等于把成本转嫁给 reviewer。**
6. **开 PR 前先自查 diff**（`git_diff` 或 `github_pr_diff`）：有没有夹带调试代码、临时文件、密钥。
7. **review 要具体。** 用 `github_pr_review` 留意见时指向文件与行，说清"这里为什么有问题"，
   不要只写"看起来不错"。
8. **不要催合并。** 合并时机是人决定的；你只负责把改动做到可合并、把信息给全。

**读**

9. **读远端优先用 `github_tree` / `github_file_read`**，别为了看一眼就克隆（§1.1）。
10. **拿不准仓库的实际状态时先读再动**：`github_repo_view` 看默认分支、`github_branch_list` 看
    远端分支现状——凭印象推送是事故之源。

---

## 3. 审批策略登记（`app/agent/tool.json`）

> 本表是**这个领域的登记口径**；`app/agent/tool.json` 里此刻只有 ✅ 那 8 条 —— 写操作与 ⏳ 的工具
> 等实现时再连同登记一起加。**登记是硬约束的落点**：一个工具若没登记（或 `source` 不是
> `skills/github`），`get_skill` 会**拒绝加载整个技能**并告警——这是刻意的，"写操作默认免审"
> 是不允许的失败形态（[[docs/SKILL_DESIGN]] §13.4）。

| 工具 | `need_review` | `source` |
| --- | --- | --- |
| `github_auth_status` / `github_repo_view` / `github_tree` / `github_file_read` | `false` | `skills/github` |
| `github_search_repos` / `github_search_code` | `false` | `skills/github` |
| `github_issue_list` / `github_issue_view` | `false` | `skills/github` |
| `github_pr_list` / `github_pr_view` / `github_pr_diff` / `github_pr_files` | `false` | `skills/github` |
| `github_branch_list` / `github_release_list` | `false` | `skills/github` |
| `github_branch_create` / `github_push` / `github_pr_create` | `true` | `skills/github` |
| `github_pr_review` / `github_pr_merge` / `github_issue_comment` | `true` | `skills/github` |

> **原则**：**读全免审、写全需审**。读操作没有副作用，不该占用人；写操作无论多"小"都要人点头
> ——这正是 Theta 的立身之本。唯一的例外是 §2.1 里那些**连问都不问就直接拒**的。

---

## 4. 环境 / 凭证

| 键 | 必需 | 说明 |
| --- | --- | --- |
| `GITHUB_TOKEN` | **是** | Personal Access Token。私有仓库读写需 `repo` scope；只碰公开仓库 `public_repo` 即可 |

- 声明在**同目录的 `skill.json`**（`{"env": ["GITHUB_TOKEN"]}`），由 host 侧按
  `app/config.py::SKILL_ENV_WHITELIST` 从配置取明文转发进子进程 env——
  **不要在技能代码里自己读 `.env`**，也不要让模型去猜凭据从哪来。
  （空值 → `get_skill` 拒绝加载并回执点名 `GITHUB_TOKEN`，不会让模型撞一堆 401。）
- **token 为空时的两道闸**：① host 侧先拦——`get_skill` 拒绝加载并回执点名 `GITHUB_TOKEN`
  （模型看到的是一句可行动的话，不是一堆 401）；② `server.py` 在 **import 时**再校验一次并抛
  `ConfigError`（照 `mcp_service/file_io.py` 校验 `WORKSPACE_PATH` 的做法，只是那里是"跳过 server"
  ——联网是可选能力；这里方向相反：**用户既然要了 GitHub skill，缺凭证就该立刻说清楚**）。
- **绝不允许把 token 写进**：代码、提交、PR 描述、issue 留言。需要引用时只写"见 `GITHUB_TOKEN`"。

---

## 5. 边界（这个技能不干什么）

- **不替人做决定**：不合并、不批准、不代替 reviewer 下结论（§2.1）。
- **不碰别的平台**：GitLab / Gitea 等不在本技能范围（将来另立技能）。
- **不做本地 git 的活**（§1.3）。
- **不做"自动提交马拉松"**：不要在一轮里连续推送多个分支、批量开 PR。每次远端改动都过一次审批，
  批量操作会变成审批轰炸——**做一步、验一步、报一步**。
