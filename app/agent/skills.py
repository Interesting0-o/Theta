"""技能（skill）：按需加载的"领域包"——扫描 / 解析 / 渲染的**只读单点**。

设计见 docs/SKILL_DESIGN.md。一句话：把某个领域要用的**工具**与该领域的**纪律**打成一包，
平时既不占上下文、也不起进程，需要时才整个拉进来（渐进披露）。

**源只有一个**：项目根的 `skills/`，即 `<项目根>/skills/<name>/SKILL.md`。
（设计 §8.2 曾规划第二个"工作区源" `<workspace>/.theta/skills/`，2026-09-12 决定**先不做**：
单源少掉"重名谁覆盖谁"与"工作区外的目录要不要进沙箱故事"两处复杂度。以后真需要再加回来，
那时重新论证比留一段没人跑的代码便宜。）

**位置**：与 `app/agent/memory.py` 并列，同属"工作区内容 → 注入"的通道。**独立成模块**
（而非并进 `LLMNode`）是因为消费者跨三个文件：`tools.py` 的 `get_skill`/`drop_skill`、
`nodes.py::LLMNode` 的注入块、`graph.py` 构图期把目录烤进 docstring。

**一个技能 = 一个目录**：`<name>/SKILL.md`（+ 可选 `server.py`、可选 `skill.json`）。
有 `server.py` 是**能力型**（get_skill 会把它的工具拉进会话）、没有是**知识型**（只有正文）。
本模块只管**读**：扫描 / 解析 / 渲染；拉起 server、登记工具、关运行体都在 `tools.py`
（`get_skill`/`drop_skill`/`SessionToolset`）+ `app/agent/mcp.py`。属二期（§13）。

三条纪律：

- **只读**。技能是用户写的文件，agent 不改它——所以本模块没有写单点（与 `memory.py` 不同）。
- **`name` 白名单校验，绝不拼路径**。`name` 来自模型（`get_skill(name)`），而按 §2.2 的不变量是
  **host 侧读 `SKILL.md`**：`read_body` 只接受扫描结果里已有的名字，`get_skill("../../etc/passwd")`
  不会变成一次任意文件读。技能目录在**工作区之外**（项目根），file_io 的沙箱管不到这里，必须自查。
- **正文不落 state**。state 只存技能 `name` 清单（`loaded_skills`），正文每轮由 `skills_block`
  从盘上读——口径同记忆的"文件即真值"。这条让 §3.5 的铁律（正文只有一个家）**结构性成立**：
  工具回执里不带正文、state 里不存正文，卸载就是删一个名字，不存在"两份拷贝"。

本模块只依赖 stdlib + `app/schema`（`SkillMeta` 是只有一个属性的值对象，按项目纪律落 app/schema，
不放业务模块）。
"""
from __future__ import annotations

import json
import keyword
import logging
from pathlib import Path

from app.schema.agent_schema import SkillMeta, SkillPreflight

logger = logging.getLogger(__name__)

# ---------------------- 常量 ----------------------

# 项目根：技能源的位置（与 graph.py / tools.py 同一算法）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 技能源目录（随 Theta 出厂；首个技能 = skills/github/）
SKILLS_DIR = _PROJECT_ROOT / "skills"

# 技能定义文件名。固定叫 SKILL.md（两型都是），扫描逻辑因此只有一套。
SKILL_FILENAME = "SKILL.md"

# 能力型的标志文件：skill 目录里有它就按"能力型"处理（模块名固定，启动器才能通用化——
# `python -m skills.<dir>.server`，不为每个技能记模块名，见 §8.1）。
SERVER_FILENAME = "server.py"

# 技能自带的启动配置（可选）：目前只声明"需要哪些 env 键"（由 host 侧白名单转发，见 §13.5）。
SKILL_CONFIG_FILENAME = "skill.json"

# 技能包的顶层包名（能力型按 `"<SKILLS_PACKAGE>.<dir>.server"` 拼 import 路径）。
# 模块级常量便于测试 monkeypatch —— 测试把临时技能目录挂到另一个包名下，子进程才 import 得到。
SKILLS_PACKAGE = "skills"

# 正文注入预算（字符，口径同 memory.MEMORY_INJECT_CAP / LLMNode.AGENT_MD_INJECT_CAP）。
# 满了**不淘汰**：由 get_skill 拒绝新加载并要求模型先 drop_skill（§3.5——知识型没有可观测的
# "使用事件"，自动淘汰无法定义）。
SKILL_BODY_BUDGET = 8000

# 目录条数上限（§3.6：目录与正文是两个预算，别混用一个数）。目录**不可淘汰**（淘汰 =
# 不可发现 = 技能等于不存在），溢出只能截断——所以设宽一点，且溢出必须告警。
SKILL_CATALOG_CAP = 60


