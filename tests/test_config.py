"""`app/config.py` 的配置契约：**哪些键能从 `.env` 读到**、默认值从哪来、空值怎么办。

为什么单开一条：2026-09-22 修掉一个"承诺未兑现"——`AGENT_INBOX_PORT` 被文档写成用户旋钮，却读的是
`os.environ`，而 pydantic-settings 读 `.env` **不注入 `os.environ`**，于是"按文档写进 `.env`"静默
失效。这里用**临时 env 文件**（`Settings(_env_file=…)`）直接验"`.env` 这条路通"——不碰仓库里的
真 `.env`，因此本文件也**不需要 .env**（工具链那两条前提只剩 `python -m pytest`）。
"""
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schema.approval_schema import DEFAULT_INBOX_PORT

# 核心键必须齐（键不可缺、值可空）—— 与真 .env 的形状一致，只是值无关紧要
_CORE = """\
CHAT_MODEL_API_KEY=sk-test
CHAT_MODEL_URL=http://127.0.0.1:1/v1
CHAT_MODEL_NAME=fake-model
EMBEDDING_MODEL_API_KEY=
EMBEDDING_MODEL_URL=http://127.0.0.1:1/v1
EMBEDDING_MODEL_NAME=
TAVILY_API_KEY=
"""


def _settings_from(tmp_path: Path, extra: str) -> Settings:
    env_file = tmp_path / ".env"
    env_file.write_text(_CORE + extra, encoding="utf-8")
    return Settings(_env_file=str(env_file))


def test_optional_keys_have_defaults_and_core_keys_do_not(tmp_path):
    """可选键（技能凭证 / 思考模式 / 收件箱端口）缺失不该让核心起不来；核心键缺一个就抛。"""
    settings = _settings_from(tmp_path, "")
    assert settings.AGENT_INBOX_PORT == DEFAULT_INBOX_PORT == 25010
    assert settings.GITHUB_TOKEN.get_secret_value() == ""
    assert settings.CHAT_THINKING == ""

    with pytest.raises(ValidationError):
        Settings(_env_file=str(tmp_path / "no_such_file"))  # 核心键一个都读不到 → 必须报错


def test_inbox_port_is_readable_from_dotenv(tmp_path):
    """**这条钉的是那条修复**：`.env` 里写 `AGENT_INBOX_PORT` 必须真的生效。

    此前 `ApprovalInboxServer` 直接读 `os.environ`（`.env` 的值不在那里）→ 静默回到默认端口，
    人以为换了端口、worker 却还在往别处发审批。
    """
    assert _settings_from(tmp_path, "AGENT_INBOX_PORT=25011\n").AGENT_INBOX_PORT == 25011


def test_empty_value_for_an_int_key_is_a_loud_error(tmp_path):
    """整数键**不能留空**：`AGENT_INBOX_PORT=` 会 ValidationError（实测）。

    所以 `.env.example` 里这一行是注释掉的（要用才取消注释）——把模板改回"留空"会让程序起不来。
    """
    with pytest.raises(ValidationError):
        _settings_from(tmp_path, "AGENT_INBOX_PORT=\n")
