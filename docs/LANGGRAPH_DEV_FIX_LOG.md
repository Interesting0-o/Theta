# LangGraph dev 修复日志

日期：2026-11-09
环境：Windows、Python 3.13、系统默认代码页 GBK

## 现象与排查时间线

### 1. 开发服务器启动时出现 `UnicodeDecodeError`

最初启动日志同时提示 `langgraph-api 0.8.6` 已 EOL。崩溃栈定位到
`langgraph_api.validation` 使用未指定编码的 `open()` 读取 `openapi.json`；
Windows Python 默认使用 GBK，而该文件是 UTF-8，因此读取失败。

**处理：**将开发依赖升级并锁定到 `langgraph-cli 0.4.42`、
`langgraph-api 0.17.1` 和 `langgraph-runtime-inmem 0.37.1`。升级 API 后，
仅直接导入模块仍能复现 GBK 解码错误，所以增加 Windows 启动脚本，在
Python 进程启动前设置 `PYTHONUTF8=1`。脚本同时避免旧版 CLI 在 GBK
终端打印帮助信息时发生 emoji 编码错误。

**结论：**升级解决 EOL 版本问题；UTF-8 模式才是修复 Windows 默认编码
导致读取失败的直接措施。

### 2. 升级后启动又遇到 Prometheus `AttributeError`

新错误为 `AttributeError: module 'resource' has no attribute 'getpagesize'`。
检查发现，仓库根目录用于存放运行数据的 `resource/` 在 Windows 上被
Python 识别为同名 namespace package。Prometheus 将它误认为 Unix
标准库 `resource`；Windows 实际没有该标准库模块。

**处理：**增加根目录 `resource.py`，在 Windows 上抛出 `ImportError`，
让 Prometheus 进入其非 Unix 回退路径；运行数据仍保留在原来的
`resource/` 目录。

**验证：**从仓库根目录导入 `prometheus_client.process_collector` 成功，
页面大小回退值为 4096；回归测试通过。

### 3. Studio 显示 `Unable to connect` / `Failed to fetch`

这并非单纯的服务器未启动：排查时默认端口 `2024` 的 `/ok`、`/info`
及 `/assistants/search` 均返回 HTTP 200。浏览器控制台和实际 OPTIONS
请求进一步定位到两个问题：

1. Studio 从 `https://smith.langchain.com` 访问本机 `127.0.0.1` 时会发送
   Private Network Access 预检。服务器的 OPTIONS 响应为
   `400 Disallowed CORS private-network`，浏览器因此拦截后续 graph 请求。
2. 允许预检后，schema 和 graph 请求进入服务器，但返回 HTTP 500。服务端
   traceback 显示，加载 MCP 工具时 Windows 的 `shutil.which()` 调用
   `os.access()`，被 LangGraph dev 的同步 I/O 检查器拒绝。

**处理：**Windows 启动脚本设置专用环境标记；随脚本加载的
`scripts/sitecustomize.py` 仅在该标记及 Windows 条件下，为 Starlette
CORS middleware 启用 `allow_private_network`。脚本另传入 LangGraph dev
提供的 `--allow-blocking`，用于本地开发时放行上述 Windows 可执行文件探测。

**验证：**PNA 预检返回 HTTP 200，且有
`Access-Control-Allow-Private-Network: true`；Studio 所需的
`/schemas`、`/subgraphs` 和 `/graph` 请求均返回 HTTP 200。浏览器中 graph
成功显示。

## 最终启动方式

Windows 下从仓库根目录运行：

```powershell
.\scripts\langgraph-dev.cmd
```

直接运行 `langgraph dev` 不会加载上述 Windows 编码及本地网络兼容设置。
修改启动方式后需停止旧进程并重新启动；旧进程不会自动获得新环境或补丁。
排查期间确认原先监听在 `2024` 的进程仍是旧实例，其 PNA 预检仍返回 400。

## 回归测试与验证记录

- `tests/test_resource_module_collision.py`：通过。
- `tests/test_langgraph_dev_pna.py`：通过。
- 两项 Windows 针对性回归测试：`2 passed`。
- 完整测试套件在设置所需的临时 `WORKSPACE_PATH` 后通过：
  `582 passed, 7 skipped`。若测试进程额外设置 `PYTHONUTF8=1`，一个检查
  子进程 traceback 的既有测试会因其显式 UTF-8 解码与 Windows 子进程
  GBK 输出不匹配而失败；清除该变量后该测试及全套测试通过。启动脚本的
  UTF-8 设置只作用于 LangGraph 开发服务器进程。
