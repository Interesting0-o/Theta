# Theta $\theta$ 异常管理设计

> 本文档记录 $\theta$ 异常体系的设计讨论与结论，作为后续实现的参考。
> 核心主题：**异常的边界是语义过滤器——它决定什么信息能进入大模型的推理。**
>
> 状态标注：`[已落地]` = 已实现；未标注 = 目标态。**2026-09-10 校准过一次**：失效标识符（`WORKSPACE_ROOT`、`_workspace_root()`/`_resolve_within_workspace()`、`ToolExecutionError`）与已实现的落地清单已按代码现值更新；它仍是**设计文档**（保留目标态叙述），不是现状说明书。

---

## 1. 设计目标

1. 让异常成为项目的"业务词汇表"：`WorkspaceViolationError` 这个词只有 $\theta$ 懂，它编码了"文件系统是有边界的工作区"。
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
| 异常类 | `AgentError` 家族（如 `WorkspaceViolationError`） | 程序代码（`except`） | 粗 | "这是哪一类我们要识别的失败" |
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
    """Theta 领域异常基类（全项目共享的语言层）。"""

class ConfigError(AgentError):
    """配置缺失或非法。启动即停，引导用户修改。"""

class WorkspaceViolationError(AgentError):
    """路径超出工作区沙箱边界。安全事件，必须显式处理。"""

class ToolExecutionError(AgentError):
    """工具可预期的执行失败（区别于代码 bug）。"""

class InvalidArgumentError(AgentError):
    """工具参数非法（违反输入契约，调用方可修正重试）。guard 依类名归成 invalid_argument。"""
```

> `[已落地]` 上面前四类都在 `app/exception.py`。**`ToolExecutionError` 未建**——§2.6 的结论是"操作失败就地 `return ToolResult`，不需要类"，故按下表 Q2 那一行（"代码层反应相同"）不成立为独立类；`ToolResult.error_type` 承担了它的角色。

设计依据（Q1/Q2/Q3 逐条验证）：

| 异常 | Q1 可预期 | Q2 反应不同 | Q3 跨模块 |
| --- | --- | --- | --- |
| `ConfigError` | ✅ 配置条件缺失，说得清 | ✅ 启动即停+引导，与"喂回模型继续"本质不同 | ✅ 抛在 config/file_io，接在 main/guard |
| `WorkspaceViolationError` | ✅ 沙箱规则明确 | ✅ 安全事件：记日志、警告，不是普通失败 | ✅ write/terminal 跨工具共用 |
| ~~`ToolExecutionError`~~（**未建**） | ✅ 工具操作失败 | ⚠️ 代码层反应相同 → 与"内部 bug"区分即可 | ✅ 所有工具共用 |
| `InvalidArgumentError` | ✅ 参数非法，说得清 | ✅ 输入错模型要修要重试，区别于"内部 bug 别管" | ✅ 各工具参数校验共用 |

---

## 4. 边界设计：guard（唯一的分类点）

分类只做一次，收敛到工具边界。`error_type` 是数据，能安全穿过 MCP 边界；异常类型不行。

```python
# mcp_service/utils.py —— guard：唯一的分类点（[已落地]；同步/异步工具都支持）
def guard(fn):
    @functools.wraps(fn)
    def _sync_wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:            # 有意兜底全部异常：这是最后一层网
            return _error_result(fn.__name__, exc)
    # async 版同形（await fn(...)），靠 inspect.iscoroutinefunction 分派
    return _sync_wrapped

def _error_result(fn_name: str, exc: BaseException) -> ToolResult:
    if isinstance(exc, AgentError):
        # ① 可预期业务失败 → 模型该分析、该调整
        return ToolResult(success=False, error_type=_type_token(type(exc)), content=str(exc))
    # ② 内部 bug → 模型别管，开发者来修（必须在 except 块内调用才能取到 traceback）
    logger.exception("工具 %s 发生未预期异常", fn_name)
    return ToolResult(
        success=False,
        error_type="internal_error",
        content="工具内部错误（非输入问题），无需重试。",
    )
