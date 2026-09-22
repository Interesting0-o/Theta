from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.exception import ConfigError
from app.resource.paths import PROJECT_ROOT
from app.schema.approval_schema import DEFAULT_INBOX_PORT

# .env 固定在 theta 项目根，不随进程 cwd 漂移——使 TUI 可在任意"目标工作区目录"启动：
# 工作区 = 启动目录(cwd)，而代码与 .env 仍定位到项目根。项目根本身归 app/resource/paths.py。
_ENV_FILE = str(PROJECT_ROOT / ".env")


class Settings(BaseSettings):

    #聊天模型配置
    CHAT_MODEL_API_KEY:SecretStr
    CHAT_MODEL_URL:str
    CHAT_MODEL_NAME:str

    # 思考模式（可选，三态）：`enabled` = 强制思考 / `disabled` = 不思考 / **留空 = 不下发该参数**、
    # 完全交给服务端默认（当前智谱 GLM-4.7 系列默认就开思考）。
    # 与 GITHUB_TOKEN 同理带空默认：它是**可选的厂商参数**，不是"必须配的东西"——不填时行为与
    # 没有这个键完全一样。取值 → `extra_body` 的映射单点在
    # app/agent/nodes.py::LLMNode.thinking_extra_body。
    CHAT_THINKING: str = ""

    #Tavily搜索api
    TAVILY_API_KEY: SecretStr

    # GitHub 技能凭证（能力型技能用，见 app/resource/skills.py 与 docs/SKILL_DESIGN.md §13.5）。
    # **有意偏离"所有字段无默认值"那条约定**：技能是可插拔的，它的凭证缺失不该让核心启动就
    # ValidationError；空值 = 该技能拒绝加载（用户能看懂的回执），而不是整个程序起不来。
    # 核心字段（CHAT_* / EMBEDDING_* / TAVILY_API_KEY）仍保持"键必须都出现"。
    GITHUB_TOKEN: SecretStr = SecretStr("")

    # 主侧审批收件箱（ApprovalInboxServer）的端口：可选键，默认 = schema 里的共用常量
    # （worker 侧也由它派生，见 app/schema/approval_schema.py）。
    # **为什么必须走 Settings 而不是 `os.environ`**（2026-09-22 修）：pydantic-settings 读 `.env`
    # 但**不把值注入 `os.environ`**，所以"读 os.environ"等于让"写进 .env"静默失效——而这个旋钮在
    # README / CLAUDE.md / docs/MULTI_AGENT.md 里都是写给用户调的。走 Settings 后**两条路都生效**
    # （shell env 优先级高于 .env 文件）。
    AGENT_INBOX_PORT: int = DEFAULT_INBOX_PORT

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore"
    )


# 技能可声明的 env 键 → Settings 属性名的**白名单**（§13.5）。
#
# 为什么必须有白名单：`skills/<name>/skill.json` 是**技能作者写的文件**，若允许它任意点名
# `${VAR}`，等于让技能配置读走进程里任何一个环境变量（含 CHAT_MODEL_API_KEY）。所以只转发
# **这里显式列出**的键，不做通用 env 透传。
#
# 为什么值必须从 `get_settings()` 取而不是 `os.environ`：`.env` 是 pydantic-settings 自己读的
# 文件，其键值**不在 os.environ 里**——用 `os.environ.get("GITHUB_TOKEN")` 会对用户明明写进
# `.env` 的键报"未配置"。
# ---------------------- 跨模块共用的量级常量（不是 .env 键） ----------------------
# `config.py` 不只管"环境配置"：**一处值、多处调用**的口径也放这里（2026-09-22 用户拍板：
# "多处调用可以进 config"——config 是全局权威来源，避免同一量级在各模块各写一遍、再靠注释
# 互相指认"同量级"）。
#
# ⚠️ 与 `Settings` 的区别：这里**不是 .env 键**，改它要动代码（照 §4.3：口径留代码）。
# 若哪天某个量真成了用户旋钮（随部署变、改它不该动代码），再把它升级成 `Settings` 字段。
#
# 文本预算：**往上下文 / 历史里放一段正文的字符上限**。四个消费者各留自己的名字（语义不同：
# 记忆注入 / 画像注入 / 技能正文 / 归档正文），但值只有这一个出处——改这里四处一起动。
TEXT_BUDGET_CHARS = 8000

# `CHAT_THINKING` 的取值域（与该配置键**同处声明**：取值域属于键，不归消费方）。
# 消费方 `LLMNode.thinking_extra_body()` 用它校验并翻成 provider 参数。
THINKING_MODES: tuple[str, ...] = ("enabled", "disabled")


SKILL_ENV_WHITELIST: dict[str, str] = {
    "GITHUB_TOKEN": "GITHUB_TOKEN",
}


@lru_cache()
def get_settings() -> Settings:
    return Settings()#type:ignore


def skill_env(declared: list[str]) -> dict[str, str]:
    """把技能声明的 env 键解析成 {键: 明文值}；键不在白名单或值为空 → 抛 ConfigError（带键名）。

    调用方（`get_skill`）据此**拒绝加载**该技能并给出可行动回执——比"起一个注定失败的 server
    再让模型猜原因"好得多。
    """
    if not declared:
        return {}

    settings = get_settings()
    resolved: dict[str, str] = {}
    unknown: list[str] = []
    empty: list[str] = []
    for key in declared:
        attr = SKILL_ENV_WHITELIST.get(key)
        if attr is None or not hasattr(settings, attr):
            # hasattr 校验让白名单里的笔误**响亮失败**，而不是 AttributeError 冒到工具层
            unknown.append(key)
            continue
        value = getattr(settings, attr)
        plain = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        if not plain:
            empty.append(key)
            continue
        resolved[key] = plain

    if unknown:
        raise ConfigError(
            f"技能声明了未被允许的环境变量 {unknown}——"
            f"可用键见 app/config.py::SKILL_ENV_WHITELIST（若确需新增，请在那里登记）"
        )
    if empty:
        raise ConfigError(f"环境变量未配置（.env 里为空）：{'、'.join(empty)}")
    return resolved