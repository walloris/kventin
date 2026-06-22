from src.local_openai_client import LocalOpenAIClient


def test_local_client_accepts_base_url(monkeypatch) -> None:
    monkeypatch.setattr("config.LOCAL_LLM_API_URL", "http://127.0.0.1:3333/v1")
    monkeypatch.setattr("config.LOCAL_LLM_API_KEY", "local")
    monkeypatch.setattr("config.LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr("config.LLM_REQUEST_TIMEOUT_SEC", 60)

    client = LocalOpenAIClient()

    assert client.base_url == "http://127.0.0.1:3333/v1"
    assert client.chat_url == "http://127.0.0.1:3333/v1/chat/completions"
    assert client.models_url == "http://127.0.0.1:3333/v1/models"


def test_local_client_accepts_full_chat_completions_url(monkeypatch) -> None:
    monkeypatch.setattr("config.LOCAL_LLM_API_URL", "http://127.0.0.1:3333/v1/chat/completions")
    monkeypatch.setattr("config.LOCAL_LLM_API_KEY", "local")
    monkeypatch.setattr("config.LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr("config.LLM_REQUEST_TIMEOUT_SEC", 60)

    client = LocalOpenAIClient()

    assert client.base_url == "http://127.0.0.1:3333/v1"
    assert client.chat_url == "http://127.0.0.1:3333/v1/chat/completions"
    assert client.models_url == "http://127.0.0.1:3333/v1/models"
