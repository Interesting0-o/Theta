"""夹具：一个**起不来**的技能 server。

import 期就 import 一个不存在的模块，模拟真实故障（缺依赖 / 代码报错）。它存在的唯一目的，是让
`tests/test_skill_runtime.py` 验证"能力型技能起不来时，回执能不能带出**子进程的真 traceback**"
——MCP 适配器只会把 `ExceptionGroup → McpError: Connection closed` 交给 host，真正的原因只在
这个子进程的 stderr 上。
"""
import imaginary_dependency_that_does_not_exist  # noqa: F401  ← 故意的，见模块 docstring
