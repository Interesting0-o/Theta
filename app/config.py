from functools import lru_cache
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
        env_file=".env", 
        env_file_encoding="utf-8",
        extra="ignore"
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()#type:ignore