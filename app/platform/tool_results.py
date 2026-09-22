"""工具返回的统一归一化：结构化 ToolResult / 模型可见文本——**MCP 宿主侧**的解封装。

**位置**（2026-09-21 从 `app/agent/utils.py` 搬来）：与同目录的 `mcp.py` 是同一件事的两半
（主进程侧的 MCP 边界：一个管运行体、一个管返回形状）。按 docs/ARCHITECTURE.md §4 的
**反向限制**——"如果 MCP 这条外部边界不存在，这段代码还需要吗？"——答案是"不需要"：
本模块的全部存在理由就是解开**跨进程返回的形状**（content-block → json.loads → 还原
ToolResult），它服务的是边界，不是 agent 语义。

**跨进程序列化的单一落点 = 生产端**（2026-09-21 拍板）：写出去的那一侧是
`mcp_service/utils.py::guard`（把 ToolResult 交给 FastMCP → JSON 化进 content[].text），
本模块只**读**那个形状。改形状先改 guard，这里是消费者；`tests/test_format_tool_result.py`
与 `tests/test_mcp_pool.py` 各钉一端。

注意：早期 `app/agent/utils.py` 曾因"各 helper 单一消费者"被删（helpers 收进各自消费者
模块）。本模块只放**一个职责**（工具结果归一化），不是旧的无序工具袋——若日后要加
其它通用 helper，先判断是否够得上"多消费者 + 单一职责"再决定是否入此。

职责边界：
- format_tool_result：把工具返回值渲染成模型可见文本（失败带 `[error_type]` 前缀）；
- coerce_tool_result：只做"返回值里是否带 ToolResult"的结构化还原（不渲染），
  供打结构化戳用；
- 二者共用 MCP content-block 列表的正文提取（_join_block_texts）。
"""
import json

from app.schema.agent_schema import ToolResult


def _join_block_texts(blocks: list) -> str:
    """拼接 MCP content-block 列表的正文：dict 取 text（content 兜底）、非 dict str()。

    丢弃 adapters 附带的随机 id / 非文本块噪声；只留各块的正文。
    """
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            parts.append(block.get("text") or block.get("content") or "")
        else:
            parts.append(str(block))
    return "\n".join(part for part in parts if part)


def coerce_tool_result(result) -> ToolResult | None:
    """从工具返回值还原结构化 ToolResult（若其中带 ToolResult 信息），否则 None。

    处理顺序：
    - ToolResult：原样返回；
    - dict：仅当含 success 且 content 为 str 时按 ToolResult 还原。**多余的键不会让它抛错**
      （pydantic v2 默认 extra='ignore'，实测 ToolResult(**{...,"extra_key":1}) 正常构造）；
      这里兜的是 success/content 类型不可强转的情况（如 success 传了 dict）→ None。
    - list（MCP adapters 形态）：逐块取 text 拼成一段、json.loads；命中
      "success + content:str" 才还原为 ToolResult，否则 None。
    其余形态（str 等）一律 None。
    """
    if isinstance(result, ToolResult):
        return result
    if isinstance(result, dict):
        if "success" in result and isinstance(result.get("content"), str):
            try:
                return ToolResult(**result)
            except Exception:
                return None
        return None
    if isinstance(result, list):
        # MCP 适配器形态：content-block dict 列表，ToolResult 被 FastMCP JSON 化进 text
        try:
            data = json.loads(_join_block_texts(result))
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(data, dict) and "success" in data and isinstance(data.get("content"), str):
            try:
                return ToolResult(**data)
            except Exception:
                return None
    return None


def format_tool_result(result) -> str:
    """把工具返回统一格式化为模型可见的字符串。

    处理顺序：
    - `ToolResult`：取 `content`，若有 `error_type` 则加 `[error_type] ` 前缀，
      让模型一眼看出这是哪一类失败（如 "[workspace_violation] 路径…"）。
    - `dict`：优先取 `content` 字段；若 content 是 content-block 列表则逐个取
      `text` 拼接（兼容 langchain MCP 适配器的返回形态）。
    - `list`：MCP 工具经 ToolNode 拿到的真实形态——langchain MCP 适配器把每个
      content block 转成 dict（text/id 等），FastMCP 又把工具返回的 ToolResult
      整包 JSON 化进 text。这里逐个取 `text`（丢弃随机 id 噪声），json.loads
      还原 ToolResult 后交上面 ToolResult 分支；还原失败（非 JSON / 非
      ToolResult 形态，如未来直接返回 str 的工具）则拼接后原样透传。
    - 其它（str 等）：`str()` 原样透传。

    注意：模型读到的文本由这里决定，而不是 Python 侧的 `ToolResult.__str__`
    （MCP 跨进程返回时走的是 FastMCP 的 JSON 序列化，见 mcp_service/utils.py 注释）。
    """
    if isinstance(result, ToolResult):
        prefix = f"[{result.error_type}] " if result.error_type else ""
        return prefix + result.content

    if isinstance(result, dict):
        content = result.get("content", result)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return _join_block_texts(content)
        return str(content)

    if isinstance(result, list):
        text = _join_block_texts(result)
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
        if isinstance(data, dict) and "success" in data and isinstance(data.get("content"), str):
            return format_tool_result(ToolResult(**data))
        return text

    return str(result)
