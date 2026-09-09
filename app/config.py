from functools import lru_cache
from pathlib import Path
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env 固定在 theta 项目根（本文件上两级的目录），不随进程 cwd 漂移——
# 使 TUI 可在任意"目标工作区目录"启动：工作区 = 启动目录(cwd)，而代码与 .env 仍定位到项目根。
_ENV_FILE = str(Path(__file__).resolve().parent.parent / ".env")


class Settings(BaseSettings):

    #聊天模型配置
    CHAT_MODEL_API_KEY:SecretStr
    CHAT_MODEL_URL:str 
    CHAT_MODEL_NAME:str

    #词嵌入模型配置
    EMBEDDING_MODEL_API_KEY:SecretStr
    EMBEDDING_MODEL_URL:str
    EMBEDDING_MODEL_NAME:str

    #Tavily搜索api
    TAVILY_API_KEY: SecretStr

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore"
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()#type:ignore