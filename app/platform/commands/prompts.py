"""提示词型命令的**载荷**：命令触发时才投给模型的那一段预设提示词。

与 `app/agent/prompt.py` 是两码事：
- `app/agent/prompt.py` = 模型**每轮**都会读到的行为契约（SYSTEM_PROMPT / worker 人设 / 工作区块）；
- 本模块 = **某条命令**触发时才投出去的一次性提示词（`/init`、`/fast readme`；将来 `/compact` 的也放这里）。

载荷可以是**常量字符串**（`/init`），也可以是**收 args 的函数**（`/fast readme [语言]` 按参数
渲染不同提示词）——命令表两种都收，见 `app/platform/commands/__init__.py`。

命令的注册与解析在那边的 `__init__.py`——它只管"名字 → 载荷 / handler"，这类大段散文留在
本模块，免得机制文件被文本淹掉。
"""

INIT_PROMPT = """\
请生成或更新本工作区的项目画像文件 AGENT.md。

"生成或更新"都由**写入**完成：文件不存在就新建，已存在就更新——不要预设它一定是新建的。

AGENT.md 会随仓库走、并在之后**每个会话开始时被读入系统上下文**，所以它写的是"这个项目长期
成立的事实"，不是本次任务的记录（临时结论写进长期记忆或直接答复即可）。

怎么做：
1. 先摸清现状，**不要凭猜测写**：get_directory_tree / list_dir 看布局，读 README（若有）、
   依赖与构建清单（pyproject.toml / package.json / requirements.txt / Makefile 等），
   必要时用 search_content 定位主流程、入口与测试目录。
2. 落盘 = **写入工作区根目录的 "AGENT.md"**（相对路径直接写 "AGENT.md"）：
   - **不存在** → 用 create_file 新建；
   - **已存在** → 先 read_file 看懂现状再更新：改动小用 edit_file 逐处刷新，改动面覆盖大半
     才用 write_file 整文件重写；两条路都要**保住仍然成立的内容**，别把已攒下的共识写丢。
   （AGENT.md 是可反复刷新的文件，不必一次写到完美。）
3. 内容按"项目整体画像"分块写清：
   - 这个项目是做什么的（一两句）；
   - 目录与技术栈：关键目录各放什么、用什么语言/框架/包管理器；
   - 主流程与入口：程序从哪进、一次请求或一次运行怎么走；
   - 怎么跑：构建、启动、跑测试的**具体命令**；
   外加**容易踩的坑或必须遵守的约定**（如测试必须怎么跑、哪些目录不要动）。
4. 写法：中文、简明分节（Markdown 小标题）、只写事实；**不要把大段代码抄进来**，需要时给出
   文件路径让人自己去看；篇幅控制在能一眼扫完的量级。

完成后用一两句说明 AGENT.md 落在哪、这次是新建还是更新、写了哪几块，不必复述全文。\
"""


# ------------------------- /fast readme [语言] -------------------------

# 不给语言时用它
_DEFAULT_README_LANGUAGE = "English"

# 语言参数别名 → 撰写语言名（提示词里直接用的那个词）。大小写不敏感；也收中文说法。
_LANGUAGES: dict[str, str] = {
    "en": "English", "eng": "English", "english": "English", "英文": "English", "英语": "English",
    "zh": "简体中文", "cn": "简体中文", "zh-cn": "简体中文", "chinese": "简体中文",
    "中文": "简体中文", "汉语": "简体中文", "简体": "简体中文",
    "zh-tw": "繁體中文", "tw": "繁體中文", "繁体": "繁體中文",
    "de": "Deutsch", "german": "Deutsch", "德语": "Deutsch",
    "jp": "日本語", "ja": "日本語", "japanese": "日本語", "日语": "日本語", "日文": "日本語",
    "fr": "Français", "french": "Français", "法语": "Français",
    "es": "Español", "spanish": "Español", "西班牙语": "Español",
    "ru": "Русский", "russian": "Русский", "俄语": "Русский",
    "ko": "한국어", "kr": "한국어", "korean": "한국어", "韩语": "한국어",
    "pt": "Português", "portuguese": "Português", "葡萄牙语": "Português",
    "it": "Italiano", "italian": "Italiano", "意大利语": "Italiano",
}


def readme_language(args: str) -> str:
    """把 `/fast readme` 的语言参数解析成**撰写语言名**。

    - 没给参数 → 默认 `English`；
    - 命中别名表（大小写不敏感，也认"中文/日语"这类说法）→ 对应的语言名；
    - 不认识的记号（如 `vi`、`pt-BR`）→ **原样返回**：多数模型认语言代码，比悄悄退回英语更贴近
      用户的意图（写错语言比写得生硬更难接受）。
    """
    token = (args or "").strip().split(" ")[0]
    if not token:
        return _DEFAULT_README_LANGUAGE
    return _LANGUAGES.get(token.lower(), token)


def fast_readme_prompt(args: str) -> str:
    """`/fast readme [语言]` 的载荷：让 agent 快速为项目写一份 README（用指定语言）。"""
    language = readme_language(args)
    return f"""\
请为当前工作区快速写一份 README.md，**全文用 {language} 撰写**（标题、正文、示例说明都用该语言；
代码块、命令、路径、专有名词该保留原文就保留）。

目标是"新人一分钟看懂这是什么、怎么跑起来"，**不是**写成一本手册——篇幅克制、能扫完。

怎么做：
1. 先扫一眼现状，**不要凭猜测写**：get_directory_tree / list_dir 看布局，读依赖与构建清单
   （pyproject.toml / package.json / requirements.txt / Makefile 等）、入口文件、测试目录；
   若工作区里已有 README.md，先 read_file 看懂它，**在此基础上更新**而不是推倒重来。
2. 落盘 = **工作区根目录的 "README.md"**（相对路径直接写 "README.md"）：不存在用 create_file；
   已存在用 edit_file 逐处更新（改动面覆盖大半才用 write_file），保住仍然成立的内容。
3. 建议结构（按项目实际情况取舍，别硬凑）：
   - 一句话：这是什么、解决什么问题；
   - 快速开始：安装依赖、配置（需要哪些环境变量/密钥）、启动命令；
   - 核心特性：3–6 条，每条一句、说清价值；
   - 怎么跑测试 / 构建；
   - 目录结构或架构要点（简短即可，别贴大段代码）；
   - 许可、贡献等（仓库里能看出来的才写）。
4. 纪律：只写**能核实的事实**——命令、路径、配置项都要与工作区对得上，**不要编造**不存在的功能；
   不确定的地方宁可不写。

完成后用一两句说明 README.md 落在哪、这次是新建还是更新、写了哪几块，不必复述全文。\
"""
