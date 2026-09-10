"""基座对前端的要求：一个三方法的协议（前端只实现它，基座不碰 stdin/print）。

方向（docs/ARCHITECTURE.md §2.7）：`app/tui`（终端）与将来的 web 前端**都实现本协议**，
基座（`AgentPlatform`）只认协议、不认具体前端。事件形状在 `app/schema/ui_schema.py`。

契约要点：
- `read_line` 会被基座拿去与"审批到达"竞速（`turn.py::_race`），**输的一方会被 cancel**：
  前端必须做到"被取消不丢输入"——终端靠"独立 reader 线程 + 队列"满足（app/tui/input.py）；
- `decide` 是**唯一**能让 park 的 run 继续的入口：基座交出渲染所需的 value，前端负责问人
  （终端 = 面板 + y/n；web = 推审批帧等回包），返回是否批准。决定权威在人，不在基座、也不在模型；
- `emit` 只渲染，不得改基座状态、不得阻塞（同步方法）。
"""
from typing import Protocol

from app.schema.ui_schema import Event


class UI(Protocol):
    def emit(self, event: Event) -> None:
        """渲染一条基座事件（会话就绪 / 可以输入 / turn 答复 / turn 失败）。"""
        ...

    async def read_line(self) -> str | None:
        """取一行用户输入；None = EOF/退出。**必须可取消且不丢输入**（见模块 docstring）。

        约定：返回前先 strip（终端前端在 pump 里做）；**空串 = 用户没说话**，基座会保持
        同一提示继续等——所以纯空白行不该被当消息送进来。
        """
        ...

    async def decide(self, value: dict) -> bool:
        """就一条待审请求询问人类，返回是否批准（value 与 ReviewNode interrupt 同构）。"""
        ...
