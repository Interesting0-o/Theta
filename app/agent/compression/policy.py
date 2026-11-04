"""压缩策略表：哪些工具可压、要归档、受保护。

**策略来源单一**：编排工具集合从 `tool.json` 的 `source` 派生（`gates.ORCHESTRATE_SOURCES`），
其余保护表按工具语义分为四类。轮内与轮末共享这一份，不再各留一张硬编码名单。
"""
from app.agent import gates

# 编排/state 工具：只操作会话 state，不是可重取的外部结果——折掉就是误伤。
def orchestrate_tools() -> frozenset[str]:
    """从 tool.json 派生编排工具集合（策略单点：`gates.tool_config`）。"""
    return frozenset(
        name
        for name, conf in gates.tool_config().items()
        if conf.get("source") in gates.ORCHESTRATE_SOURCES
    )


# 进笔记区的工具：仅联网四件套——其结果花钱、不可免费重取（时效性/可重取性判据）；
# 其余工具（文件读/命令输出/git/终端）结果时效强、重取便宜，折叠即弃、绝不进笔记。
ARCHIVE_TOOLS: frozenset[str] = frozenset(
    {"web_search", "extract_urls", "crawl_website", "deep_research"}
)

# 状态回执：每个工具只保留最近一条；旧回执的内容已由 current_plan / RUNTIME_STATE 承载。
STATE_RECEIPT_TOOLS: frozenset[str] = frozenset({
    "create_plan", "update_plan_step", "clear_plan",
    "read_memory", "write_memory", "get_skill", "drop_skill",
})

# 写操作回执：Phase 1 保持原文（写文件的正文在 tool_call args 里，Phase 2 再处理）。
WRITE_TOOLS: frozenset[str] = frozenset({
    "write_file", "edit_file", "create_file", "create_dir",
    "delete_file", "delete_dir", "copy_path",
})

# 不进轮内移动窗口的工具：
# - web 四件套：正文由轮末归档进 notes，轮内压掉等于丢研究原料；
# - ask_user：用户决定，必须保留；
# - dispatch_subtasks：worker 结论本身就是"结论"，不是可重取的 dump；
# - 状态回执与写操作回执：见上。
NO_MASK_TOOLS: frozenset[str] = frozenset({
    "web_search", "extract_urls", "crawl_website", "deep_research",
    "ask_user", "dispatch_subtasks",
}) | STATE_RECEIPT_TOOLS | WRITE_TOOLS
