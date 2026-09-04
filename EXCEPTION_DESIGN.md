# CodingAgent 异常管理设计

> 本文档记录 CodingAgent 异常体系的设计讨论与结论，作为后续实现的参考。
> 核心主题：**异常的边界是语义过滤器——它决定什么信息能进入大模型的推理。**

---

## 1. 设计目标

1. 让异常成为项目的"业务词汇表"：`WorkspaceViolationError` 这个词只有 CodingAgent 懂，它编码了"文件系统是有边界的工作区"。
2. 区分"可预期的业务失败"与"真正的 bug"，**绝不让 bug 伪装成工具调用失败**喂给大模型。
3. 异常类保持粗粒度（只表达"程序怎么应对"），细粒度信息放数据里（`error_type`、消息正文）。
4. 分类点收敛到边界（guard / 入口），中间层不散落 `except`。

---

## 2. 核心原则

### 2.1 异常是项目的业务词汇表

异常不是通用工具。`ConfigError`、`WorkspaceViolationError` 编码的是这个项目对世界的看法。因此 `app/exception.py` 放在 app 根目录——它是**共享语言层**，`app/` 与 `mcp_service/` 都要 import 它，放中间避免循环依赖。

### 2.2 分类按"业务语义"，不按"项目结构"

❌ **错误示范**——按目录/模块拆：

```python
class FileIOModuleError(AgentError): ...   # 错
class TerminalModuleError(AgentError): ... # 错
class AgentModuleError(AgentError): ...    # 错
```

`file_io.py` 和 `terminal.py` 都会遇到"越界"，它们要的是**同一个** `WorkspaceViolationError`——因为调用方对"越界"的处理与模块无关。

**分类的唯一标准**：调用方会对这两种错误做不同的处理吗？会 → 拆类；不会 → 合并。

### 2.3 三个决策问题（Q1 / Q2 / Q3）

设计每个异常时依次回答，**三个问题各管一件事，别揉在一起**：

| 问题 | 决定什么 |
| --- | --- |
| Q1 可预期还是 bug？ | 决定**是不是异常**。可预期 = 程序能解释原因；bug = 解释不了，不该建类 |
| Q2 调用方反应不同吗？ | 决定**分不分两个类**。反应相同就合并 |
| Q3 会跨模块出现吗？ | 决定**挂共享根还是模块头**（本项目一律共享根） |

> 注意：Q1 的"可预期"不等于"可恢复"，只等于"说得清"。致命错误（如缺 API Key）也可预期，它要的是一张漂亮的"拒单说明"而不是静默。

### 2.4 三层模型：异常类粗，数据细

| 层 | 载体 | 谁消费 | 粒度 | 职责 |
| --- | --- | --- | --- | --- |
| 异常类 | `ToolExecutionError` | 程序代码（`except`） | 粗 | "这是工具失败" |
| 结果标记 | `ToolResult.error_type` | 边界数据 | 中 | "是哪一类失败"（`not_found`/`permission`/`io_error`） |
| 消息正文 | `ToolMessage.content` | **大模型** | 细 | "为什么失败、哪个路径、errno 是什么" |

大模型**从不读异常类**，它只读 `ToolMessage.content`。所以合并异常类不会饿死模型——**前提是正文必须携带足够的诊断事实**（路径、errno、具体原因）。

### 2.5 边界是语义过滤器

边界（guard、入口、ToolNode）不是管道，它决定"什么信息可以进入模型的推理"。存在一个不对称：

| 穿过边界的错误 | 对模型的价值 |
| --- | --- |
| 可预期失败（文件不存在、越界） | **有用**——合法反馈，模型应分析原因、调整计划 |
| 内部 bug（类型不匹配、空指针） | **有害**——模型分析不出原因，只会浪费 token 乱猜，掩盖我们的 bug |

**"不掩盖"≠"必须 re-raise"**。"不掩盖"的意思是"路由给正确的消费者，且信息完整"。bug 的正确消费者是**开发者日志**，不是模型——`logger.exception()` 留全 traceback，同时明确标记"非输入问题"，不让模型去追。

### 2.6 什么时候异常是多余的

**当且仅当：所有异常都在同一处被无差别捕获，且唯一消费者是"转成一句话"时，异常类多余**——此时直接返回结果对象更干净。

因此我们的设计里其实两种都用：

