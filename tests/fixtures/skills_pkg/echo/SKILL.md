---
name: echo
description: 测试夹具技能（能力型）：只有一个 demo_echo 工具，不碰网络与凭证。
---

# Echo（测试夹具）

这个技能**只服务于 `tests/test_skill_runtime.py` 的端到端用例**：它证明"技能自带的 MCP server
能被真拉起、它的工具能真被调用、卸载后子进程真被收掉"这条链路是通的。

它不在项目根的 `skills/` 下（而在 `tests/fixtures/skills_pkg/`），所以模型在真实会话里看不到它
——测试通过把技能源与包名一起指到 fixture 根来加载（`SKILL_DESIGN.md` §13 的测试缝）。
