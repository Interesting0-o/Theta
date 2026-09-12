from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

from pydantic import BaseModel


class ToolResult(BaseModel):
    success: bool
    content: str
    error_type: str | None = None


# 计划每步的状态枚举（与运行时 current_plan 的元素字段保持一致）
PlanStatus = Literal["pending", "in_progress", "done"]


class PlanStep(TypedDict):
    # 创建时分配的序号（"1","2",...）
    id: str
    task: str
    status: PlanStatus


class NoteEntry(TypedDict):
    """会话级笔记条目（外层 dict 的 key 即 ref，如 "notes#r3"；随 checkpoint 存亡）。

    定位：给"花钱、不可免费重取"的大结果（联网检索等）一个指针常驻、正文按需取回的落点，
    让折叠可以更激进。不做跨会话语义库——值钱结论由 agent 主动 promote 进 repo。
    kind 约定：research / web / crawl / extract / command_output …（系统按 kind + 体积阈值归档）。
    topic/tags：归档时留空，需整理时由模型调 update_note_tags / notes_by_topic 懒分类。
    created：unix 秒；size：content 的字符数；content：markdown payload（消费者是模型）。
    """
    kind: str
    title: str
    source_tool: str
    content: str
    created: int
    size: int
    topic: NotRequired[str]
    tags: NotRequired[list[str]]


@dataclass(frozen=True)
class MCPToolSpec:
    """一条 MCP 工具的静态 schema（**不含会话**，故可跨会话复用、按工作区缓存）。

    与 `ToolResult` 的分工：那是工具**执行后**的返回形状，这是工具**调用前**的描述形状。
    生产者 = `app/agent/mcp.py`（从适配器列到的工具里取 schema），消费者 = 同模块造 shim 工具、
    以及二期能力型 skill 的工具注册表。

    `server` 必须显式记着：`web_search` 这个**工具名与 server 名同名**，任何"按名字反推 server"
    的做法都会错。`args_schema` 是 MCP 的 `inputSchema` **原样**（camelCase、可能带 `$defs`/`anyOf`）——
    不要规范化成 pydantic 模型、不要转 snake_case，schema 一变就污染 prompt cache 与评估复现。

    - server：MCP server 名（file_io / terminal / git / web_search / 将来的 skills/<name>）；
    - name：工具名（模型可见、也是 tool.json 审批策略的查表键）；
    - description：工具描述（模型可见）；
    - args_schema：MCP inputSchema，原样搬运；
    - metadata：适配器从 MCP 注解/`_meta` 取的元信息（当前无消费者，保留以备将来）。
    """

    server: str
    name: str
    description: str
    args_schema: dict
    metadata: dict | None


@dataclass(frozen=True)
class MemoryEntry:
    """长期记忆的一条（`resource/<ws_key>/memory/memory.md` 里的一段，跨会话长存）。

    与 PlanStep / NoteEntry 的差别在**载体**：那两个是会话内的 state 切片、要经 checkpoint
    序列化，所以用 TypedDict；MemoryEntry 是解析记忆文件得到的值对象，从不进 state，
    因此用 frozen dataclass——不可变、按属性访问，四要素即文件里的一行标题 + 一段正文。

    - key：单调递增编号（m1, m2, …），供 read_memory 取单条 / write_memory 按 key 覆写；
    - type：user-preference / decision / convention / project-fact（见 §6）；
    - date：**首次记入日** YYYY-MM-DD——覆写保留原日期，不重排、不重新编号；
    - content：条目正文（不含标题行）。

    设计见 docs/LONG_TERM_MEMORY.md §3；读写与解析在 app/agent/memory.py。
    """

    key: str
    type: str
    date: str
    content: str


@dataclass(frozen=True)
class SkillMeta:
    """一个**已安装技能**的元信息——即技能目录里的一条（skills/<name>/SKILL.md）。

    与 `MemoryEntry` 同族：都是"**解析出来的值对象**"（这里由 `app/agent/skills.py::scan_skills`
    从 SKILL.md 的 frontmatter 解析），且**消费者跨模块**（`app/agent/tools.py` 的
    `get_skill`/`drop_skill`、`app/agent/nodes.py::LLMNode` 的注入块、构图期把目录烤进 docstring），
    所以落在这里，而不是定义在扫描器旁边。

    - name：技能名（frontmatter 的 `name`，缺省退化用目录名）——模型传给 `get_skill` 的标识；
    - description：**"什么时候用 + 解决什么"**（不是"是什么"）——决定模型会不会、该不该加载它；
    - dir_name：技能**目录名**（`skills/<dir_name>/`）。它是另一条标识：能力型技能要按它拼
      import 路径（`skills.<dir_name>.server`）与审批策略的 `source`（`skills/<dir_name>`）——
      **绝不拿 frontmatter 的 name 拼路径**（同"绝不拿模型给的名字拼路径"那条安全纪律）；
    - capability：是否能力型（目录里有 `server.py` 且目录名是合法 Python identifier）。false 时
      该技能只有正文、`get_skill` 不起进程；
    - path：SKILL.md 的**绝对路径字符串**（值类型而非 `Path`：schema 里的字段一律 JSON 友好，
      读盘处再转 `Path`）。正文按需从这个路径读，不在本结构里缓存。
    """

    name: str
    description: str
    dir_name: str
    capability: bool
    path: str