| 错误 | 处理方式 | 为什么 |
| --- | --- | --- |
| 越界 / 配置缺失 | **抛**（typed 异常） | 需要被不同层级识别、需要安全反应、会跨模块 |
| 操作失败（找不到/没权限/磁盘满） | **就地 return** ToolResult | 唯一消费者是模型，读正文就够，不需要类 |

> 需要"被别的地方认识"的错误才抛异常；只需要"给模型看一句话"的错误直接返回结果。

---

## 3. 最终异常体系

```python
class AgentError(Exception):
    """CodingAgent 领域异常基类（全项目共享的语言层）。"""

class ConfigError(AgentError):
    """配置缺失或非法。启动即停，引导用户修改。"""

class WorkspaceViolationError(AgentError):
    """路径超出工作区沙箱边界。安全事件，必须显式处理。"""

class ToolExecutionError(AgentError):
    """工具可预期的执行失败（区别于代码 bug）。"""

class InvalidArgumentError(AgentError):
    """工具参数非法（违反输入契约，调用方可修正重试）。guard 依类名归成 invalid_argument。"""
```

设计依据（Q1/Q2/Q3 逐条验证）：

| 异常 | Q1 可预期 | Q2 反应不同 | Q3 跨模块 |
| --- | --- | --- | --- |
| `ConfigError` | ✅ 配置条件缺失，说得清 | ✅ 启动即停+引导，与"喂回模型继续"本质不同 | ✅ 抛在 config/file_io，接在 main/guard |
| `WorkspaceViolationError` | ✅ 沙箱规则明确 | ✅ 安全事件：记日志、警告，不是普通失败 | ✅ write/terminal 跨工具共用 |
| `ToolExecutionError` | ✅ 工具操作失败 | ⚠️ 代码层反应相同 → 与"内部 bug"区分即可 | ✅ 所有工具共用 |
| `InvalidArgumentError` | ✅ 参数非法，说得清 | ✅ 输入错模型要修要重试，区别于"内部 bug 别管" | ✅ 各工具参数校验共用 |

---

## 4. 边界设计：guard（唯一的分类点）

分类只做一次，收敛到工具边界。`error_type` 是数据，能安全穿过 MCP 边界；异常类型不行。

```python
# mcp_service —— guard：唯一的分类点
import functools
import logging

from app.exception import AgentError
from app.schema.agent_schema import ToolResult

logger = logging.getLogger(__name__)

def guard(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AgentError as e:
            # ① 可预期业务失败 → 模型该分析、该调整
            return ToolResult(success=False, error_type=type(e).__name__, content=str(e))
        except Exception:
            # ② 内部 bug → 模型别管，开发者来修
            logger.exception("工具 %s 发生未预期异常", fn.__name__)   # traceback 全留
            return ToolResult(
                success=False,
                error_type="internal_error",
                content="工具内部错误（非输入问题），无需重试。",
            )
    return wrapped
```

工具函数遵循一个关键模式——**`resolve` 放 try 外面**，让越界异常冒泡给 guard，而不是被工具自己的 `except` 吞掉：

```python
@mcp.tool()
@guard
def write_file(path: str, content: str) -> ToolResult:
    file_path = _resolve_within_workspace(path)   # 越界 → WorkspaceViolationError 冒泡给 guard
    try:
        file_path.write_text(content)
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=str(e))
    return ToolResult(success=True, content="ok")
```

### 工作区守卫

```python
import os
from pathlib import Path

from app.exception import ConfigError, WorkspaceViolationError

def _workspace_root() -> Path:
    root = os.environ.get("WORKSPACE_ROOT")
    if not root:
        raise ConfigError("未配置 WORKSPACE_ROOT 环境变量")   # 没配则 fail-fast，不放行
    return Path(root).expanduser().resolve()

def _resolve_within_workspace(path: str) -> Path:
    """解析真实路径并校验在工作区内，越界抛 WorkspaceViolationError。"""
    resolved = Path(path).expanduser().resolve()      # 干掉 ../ 和符号链接
    ws = _workspace_root()
    if not resolved.is_relative_to(ws):
        raise WorkspaceViolationError(str(resolved), str(ws))
    return resolved
```

---

## 5. ToolNode：只管格式化，不管分类