# ---------------------- 解析 ----------------------


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """切出 `---` 包围的 frontmatter 与正文。

    **自写最小实现，不引 YAML**（§9：别默认蹭传递依赖）——只认单行 `key: value`，
    不支持嵌套 / 列表 / 多行值。技能的 frontmatter 只有 `name` 和 `description` 两个键，
    用不上完整 YAML；真需要时再加。

    没有 frontmatter（或 `---` 未闭合）→ `({}, 原文)`，正文原样返回。
    """
    if not text.startswith("---"):
        return {}, text

    lines = text.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text

    meta: dict[str, str] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    return meta, "\n".join(lines[end + 1 :]).strip()


def _read_skill_md(path: Path) -> tuple[dict[str, str], str]:
    return _split_frontmatter(path.read_text(encoding="utf-8"))


# ---------------------- 扫描 ----------------------


def _is_module_name(name: str) -> bool:
    """目录名能不能拿来拼 `python -m skills.<目录名>.server`。

    `isidentifier()` 挡得住点 / 空格 / `-`，**挡不住关键字**：`"class"`、`"for"` 都是合法
    identifier，可 `import skills.class.server` 是 SyntaxError——那样的技能会被标成能力型、
    目录里打上 `[带工具]`，却永远加载不上（正好违反"目录不撒谎"那条不变量）。
    """
    return name.isidentifier() and not keyword.iskeyword(name)