```

> **`error_type` 不是类名**：`_type_token(type(exc))` 把 `WorkspaceViolationError` 转成
> `workspace_violation`（去 `Error`/`Exception` 后缀 → 驼峰转小写下划线），与 §10 L3 的断言一致。
> 早期伪码写的 `error_type=type(e).__name__` 会得到 `"WorkspaceViolationError"`，已按实现更正。

工具函数遵循一个关键模式——**`resolve` 放 try 外面**，让越界异常冒泡给 guard，而不是被工具自己的 `except` 吞掉：

```python
@mcp.tool()
@guard
def write_file(path: str, content: str) -> ToolResult:
    file_path = _resolve_path(path)       # 越界 → WorkspaceViolationError 冒泡给 guard
    try:
        file_path.write_text(content)
    except OSError as e:
        return ToolResult(success=False, error_type="io_error", content=str(e))
    return ToolResult(success=True, content="ok")
```

### 工作区守卫

`[已落地]` 在 `mcp_service/file_io.py`（删除的 `git.py` 曾是同款）。**环境变量是 `WORKSPACE_PATH`**（早期伪码写的 `WORKSPACE_ROOT` 从未存在）；工作区在 **import 时**读取并校验，**不存在即 `ConfigError`**：

```python
import os
from pathlib import Path

from app.exception import ConfigError, WorkspaceViolationError

_workspace_path = os.environ.get("WORKSPACE_PATH")
if not _workspace_path:
    raise ConfigError("WORKSPACE_PATH 未设置")            # 没配则 fail-fast，不放行
WORKSPACE_PATH = Path(_workspace_path).resolve()
if not WORKSPACE_PATH.exists():
    raise ConfigError(f"agent工作区路径{WORKSPACE_PATH} 不存在")

def _resolve_path(path: str) -> Path:
    """相对路径以工作区为基准解析成绝对路径；resolve() 干掉 ../ 与符号链接。"""
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raw = WORKSPACE_PATH / raw        # 关键：**不**基于 cwd（子进程 cwd 是项目根）
    return raw.resolve()

def _is_in_workspace(path: Path) -> None:
    """越界即抛 WorkspaceViolationError（调用方放在 try 之外，让它冒泡给 guard）。"""
    if not path.is_relative_to(WORKSPACE_PATH):
        raise WorkspaceViolationError(str(path), str(WORKSPACE_PATH))
```

> 与早期伪码的三处差异：① 函数叫 `_resolve_path` + `_is_in_workspace`（不是 `_resolve_within_workspace`）；
> ② 根是模块级常量 `WORKSPACE_PATH`（不是每次调用再读 env 的 `_workspace_root()`）——"以哪个目录为界"
> 进进程就定死，工具调用期不会被环境变动挪动边界；③ 相对路径**以工作区为基准**（不是 cwd——MCP 子进程
> 的 cwd 是项目根，按 cwd 解析会让相对路径落到工作区外被误判越界）。

---

## 5. ToolNode：只管格式化，不管分类

分类已在 guard 完成，`ToolNode` 收到的全是已分类的 ToolResult，只需提取正文。`except Exception` 在 ToolNode 仅剩一个职责：**MCP 传输层本身挂了**（机制故障，非工具逻辑）。

```python
# app/agent/utils.py —— format_tool_result（[已落地]；ToolNode 与编排节点共用）
from app.schema.agent_schema import ToolResult

def format_tool_result(result) -> str:
    if isinstance(result, ToolResult):
        prefix = f"[{result.error_type}] " if result.error_type else ""
        return prefix + result.content
    if isinstance(result, dict):
        return result.get("content", str(result))
    return str(result)
```

> 位置注意：它在 **`app/agent/utils.py`**（早期本节写作"写在 nodes.py"，已按现值更正）；还额外支持
> MCP adapter 的 content-block 列表形态（逐块取 `text`、`json.loads` 还原 `ToolResult` 后走前缀分支）。

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
            # 传输层故障（MCP 子进程挂了/协议错）——留 traceback，正文给事实
            result_content = f"工具执行失败: {exc}"
            extra = {"error_type": "tool_error"}
        results.append(ToolMessage(content=content,
                                    tool_call_id=tool_call.get("id", "unknown"),
                                    name=tool_name))
    return {"messages": results, "approved_tool_calls": []}
```

---

## 6. 配置错误的入口翻译

`ConfigError` 的价值不是"发明异常"（pydantic 已经在抛 `ValidationError`），而是**在入口边界做翻译**：把机器语言（"5 个必填字段缺失"）翻译成人话（"请配置 CHAT_MODEL_API_KEY"）。