分类已在 guard 完成，`ToolNode` 收到的全是已分类的 ToolResult，只需提取正文。`except Exception` 在 ToolNode 仅剩一个职责：**MCP 传输层本身挂了**（机制故障，非工具逻辑）。

```python
# app/agent/nodes.py —— ToolNode
from app.schema.agent_schema import ToolResult

def format_tool_result(result) -> str:
    if isinstance(result, ToolResult):
        prefix = f"[{result.error_type}] " if result.error_type else ""
        return prefix + result.content
    if isinstance(result, dict):
        return result.get("content", str(result))
    return str(result)

async def __call__(self, state: AgentState) -> dict | AgentState:
    tool_calls = list(state.get("approved_tool_calls", []))
    results: list[ToolMessage] = []
    for tool_call in tool_calls:
        tool_name = tool_call.get("name")
        tool_obj = self.tools_map.get(tool_name)
        if not tool_obj:
            results.append(ToolMessage(
                content=f"工具不存在或未注册: {tool_name}",
                tool_call_id=tool_call.get("id", "unknown"),
                name=tool_name,
            ))
            continue
        try:
            result = await tool_obj.ainvoke(tool_call.get("args", {}))
            content = format_tool_result(result)      # "[workspace_violation] 路径越界: ..."
        except Exception as exc:
            logger.exception("工具调用机制失败: %s", tool_name)   # 传输层故障，留 traceback
            content = "[内部错误] 工具调用失败（非输入问题），请检查服务状态。"
        results.append(ToolMessage(content=content,
                                    tool_call_id=tool_call.get("id", "unknown"),
                                    name=tool_name))
    return {"messages": results, "approved_tool_calls": []}
```

---

## 6. 配置错误的入口翻译

`ConfigError` 的价值不是"发明异常"（pydantic 已经在抛 `ValidationError`），而是**在入口边界做翻译**：把机器语言（"5 个必填字段缺失"）翻译成人话（"请配置 CHAT_MODEL_API_KEY"）。

```python
# app/main.py —— 入口边界翻译
from pydantic import ValidationError
from app.config import get_settings

try:
    settings = get_settings()
except ValidationError as exc:
    missing = ", ".join(str(e["loc"][0]) for e in exc.errors())
    print(f"❌ 配置缺失: {missing}")
    print("请复制 .env.example 为 .env 并填写相应配置。")
    sys.exit(1)
```

> ⚠️ 前提：`get_settings()` 必须**懒加载**。当前 `model.py` / `tools.py` 在模块顶层调用 `get_settings()`，会让异常在 import 时就崩、根本走不进入口的 try。需要把模块顶层的调用挪进函数内。

---

## 7. 验收标准

给边界定一条可自查的标准：

> **每一条穿过边界、进入模型视野的消息，都必须能回答：这是模型的错、环境的错、还是我们的 bug？**
>
> - 模型的错 → 给足事实，让它调整
> - 环境的错 → 给足事实，允许重试
> - **我们的 bug → 必须标记 `internal_error`，必须留 traceback，绝不能让模型以为是自己造成的**

漏的后果不是崩溃，而是**模型拿着错误的信息，白白地努力**。

---

## 8. 常见误区

1. **一个方法一个异常类** → 异常按"错误类别"分，不按函数/模块分。
2. **`except Exception` 通吃一切** → 连 bug 也吞，让问题悄悄消失。只在"唯一消费者是转成一句话"时才允许。
3. **一次性设计完整体系** → 分类体系是长出来的：先 `AgentError` + 实际遇到的 2~3 个，出现"要单独捕获、单独处理"的新类别再加。
4. **把异常当控制流** → "用户拒绝审批"是正常分支，不是错误，不该用异常表达。
5. **在消息里给"指令"不给"事实"** → 正文给路径、errno、原因（事实），规划让模型自己做（它是 agent，不是脚本）。

---

## 9. 落地清单

