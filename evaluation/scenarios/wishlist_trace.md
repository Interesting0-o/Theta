# 典型场景：多文件功能开发（zero-to-one 轨迹版）

> 灵感来自 ViBench 2.0 的 `simple_wish_list` 示例（Apache-2.0，公开发布在
> `ViBench/vibench-public` 的 `_harness/examples/simple_wish_list/`）。本任务**改写**了
> PRD，不复制原文；ViBench 的 canary GUID 与原始 test plan 不进入本仓库。
>
> 与 ViBench 的区别：不跑浏览器评分器，不要求 Web 框架；只观察 agent 在文件/终端环境里
> 如何从零构建一个多文件项目，并把工具轨迹留成 fixture。

## 任务

`wishlist_trace` 要求 agent 用 **Python 标准库**（不装第三方依赖）从零实现一个匿名许愿板：

- `app.py`：HTTP 服务与路由；
- `storage.py`：共享持久化存储；
- `templates/index.html`：页面模板；
- `assets/poem.txt`：页面底部的小诗（任务预置）；
- `tests/test_app.py`：pytest 测试，至少覆盖"提交后新愿望在最前"与"201 字被拒绝"。

产品需求：200 字上限、最新在前、共享持久化、底部显示小诗、无账号/编辑/删除。

## 检查

| 断言 | 说明 |
| --- | --- |
| `files_exist(app.py, storage.py, templates/index.html, tests/test_app.py)` | 多文件交付物 |
| `pytest_passes("tests", 120)` | 模型自己写的测试必须通过（回归闸门） |
| `approvals_between(1, 40)` | 写文件/跑测试的审批次数在合理区间 |
| `terminated()` | 正常收尾 |

## 策略

`policy.allow_dev_terminal()`：

- 文件读写工具放行；
- 终端允许只读命令与 `python` / `python3` / `py` / `pytest`；
- 拒绝 `pip/npm/curl/wget/git clone`、输出重定向、`rm/del/mv/cp/sudo` 等；
- 这是开发场景的 pragmatic 白名单，只应跑在每任务的临时工作区。

## 运行

```bash
# 录制轨迹（层 B，花 token）
python -m evaluation --record --task wishlist_trace

# 回放（层 A，零 token）
python -m evaluation --task wishlist_trace --trace
```

## 已录轨迹（2026-10-18）

层 B 录制：**16 轮、15 次审批、828.8 s**；fixture 约 40 KB；层 A 回放 3.2 s、34 条 trace 事件。

```text
→ get_directory_tree / read_file(assets/poem.txt)
→ create_plan（5 步）
→ create_file(storage.py) / create_dir(templates, tests)
→ run_command: python -c ...  （storage 冒烟）
→ create_file(smoke_storage.py) / run_command: python smoke_storage.py  （ok）
→ create_file(app.py) / create_file(templates/index.html)
→ edit_file ×3（第一次缺 path 被参数校验拒绝，补 path 后成功）
→ create_file(tests/conftest.py) / create_file(tests/test_app.py)
→ run_command: python -m pytest tests/ -v   ← exit_code 1（2 个测试失败）
→ run_command: python -m pip install pytest ← denied（策略拒绝装依赖）
（拒绝后回合结束）
```

结果：**3/4 检查通过**。`files_exist` / `approvals_between` / `terminated` 通过；
`pytest_passes` 失败，模型自己的测试 14 passed / 2 failed：

- `test_template_has_required_structure`：页面模板缺 `maxlength`；
- `test_unknown_path_404`：测试自身没有捕获 `urllib.error.HTTPError`。

这不是框架 bug，而是这条轨迹的真实行为；它同时暴露了一个框架行为：**审批被拒后
`review_node` 走 END，模型没有机会在同一回合内改用现有 pytest 修测试**。下一版要么在
prompt 里说明"测试由评估器运行，不要安装 pytest"，要么把拒绝后的路由改成回到 `llm_node`。
