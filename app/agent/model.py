

from langchain.chat_models import init_chat_model
from app.config import get_settings
from app.exception import ConfigError

settings = get_settings()

# 思考模式：`CHAT_THINKING` 的三态（app/config.py）到此映射成 provider 参数。**换 provider 只改
# 这一个函数**——厂商各叫各的（智谱/GLM 是 `thinking.type`；部分平台 `enable_thinking`；OpenAI 自家
# `reasoning_effort`；DeepSeek 干脆靠换 model 名），把参数散到别处等于把厂商知识灌进核心节点。
#
# 三件记在案的事（都实测过，见 tests/test_thinking_config.py）：
# - **不配 `clear_thinking`**：服务端默认（`true`）就是"历史轮次的思考不随上下文给模型"，正是我们
#   要的（思考不占历史）。要"跨轮保留思考"是另一件大事（要求把思考逐字回传，与不占历史冲突）。
# - **思考 token 计入 `completion_tokens`，因此也计入 `max_tokens`**：实测 `enabled` + 偏小的
#   `max_tokens` 会让思考吃光预算、正文变空（300 时就是这样）。本仓库不设 `max_tokens`，故安全；
#   哪天要设，必须把思考的额度算进去。
# - **思考内容进不了 `messages`**：`langchain-openai` 有意不提取 `reasoning_content`（转换函数是
#   白名单式的），所以我们不需要剥离代码——但这条**依赖库的行为**，由上面那个测试文件里的契约
#   用例钉住：哪天库开始提取，用例会红，届时在 LLMNode 的写回点补剥离。
_THINKING_TYPES = ("enabled", "disabled")


def thinking_extra_body(mode: str) -> dict | None:
    """把 `CHAT_THINKING` 翻成 `ChatOpenAI` 的 `extra_body`；**不下发（留空）→ `None`**。

    用 `None` 而不是空 dict 表示"不下发"，是沿用库自己的约定：`_default_params` 里的
    `exclude_if_none` 会把值为 `None` 的键**整个丢掉**（`langchain_openai/chat_models/base.py:1266-1292`），
    所以 `extra_body=None` 与"根本没有这个参数"在请求体上等价——而空 dict 会以 `{}` 发出去。

    取值写错**当场抛** `ConfigError`，不静默按"没配"处理：静默的后果是"用户以为开了思考、
    实际没开"，而这种偏差在行为上几乎不可观测。
    """
    text = (mode or "").strip().lower()
    if not text:
        return None
    if text not in _THINKING_TYPES:
        raise ConfigError(
            f"CHAT_THINKING 只能是 {' / '.join(_THINKING_TYPES)}（或留空 = 不下发），收到的是 {mode!r}"
        )
    return {"thinking": {"type": text}}


def get_main_chat_model():
    return init_chat_model(
        model = settings.CHAT_MODEL_NAME,
        base_url = settings.CHAT_MODEL_URL,
        api_key = settings.CHAT_MODEL_API_KEY,
        model_provider="openai",
        # 思考模式：`None` = 不下发该参数（见 thinking_extra_body）。`extra_body` 是 ChatOpenAI 的
        # 一等字段（透传给 SDK 的 `extra_body=`），不是"未知 kwargs"——不会走 model_kwargs 那条
        # 带警告的路，字面与行为一致。
        extra_body=thinking_extra_body(settings.CHAT_THINKING),
    )


if __name__ == "__main__":
    from langchain.messages import HumanMessage
    messages = [
        HumanMessage(content=[
            {"type": "text", "text": "请描述这张图片。"},
            {
                "type": "image_url",
                "image_url": {
                    "url": "https://picx.zhimg.com/v2-94860a50353cc2bb967de2f1c294e13d_1440w.webp?consumer=ZHI_MENG"
                }
            }
        ])
    ]

    print(get_main_chat_model().invoke(messages))