- [ ] `app/exception.py`：`AgentError` + `ConfigError` + `WorkspaceViolationError` + `ToolExecutionError` + `InvalidArgumentError`
- [ ] `app/schema/agent_schema.py`：`ToolResult` 增加 `error_type` 字段
- [ ] `mcp_service/`：`guard` 装饰器（含 internal_error 桶）+ `_resolve_within_workspace` + `_workspace_root`
- [ ] 各文件工具：`resolve` 移出 try，操作失败保留 `except OSError → ToolResult`
- [ ] `app/agent/nodes.py`：`ToolNode` 用 `format_tool_result` 提取正文，`except Exception` 仅留作传输层兜底
- [ ] `app/config.py` / `app/agent/mcp.py`：`WORKSPACE_ROOT` 配置 + 注入 MCP 子进程 env
- [ ] `get_settings()` 懒加载，入口翻译 ValidationError → ConfigError 引导
- [ ] 测试：越界拒绝（绝对路径/`../`/符号链接）、工作区内放行、`internal_error` 不冒充工具失败

---

## 10. 与 pytest 的串联：测试即合同

> 异常设计的原则是"嘴上说的"，pytest 测试是"机器强制的合同"。**能写出有意义的 `pytest.raises` 测试，异常类才该存在**——可测试性反向约束设计，也是"异常何时多余"问题的裁决者。

### 测试层次（从类型到正文）

| 层次 | 验证什么 | 消费对象 |
| --- | --- | --- |
| L1 异常体系 | 类型存在、挂对共享根（Q3 决策） | 代码 / 开发者 |
| L2 守卫逻辑 | 越界 vs 放行的判定（安全核心） | 安全边界 |
| L3 边界分类 | 异常 → `error_type` 转换正确（bug 绝不伪装） | **最重要的原则** |
| L4 正文格式化 | LLM 实际读到什么 | 大模型 |

`pytest.raises` 消费**类型**，`format_tool_result` 测试消费**正文**——两类消费者各有一个测试入口，呼应"类粗、数据细"的设计。

### L1 异常体系

```python
from app.exception import AgentError, ConfigError, WorkspaceViolationError, ToolExecutionError

def test_all_domain_exceptions_share_root():          # 验证 Q3 决策
    for cls in (ConfigError, WorkspaceViolationError, ToolExecutionError):
        assert issubclass(cls, AgentError)

def test_workspace_violation_carries_structured_fields():
    err = WorkspaceViolationError("/etc/passwd", "/home/ws")
    assert err.path == "/etc/passwd"                  # 结构化字段可断言
    assert err.workspace == "/home/ws"
```

> 如果异常总是被扁平化成字符串，这层测试写不出来——**写得出这层测试，本身就是"这个类值得存在"的证据。**

### L2 守卫逻辑

```python
@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))   # 环境隔离，防测试互相污染
    return tmp_path

def test_outside_path_rejected(ws):
    with pytest.raises(WorkspaceViolationError):
        _resolve_within_workspace("/etc/passwd")          # 绝对路径逃逸
    with pytest.raises(WorkspaceViolationError):
        _resolve_within_workspace("../outside.txt")        # 相对路径逃逸

def test_inside_path_allowed(ws):
    assert _resolve_within_workspace("demo.txt") == ws / "demo.txt"
```

### L3 边界分类（最重要的测试）

```python
def test_guard_converts_domain_error(ws):
    @guard
    def fail():
        raise WorkspaceViolationError("/x", str(ws))
    result = fail()
    assert result.success is False
    assert result.error_type == "workspace_violation"

def test_guard_marks_bug_as_internal_error():
    @guard
    def bug():
        return None.split()          # 模拟真实的代码 bug（TypeError）
    result = bug()
    assert result.success is False
    assert result.error_type == "internal_error"   # bug 绝不伪装成工具失败

def test_guard_passes_success_through():
    @guard
    def ok():
        return ToolResult(success=True, content="done")
    assert ok().success is True
```

> **防回归价值**：将来有人把 guard 改回 `except Exception → generic`，`test_guard_marks_bug_as_internal_error` 立刻变红——"bug 不伪装成工具失败"这条原则由机器守住。

### L4 正文格式化

```python
def test_format_tool_result_prefixes_error_type():
    r = ToolResult(success=False, error_type="not_found", content="路径不存在")
    assert format_tool_result(r) == "[not_found] 路径不存在"
```

### 实践前提

测试**直接调原始函数**（与 `tests/test_file_io.py` 一致），不经过 FastMCP。这正是"`resolve` 放 try 外、直接抛"的又一个理由：**只有抛出去，`pytest.raises(WorkspaceViolationError)` 才能断言精确类型**；若越界被工具内部吞成 ToolResult，测试就只能弱弱断言 `success is False`，丢了类型信息。

运行：

```bash
uv run pytest
```