```python
# app/main.py —— 入口边界翻译（设想的完整形态）
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

**现状（`[已落地]` 的一半）**：入口已经在翻译 `ConfigError` ——`app/main.py` 捕获它、打一句
`❌ 启动失败（配置问题）：…` 并以退出码 1 收场。这样"审批收件箱端口被占""工作区不存在"这类
**说得清**的失败不再裹在 traceback 里像个崩溃：

```python
# app/main.py —— 现在的样子
try:
    asyncio.run(run_tui())
except ConfigError as exc:
    print(f"\n❌ 启动失败（配置问题）：{exc}")
    sys.exit(1)
```

> ⚠️ 仍未做的那一半：`get_settings()` **懒加载**。当前 `app/agent/model.py` 在模块顶层调用它
> （`tools.py` 已无该调用），于是"`.env` 缺键"会在 **import 阶段**抛 `ValidationError` 崩掉，
> 根本走不进入口的 try——所以 `ValidationError → ConfigError` 的翻译也就无从谈起。要补的是
> 把模块顶层的调用挪进函数内。

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

- [x] `app/exception.py`：`AgentError` + `ConfigError` + `WorkspaceViolationError` + `InvalidArgumentError`（**`ToolExecutionError` 未建**，见 §2.6/§3）
- [x] `app/schema/agent_schema.py`：`ToolResult` 增加 `error_type` 字段
- [x] `mcp_service/utils.py`：`guard` 装饰器（含 `internal_error` 桶，同步/异步都支持）+ 分类用 `_type_token`；`mcp_service/file_io.py`：`_resolve_path` + `_is_in_workspace`（原拟名 `_resolve_within_workspace`/`_workspace_root`）
- [x] 各文件工具：`resolve` 移出 try，操作失败保留 `except OSError → ToolResult(io_error)`
- [x] `app/agent/utils.py`：`format_tool_result` 提取正文（`ToolNode` 调它）；`ToolNode` 的 `except Exception` 仅作传输层兜底（正文 `工具执行失败: …`、`error_type="tool_error"`）
- [x] MCP 子进程 env 注入工作区：**`WORKSPACE_PATH`**（不是 `WORKSPACE_ROOT`），由 `app/agent/mcp.py::_build_servers` 注入；`app/config.py` 不参与（工作区已是图的构造期参量）
- [~] 入口翻译：**已落地一半**——`app/main.py` 捕获 `ConfigError` 打一句人话 + 退出码 1（不再让它裹在 traceback 里）；**未做**：`get_settings()` 懒加载（现在 `app/agent/model.py` 顶层调用）与 `ValidationError → ConfigError` 的翻译
- [x] 测试：越界拒绝（绝对路径/`../`/符号链接）、工作区内放行、`internal_error` 不冒充工具失败（`tests/test_guard.py`、`tests/test_file_io_sandbox.py`）

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
from app.exception import (
    AgentError, ConfigError, InvalidArgumentError, WorkspaceViolationError,
)

def test_all_domain_exceptions_share_root():          # 验证 Q3 决策
    for cls in (ConfigError, WorkspaceViolationError, InvalidArgumentError):
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
    # 工作区在 import 期就定死，所以隔离靠 monkeypatch 模块全局（不是改 env）
    monkeypatch.setattr(file_io, "WORKSPACE_PATH", tmp_path)
    return tmp_path

def test_outside_path_rejected(ws):
    with pytest.raises(WorkspaceViolationError):
        file_io._is_in_workspace(file_io._resolve_path("/etc/passwd"))   # 绝对路径逃逸
    with pytest.raises(WorkspaceViolationError):
        file_io._is_in_workspace(file_io._resolve_path("../outside.txt"))  # 相对路径逃逸

def test_inside_path_allowed(ws):
    assert file_io._resolve_path("demo.txt") == ws / "demo.txt"   # 相对路径以工作区为基准

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
WORKSPACE_PATH=/tmp python -m pytest        # 需 `python -m`（包未安装）+ 导出 WORKSPACE_PATH
```

> （用 `.venv/Scripts/python.exe -m pytest`，**不要用 `uv run`**；关键是 `-m pytest` 与 `WORKSPACE_PATH` 两条前提。）
