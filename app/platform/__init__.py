"""app/platform —— agent 基座（图外面那层）：run 生命周期 + 审批 broker + runtime 装配。

定位（docs/ARCHITECTURE.md §0/§2/§3）：**主程序是事件基座，LangGraph 图只是基座上的执行
单元**。本包承载"图外面那层"的事：起/续/取消一条 run、把 interrupt 当事件端口、统一审批
队列、按 (工作区, 会话) 装配 db/图。

**与前端的分界**（§2.7）：基座**不 print、不读 stdin**，与外界只经 `UI` 协议说话
（`app/platform/ui.py`：emit / read_line / decide）；终端前端在 `app/tui`（将来的 web 前端
同协议），事件形状在 `app/schema/ui_schema.py`。依赖方向单向：app/tui → app/platform →
app/resource / app/schema；本包**不 import app.tui**。

命名注记：这里的 "platform" 指**本项目的事件基座**，与 LangGraph Platform（`langgraph.json`
注册的零参入口 `get_main_agent_graph_langgraph` 面向的那个托管平台）不是一回事。

模块（各自单一职责）：
- ui.py         基座对前端的要求：UI 协议（三方法）
- approvals.py  统一审批 broker：ApprovalInbox（纯队列）+ 薄 HTTP 收件箱 + 排空/回填
- turn.py       一次 run 的驱动原语：drive_turn（park/resume）+ _race
- runtime.py    按 (工作区, 会话) 装配运行时：db / checkpointer / 编译图 / 记忆播种
- mcp.py        **宿主侧 MCP 边界**之一：连接配置 + 工具加载 + 运行体常驻（2026-09-21 从
                app/agent/mcp.py 搬来）；tool_results.py 是它的另一半（工具返回归一化）
- loop.py       AgentPlatform：主事件循环（drain 待批 → 等 turn → 等输入）
- commands/     控制面命令（图之外）：机制与唯一一张表在 `__init__.py`，提示词载荷在
                `prompts.py`，各命令在 `session.py` 等子模块

`app.agent.*`（graph → nodes，import 即需 .env）只在 runtime.py 内懒加载——`import app.platform`
不触发 .env。`mcp.py` / `tool_results.py` 这两支**不 import app.agent**（那是"宿主侧 MCP 边界"
的定位本身），所以 `app.agent.*` 反过来可以 import 它们、而前者仍是懒加载。
"""
from app.platform.loop import AgentPlatform
from app.platform.ui import UI

__all__ = ["AgentPlatform", "UI"]
