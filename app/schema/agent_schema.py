from dataclasses import dataclass
from typing import Literal, TypedDict, get_args

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
    created：unix 秒；size：content 的字符数；content：markdown payload（消费者是模型）。

    曾有一个 `kind` 字段（web / extract / crawl / research），**2026-09-22 删除**：它只被写、
    从没有任何地方读它或按它分派（悬空字段，同当年删掉的 topic/tags）——判据见
    docs/ARCHITECTURE.md §4.6。
    """
    title: str
    source_tool: str
    content: str
    created: int
    size: int


class ImageRef(TypedDict):
    """一条已发送图片的元数据（@路径 附图通道的记账，机制在 LLMNode.attach_images_to_payload）。

    与 PlanStep / NoteEntry 同族：进 state、随 checkpoint 序列化，所以用 TypedDict。
    **只存元数据**：base64 只活在 LLMNode 构造请求体副本的那一刻，从不进 messages/checkpoint
    ——图的"正文"就是盘上那个文件（口径同记忆的"文件即真值"），记账它只为"同一张别重发"。

    - path：解析后的**绝对路径**字符串（去重键；schema 字段一律 JSON 友好，不用 Path）；
    - mime：image/png 等，由扩展名映射（拼 data URI 时要用）。
    """

    path: str
    mime: str


# ---------------------- ask_user 的题目模态（跨模块词表：闸门校验 / 工具 schema / 面板渲染）---------------------
# 三处以上在用（gates 校验、tools 的签名、approvals 透传、panels 与 tui 的渲染分支），故按
# "多处使用 → app/schema" 定在这里；消费方一律 import，不再各写一份 `("one", "many")`。
AskSelectMode = Literal["one", "many"]
ASK_SELECT_ONE: AskSelectMode = "one"
ASK_SELECT_MANY: AskSelectMode = "many"
ASK_SELECT_DEFAULT: AskSelectMode = ASK_SELECT_ONE      # 模型不传时的默认（单选）
ASK_SELECT_MODES: tuple[str, ...] = get_args(AskSelectMode)


class AskAnswer(TypedDict):
    """一条 agent 提问的回答（人机闸门的产出，进 state 的 `ask_answers`，随 checkpoint 序列化）。

    与 PlanStep / NoteEntry / ImageRef 同族：**进 state → TypedDict**（值对象才用 frozen dataclass）。
    生产者 = `ReviewNode`（提问闸门），消费者 = `ask_user` 工具——**闸门的产出是 state，执行器消费
    state**，与 `approved_*` 队列同一条路子。工具靠它 + 自己的 options 渲染出给模型的回执。

    回答是**两段**：`option_indexes` 是用户选中的选项序号（**0 起始**；没选任何给定选项时为
    空列表），`supplement` 是用户自己补的一段话（可空）。**两段皆空 = 未回答**，消费方只认
    这一条判据（见 `app/schema/approval_schema.py::Decision.unanswered`）。

    单选与复选**共用这一个字段**（单选时长 0 或 1），与 `Decision.option_indexes` 同形状——
    它是跨 checkpoint 的值，**必须是 list 不能是 tuple**（tuple 过一遍序列化会变 list）。
    """

    option_indexes: list[int]
    supplement: str | None


@dataclass(frozen=True)
class MCPToolSpec:
    """一条 MCP 工具的静态 schema（**不含会话**，故可跨会话复用、按工作区缓存）。

    与 `ToolResult` 的分工：那是工具**执行后**的返回形状，这是工具**调用前**的描述形状。
    生产者 = `app/platform/mcp.py`（从适配器列到的工具里取 schema），消费者 = 同模块造 shim 工具、
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

    设计见 docs/LONG_TERM_MEMORY.md §3；读写与解析在 app/resource/memory.py。
    """

    key: str
    type: str
    date: str
    content: str


@dataclass(frozen=True)
class SkillMeta:
    """一个**已安装技能**的元信息——即技能目录里的一条（skills/<name>/SKILL.md）。

    与 `MemoryEntry` 同族：都是"**解析出来的值对象**"（这里由 `app/resource/skills.py::scan_skills`
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


@dataclass(frozen=True)
class SkillPreflight:
    """技能声明的**加载时体检**（`skill.json` 的 `preflight`，可选）。

    **为什么要有它**：能力型技能的凭证在 `skill_env` 那关只验"键有没有值"，验不出"值对不对"
    ——过期 / 被撤销 / 权限不足都要等第一次真请求才暴露。把这件事做成 MCP 工具，等于让模型自己
    想到去查（而它并不知道该查）；而这是**加载那一刻 host 就该知道**的事实：所以由 host 在
    `get_skill` 里打一次只读端点，结论随回执给出（不是工具，不进模型可见的工具表）。

    - url：探活的**绝对 URL**。应当是"只读、廉价、且能证明凭证有效"的端点（GitHub 是 `/user`）；
    - bearer_env：用哪个 env 键的取值作为 `Authorization: Bearer` 头。必须是该技能 `env` 里
      **声明过**的键——否则就是让技能凭空读一个它没申请的凭证。
    """

    url: str
    bearer_env: str