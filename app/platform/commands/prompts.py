"""提示词型命令的**载荷**：命令触发时才投给模型的那一段预设提示词。

与 `app/agent/prompt.py` 是两码事：
- `app/agent/prompt.py` = 模型**每轮**都会读到的行为契约（SYSTEM_PROMPT / worker 人设 / 工作区块）；
- 本模块 = **某条命令**触发时才投出去的一次性提示词（`/init`、`/fast readme`；将来 `/compact` 的也放这里）。

载荷可以是**常量字符串**（`/init`），也可以是**收 args 的函数**（`/fast readme [语言]` 按参数
渲染不同提示词）——命令表两种都收，见 `app/platform/commands/__init__.py`。

两条命令共用的"落盘纪律 + 回执"收在 `_landing_and_report()`：同一口径只写一处，免得两份提示词
各改各的、慢慢走散。
"""


def _landing_and_report(filename: str) -> str:
    """两条提示词共用的收尾：落盘纪律（新建/更新两条路）+ 回执要求。"""
    return f"""\
落盘 = **写入工作区根目录的 "{filename}"**（相对路径直接写 "{filename}"）：
- **不存在** → 用 create_file 新建；
- **已存在** → 先 read_file 看懂现状再更新：改动小用 edit_file 逐处刷新，改动面覆盖大半才用
  write_file 整文件重写；两条路都要**保住仍然成立的内容**，别把已攒下的共识写丢。

完成后用一两句说明 {filename} 落在哪、这次是新建还是更新、写了哪几块，不必复述全文。\
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
2. 内容按"项目整体画像"分块写清：
   - 这个项目是做什么的（一两句）；
   - 目录与技术栈：关键目录各放什么、用什么语言/框架/包管理器；
   - 主流程与入口：程序从哪进、一次请求或一次运行怎么走；
   - 怎么跑：构建、启动、跑测试的**具体命令**；
   外加**容易踩的坑或必须遵守的约定**（如测试必须怎么跑、哪些目录不要动）。
3. 写法：中文、简明分节（Markdown 小标题）、只写事实；**不要把大段代码抄进来**，需要时给出
   文件路径让人自己去看；篇幅控制在能一眼扫完的量级。（AGENT.md 是可反复刷新的文件，不必一次
   写到完美。）
4. """ + _landing_and_report("AGENT.md")


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
    """`/fast readme [语言]` 的载荷：按开源项目 README 的通行结构，快速生成一份。"""
    language = readme_language(args)
    return f"""\
请为当前工作区生成或更新一份**开源项目风格**的 README.md，**全文用 {language} 撰写**
（标题、正文、示例说明都用该语言；代码块、命令、路径、专有名词该保留原文就保留）。

好 README 的共性可以参照：React（简洁有力、快速开始最突出）、axios（特性列表清晰、示例即文档）、
Rust（复杂项目的信息分层）、oh-my-zsh（社区项目的贡献指引与展示感）——**按本项目体量取舍**，
小项目别硬凑大结构。

一、结构与优先级（从上到下）
1. **项目标识**：项目名（可带一句 slogan）+ 徽章 + 一句话说清"它解决什么问题"。
   徽章**只放能从仓库核实的**（许可证、语言/版本可取自 pyproject.toml / package.json 等；
   仓库确有 CI 配置时才加构建状态）——**不要编造**下载量、star 数、CI 状态这类你无法核实的东西。
2. **简介**：2–3 句讲"是什么、为什么存在"，先讲价值；别一上来堆技术栈与依赖版本。
3. **功能特性**：bullet 列核心能力，突出与同类项目的**差异**（确有对比必要时可用 ✅/❌ 表）。
4. **快速开始**（最重要，决定别人能不能在 60 秒内跑起来）：安装命令 + **最小可运行示例**。
   - 命令要从工作区的**真实配置**里取（pyproject / package.json / Makefile / 现有脚本），
     别凭印象写"常见命令"；
   - 示例要**复制粘贴就能跑**：别缺 import、别缺必要配置；需要环境变量/密钥时写明要哪些、
     从哪来（如 .env.example）。
5. **更多文档**：**不要把文档都塞进 README**——用链接指向 docs/、Wiki、站点（本仓 docs/README.md
   这类索引就是例子）。
6. **贡献指南**：一句流程 + 指向 CONTRIBUTING.md；仓库里没有就如实说明怎么提 issue/PR，别编造模板。
7. **许可证**：写明类型（MIT / Apache-2.0 / …）并指向 LICENSE；**仓库里没有就如实说"暂未指定"**。

二、可选章节（项目确实有才写，不硬凑）：架构/设计图、Changelog、API 速查、FAQ、致谢、赞助。

三、写作原则
- **先写给人看，再写给机器看**：读者先要知道"这对我有什么用"，技术细节后置。
- **信息分层**：80% 的人需要的东西放在前 30% 的篇幅，细节下沉到链接的文档里。
- **命令与示例必须当前有效**：过时的安装命令比没有 README 更糟——所有命令、路径、配置项都要与
  工作区对得上；**不确定的宁可不写，也不要编造**。
- 篇幅克制（读完不超过几分钟），不要贴大段源码。

四、语言与文件
- 本次**只写 README.md 这一个文件**（多写一个文件就多一次写入审批）。
- 只有你判断该项目明确需要双语时才另存副本，并按下述约定命名、在开头互相链接：
  `README.md`（主语言）+ `README.zh-CN.md` / `README.en.md`（另一语言）。**默认不做**。

怎么做：
1. 先扫现状再写：get_directory_tree / list_dir 看布局，读依赖与构建清单、入口文件、测试目录，
   以及**已有的 README.md**（有就先 read_file 看懂，在它基础上更新，而不是推倒重来）。
2. """ + _landing_and_report("README.md")
