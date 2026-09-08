

from langchain.chat_models import init_chat_model
from app.config import get_settings

settings = get_settings()

def get_main_chat_model():
    return init_chat_model(
        model = settings.CHAT_MODEL_NAME,
        base_url = settings.CHAT_MODEL_URL,
        api_key = settings.CHAT_MODEL_API_KEY,
        model_provider="openai"
    )


