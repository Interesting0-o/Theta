"""HTTP 版最小 agent MCP 服务（transport = streamable-http）。

一个走 HTTP 的 MCP server，暴露两个演示用工具：
- `chat(task)`：调用后端聊天模型**一次性作答并返回**（传输层 + 单次 AI 回复，无工具/状态）；
- `request_approval(...)`：模拟子 agent 在敏感动作前的 interrupt——向主 agent 的薄收件箱
  （ApprovalInboxServer）发一条审批请求、阻塞等待人工决定后返回（§6 回传通道的最小形态；
  不含真实写/命令工具，写权限下放是后续里程碑，见 docs/MULTI_AGENT.md）。

- 启动：`python -m mcp_service.http_agent`（默认 127.0.0.1:8000，路径 /mcp；
  可用 SUBAGENT_HTTP_HOST / SUBAGENT_HTTP_PORT 覆盖；收件箱地址用 AGENT_INBOX_URL 覆盖，
  默认 http://127.0.0.1:8010，与 app/approval_inbox.py 的 AGENT_INBOX_PORT 对齐）。
- 客户端连 `http://<host>:<port>/mcp`：langchain_mcp_adapters 的
  `{"transport": "streamable_http", "url": "…/mcp"}`。
- app.agent.model 懒加载：本模块 import 不触发 get_settings()，冷启动无需 .env；
  真正调 chat 时才读 CHAT_*（见 CLAUDE.md/config 的 .env 相对 cwd 约定）。
"""
import os
import uuid

import httpx
from langchain_core.messages import HumanMessage
from mcp.server.fastmcp import FastMCP

from app.exception import InvalidArgumentError
from app.schema.agent_schema import ToolResult
from app.schema.approval_schema import ApprovalRequest
from mcp_service.utils import guard

HOST = os.environ.get("SUBAGENT_HTTP_HOST", "127.0.0.1")
PORT = int(os.environ.get("SUBAGENT_HTTP_PORT", "8000"))

# 子服务人设：极简一行，仅让回复带"子服务"边界；不引入工具/状态
_SYSTEM = "你是 CodingAgent 的 HTTP 子 agent 服务。请直接针对请求作答，默认用中文。"

mcp = FastMCP("SubAgentHttp", host=HOST, port=PORT)


@mcp.tool()
@guard
async def chat(task: str) -> ToolResult:
    """把一段话交给后端聊天模型，返回其一次性回复（无工具调用、无状态）。

    当前仅验证 HTTP 传输 + AI 回复链路是否打通。

    Args:
        task: 用户请求文本。

    Returns:
        模型回复文本；失败时以 [error_type] 前缀开头说明原因。
    """
    from app.agent.model import get_chat_model  # 懒加载：避免 import 期 get_settings()

    model = get_chat_model()
    res = await model.ainvoke([_SYSTEM, HumanMessage(content=task)])
    content = getattr(res, "content", None)
    text = content if isinstance(content, str) else (content or "")
    return ToolResult(success=True, content=str(text).strip() or "（模型未返回正文）")


def _inbox_base_url() -> str:
    """主 agent 收件箱（ApprovalInboxServer）的基地址；每次现取以便测试/运行时覆盖。"""
    return os.environ.get("AGENT_INBOX_URL", "http://127.0.0.1:8010")


async def request_approval_impl(
    description: str,
    base_url: str,
    tool_name: str = "write_file",
    tool_args: dict | None = None,
) -> ToolResult:
    """request_approval 的核心实现；base_url 可注入（测试指向真实 ApprovalInboxServer 端口）。

    - description 空白 → InvalidArgumentError（要向人工说明这次动作的意图）。
    - 流程：POST /requests 入队拿 approval_id → GET /requests/{id}?block=1 长轮询，
      主侧经 decide_approval 收 y/n 后 complete 回填 → 本函数拿到决定。
    """
    if not isinstance(description, str) or not description.strip():
        raise InvalidArgumentError("description 不能为空（要向人工说明这次动作的意图）")

    payload: ApprovalRequest = {
        "worker_id": f"demo-{uuid.uuid4().hex[:8]}",
        "tool_name": tool_name,
        "tool_args": dict(tool_args) if tool_args else {},
        "description": description,
    }
    async with httpx.AsyncClient(base_url=base_url, timeout=60.0) as client:
        resp = await client.post("/requests", json=payload)
        if resp.status_code != 200:
            return ToolResult(
                success=False, content=f"收件箱入队失败: HTTP {resp.status_code} {resp.text[:200]}"
            )
        approval_id = resp.json()["approval_id"]

        rr = await client.get(f"/requests/{approval_id}", params={"block": "1"})
        if rr.status_code != 200:
            return ToolResult(success=False, content=f"等待决定失败: HTTP {rr.status_code}")
        decision = rr.json()

    if decision.get("approved") is True:
        return ToolResult(
            success=True, content=f"已批准: {tool_name}（{description}）— 模拟 worker 可继续"
        )
    return ToolResult(
        success=True, content=f"[denied] 拒绝 {tool_name}：{description} — 模拟 worker 不执行"
    )


@mcp.tool()
@guard
async def request_approval(
    description: str,
    tool_name: str = "write_file",
    tool_args: dict | None = None,
) -> ToolResult:
    """模拟子 agent 在敏感动作前的 interrupt：向主 agent 收件箱发审批请求并等待人工决定。

    本工具是 docs §6"回传通道"的最小演示：调用方（模拟 worker）在打算执行一个会改状态的
    动作前"挂起"，把该动作（description 说明意图）POST 进主 agent 的薄收件箱
    （ApprovalInboxServer /requests）；主侧 TUI 经 decide_approval 弹出面板收 y/n 后回填，
    决定经长轮询 GET /requests/{id}?block=1 返回本工具。**它本身不执行任何动作**——
    批准后由调用方决定是否继续，拒绝则不要执行。

    Args:
        description: 一句话说明这次动作的意图（审批时与动作同屏展示，必填）。
        tool_name: 被审批的动作名（默认 write_file，仅作面板展示）。
        tool_args: 被审批动作的参数（展示用；含 description 键时面板会单列"解释"）。

    Returns:
        决定文本：批准 / [denied] 拒绝；请求或等待失败时以 [error_type] 前缀说明。
    """
    return await request_approval_impl(description, _inbox_base_url(), tool_name, tool_args)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