def scan_skills() -> dict[str, SkillMeta]:
    """扫描技能源，返回 {name: SkillMeta}；源目录不存在 → 空表。

    坏技能（有目录但没 SKILL.md、或没写 description）**跳过并告警**，不影响其余技能。
    """
    found: dict[str, SkillMeta] = {}
    if not SKILLS_DIR.is_dir():
        return found

    for entry in sorted(SKILLS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / SKILL_FILENAME
        if not skill_md.is_file():
            # 目录里没有 SKILL.md：不是技能（可能是别的杂物），不算坏——静默跳过
            continue
        try:
            meta, _body = _read_skill_md(skill_md)
        except (OSError, UnicodeDecodeError) as exc:
            # 权限 / 读取失败 / **编码不是 UTF-8**：坏技能，别让一个目录拖垮整次扫描。
            # `UnicodeDecodeError` 不是 `OSError` 的子类（它是 ValueError）——漏掉它的话，
            # 一个编码坏掉的 SKILL.md 会让**整次扫描**抛异常，所有技能一起消失。
            logger.warning("技能 %s 读取失败，已跳过：%s", entry.name, exc)
            continue

        name = (meta.get("name") or entry.name).strip()
        description = (meta.get("description") or "").strip()
        if not description:
            # §9：description 决定模型该不该用——没有它这条目录对模型毫无信息量，不如不进目录
            logger.warning("技能 %s 缺 description（%s），已跳过", name, skill_md)
            continue

        existing = found.get(name)
        if existing is not None:
            # 技能名**必须唯一**：两个目录声明同一个 name 时若让后者覆盖前者，是静默的
            # "装了却不可发现"（§3.6 里最坏的失败形态）——模型看到的目录与 get_skill 查到的
            # 元信息会指向**两个不同的目录**，且其中一个再也取不到正文。响亮跳过后者。
            logger.warning(
                "技能名 %s 重复：目录 %r 与 %r 都声明了它，已跳过后一个（技能名必须唯一）",
                name,
                existing.dir_name,
                entry.name,
            )
            continue

        capability = (entry / SERVER_FILENAME).is_file()
        if capability and not _is_module_name(entry.name):
            # 目录名要用来拼 `python -m skills.<dir>.server` 与 `source: skills/<dir>`，
            # 含点 / 空格 / `-` 的名字（以及 `class` 这类关键字）拼不出可 import 的模块名。
            # **降级为知识型**而不是跳过整个技能：正文仍然有价值，只是它的工具不可用
            # （能力型要求 name == 目录名，见 get_skill 的校验）。
            logger.warning(
                "技能 %s 的目录名 %r 不是合法 Python identifier，降级为知识型（其 server.py 不会被拉起）",
                name,
                entry.name,
            )
            capability = False
        elif capability and name != entry.name:
            # 同一条纪律的另一半（与上面那半一起，让"目录里标 [带工具] 的技能一定加载得上"
            # 成为不变量）：启动模块与 tool.json 的 source 都按**目录名**走，name 与目录名
            # 不一致的能力型**永远过不了加载校验**——留在目录里就是谎报能力。
            # 同样降级为知识型而非跳过：正文仍然是可用的那一半。
            logger.warning(
                "技能 %s 的 name 与目录名 %r 不一致，降级为知识型（其 server.py 不会被拉起）",
                name,
                entry.name,
            )
            capability = False

        found[name] = SkillMeta(
            name=name,
            description=description,
            dir_name=entry.name,
            capability=capability,
            path=str(skill_md),
        )

    return found


def server_module(meta: SkillMeta) -> str:
    """能力型技能的启动模块名：`<SKILLS_PACKAGE>.<目录名>.server`。

    **用目录名而非 frontmatter 的 name**（同一条安全纪律：名字来自用户写的文件，不能拿来拼
    import 路径）；`scan_skills` 已保证目录名是**可 import 的模块名**（合法 identifier 且不是
    关键字）才会是能力型。
    """
    return f"{SKILLS_PACKAGE}.{meta.dir_name}.server"


def _read_skill_config(meta: SkillMeta) -> dict:
    """读技能目录里的 `skill.json`（可选）；不存在 / 读不动 / 不是 JSON 对象 → 空 dict + 告警。

    `read_skill_env` 与 `read_preflight` 共用这一次读盘——两者看的是同一份配置的两个键。
    """
    config_path = SKILLS_DIR / meta.dir_name / SKILL_CONFIG_FILENAME
    if not config_path.is_file():
        return {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # UnicodeDecodeError 单独列出来：它不是 OSError 的子类，而 `read_text` 先于
        # `json.loads` 跑——编码坏掉的 skill.json 会从这道缝里漏出去。
        logger.warning("技能 %s 的 %s 读取失败，按未声明处理：%s", meta.name, config_path, exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("技能 %s 的 skill.json 不是 JSON 对象，已忽略：%r", meta.name, raw)
        return {}
    return raw


def read_skill_env(meta: SkillMeta) -> list[str]:
    """读技能声明的 env 键（`skill.json` 的 `env: [...]`）；没有配置文件 / 读取失败 → 空表。

    只做**解析**：值怎么取由 host 侧白名单决定（`app/config.py::skill_env`），本模块不碰 settings。
    坏配置（非法 JSON、`env` 不是字符串列表）告警并按"没有声明"处理——那会让 server 起不来时
    由启动失败兜住，而不是在这里静默放行一个空凭证。
    """
    declared = _read_skill_config(meta).get("env")
    if declared is None:
        return []
    if not isinstance(declared, list) or not all(isinstance(k, str) for k in declared):
        logger.warning("技能 %s 的 skill.json 里 env 不是字符串列表，已忽略：%r", meta.name, declared)
        return []
    return declared


def read_requirements(meta: SkillMeta) -> list[str]:
    """读技能声明的第三方依赖（`skill.json` 的 `requirements: [...]`，**顶层 import 名**）；无声明 → 空表。

    只做**解析**（同 `read_skill_env`）。校验落在 `app/agent/tools.py::_missing_requirements`，那里
    能这么做的前提是：技能依赖装在**主 venv**（§5.1 已决不做隔离），而技能 server 用
    `sys.executable` 起——**与 host 同一个解释器**，所以"装没装"在 host 侧就能回答。

    坏声明（不是字符串列表）告警 + 按"没声明"处理：少一层保护，但不会因此让技能加载不了。
    """
    declared = _read_skill_config(meta).get("requirements")
    if declared is None:
        return []
    if not isinstance(declared, list) or not all(isinstance(k, str) for k in declared):
        logger.warning(
            "技能 %s 的 skill.json 里 requirements 不是字符串列表，已忽略：%r", meta.name, declared
        )
        return []
    return [name.strip() for name in declared if name.strip()]


def read_preflight(meta: SkillMeta) -> SkillPreflight | None:
    """读技能声明的**加载时体检**（`skill.json` 的 `preflight`）；未声明 / 形状不对 → None。

    只做**解析**（同 `read_skill_env`）：真去打那次请求是 host 动作，落在
    `app/agent/tools.py::_skill_preflight_line`。坏声明一律告警 + 按"没声明"处理——
    体检是锦上添花，不该因为它写错就让技能加载不了。
    """
    raw = _read_skill_config(meta).get("preflight")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.warning("技能 %s 的 preflight 不是对象，已忽略：%r", meta.name, raw)
        return None

    url = str(raw.get("url") or "").strip()
    bearer_env = str(raw.get("bearer") or "").strip()
    if not url or not bearer_env:
        logger.warning(
            "技能 %s 的 preflight 缺 url 或 bearer，已忽略：%r", meta.name, raw
        )
        return None
    if bearer_env not in read_skill_env(meta):
        # 体检用的键必须是本技能**声明过**的凭证键——否则等于让技能凭空读一个它没申请的凭据
        logger.warning(
            "技能 %s 的 preflight.bearer=%s 不在它声明的 env %r 里，已忽略",
            meta.name,
            bearer_env,
            read_skill_env(meta),
        )
        return None
    return SkillPreflight(url=url, bearer_env=bearer_env)


def body_of(meta: SkillMeta) -> str | None:
    """读某个已解析技能的正文（已剥 frontmatter）；读取失败 → None。"""
    try:
        _meta, body = _read_skill_md(Path(meta.path))
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("技能 %s 正文读取失败：%s", meta.name, exc)
        return None
    return body


def get_meta(name: str) -> SkillMeta | None:
    """按名取技能元信息；名字不在**扫描结果**里 → None。

    **这是那条安全纪律的落点**：只用模型给的名字去查扫描结果，**不拿它拼路径或 import 路径**——
    所以 `../`、绝对路径、符号链接在这里都不可能生效。技能目录在项目根、不在工作区内，
    file_io 的沙箱管不到这里，必须自查。
    """
    return scan_skills().get((name or "").strip())


def read_body(name: str) -> str | None:
    """按名读某技能的正文（已剥掉 frontmatter）；名字不在扫描结果里 → None。"""
    meta = get_meta(name)
    return body_of(meta) if meta is not None else None


# ---------------------- 渲染 ----------------------


def catalog_block(cap: int = SKILL_CATALOG_CAP) -> str:
    """渲染"可用技能"目录——构图期烤进 `get_skill` 的 docstring（§3.2）。

    只列 `name` + `description`：正文不进目录（目录每轮都在工具 schema 里，塞正文等于每轮
    全量付费，正是渐进披露要避免的）。装不下时**截断并告警**——截断意味着某些技能
    "装了却不可发现"，是最坏的失败形态，绝不静默（§3.6）。
    """
    metas = list(scan_skills().values())
    if not metas:
        return ""

    shown = metas[:cap]
    lines = [
        "可用技能（需要某领域的做法时，用 get_skill(name) 取回它的正文照做；"
        "标 [带工具] 的还会同时把该领域的工具拉起来）：",
        *(
            f"- {m.name}{' [带工具]' if m.capability else ''}: {m.description}"
            for m in shown
        ),
    ]
    if len(metas) > cap:
        dropped = "、".join(m.name for m in metas[cap:])
        logger.warning("技能目录超上限 %d，以下技能不可发现：%s", cap, dropped)
        lines.append(f"（另有 {len(metas) - cap} 个技能因目录上限未列出：{dropped}）")
    return "\n".join(lines)


def _usable_bodies(loaded: list[str]) -> dict[str, str]:
    """把 `loaded` 解析成 {name: 正文}，只留真的读得到正文的（扫一次盘）。"""
    metas = scan_skills()
    bodies: dict[str, str] = {}
    for name in loaded:
        meta = metas.get((name or "").strip())
        if meta is None:
            continue
        body = body_of(meta)
        if body:
            bodies[meta.name] = body
    return bodies


def bodies_size(loaded: list[str]) -> int:
    """已加载技能的正文总字符数——get_skill 据此判预算。读不到的技能按 0 计。"""
    return sum(len(body) for body in _usable_bodies(loaded).values())


def skills_block(loaded: list[str]) -> str:
    """把已加载技能的正文渲染成注入用的系统消息正文；没有可注入的 → `""`。

    抬头**列出当前已加载的技能**（§3.5）：这是模型决定卸哪个的依据，也让"某技能不在列表里了"
    成为卸载信号——否则 history 里那句"已加载 X"的回执还在，模型会以为纪律仍然生效。

    `loaded` 里读不到正文的名字（源被删 / 读取失败）**必须显式点名告警**，不能静默丢掉：
    静默丢 = 模型以为纪律还在（它是照 `loaded_skills` 里的名字行事），而正文与工具都已经是空的。
    点名 + "请 drop_skill 卸下"是可行动的最小补救（§13.3.2 的同一口径）。
    """
    if not loaded:
        return ""

    bodies = _usable_bodies(loaded)
    missing = [
        name for name in (n.strip() for n in loaded) if name and name not in bodies
    ]
    if not bodies and not missing:
        return ""

    parts: list[str] = []
    if bodies:
        parts.append(
            f"# 已加载技能\n\n当前已加载：{'、'.join(bodies)}（不再需要时用 drop_skill 卸下）"
        )
        parts.extend(f"## {name}\n\n{body}" for name, body in bodies.items())
    if missing:
        parts.append(
            "⚠️ 以下技能已不可用（源被删除或读取失败），正文与工具都没有了，"
            f"请用 drop_skill 卸下：{'、'.join(missing)}"
        )
    return "\n\n".join(parts)
