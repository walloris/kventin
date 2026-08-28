from agent.llm.local_openai_client import LocalOpenAIClient, _looks_like_tls_proxy_error


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


def test_local_client_detects_tls_proxy_errors() -> None:
    body = "TypeError: fetch failed SELF_SIGNED_CERT_IN_CHAIN self-signed certificate in certificate chain"

    assert _looks_like_tls_proxy_error(body) is True
    assert _looks_like_tls_proxy_error("ordinary model error") is False


def test_local_client_retries_http_429(monkeypatch) -> None:
    monkeypatch.setattr("config.LOCAL_LLM_API_URL", "http://127.0.0.1:3333/v1")
    monkeypatch.setattr("config.LOCAL_LLM_API_KEY", "local")
    monkeypatch.setattr("config.LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr("config.LLM_REQUEST_TIMEOUT_SEC", 60)
    monkeypatch.setattr("config.LLM_RETRY_COUNT", 2)
    monkeypatch.setattr("config.LLM_RETRY_BASE_DELAY", 0.1)
    monkeypatch.setattr("agent.llm.local_openai_client.time.sleep", lambda _: None)
    monkeypatch.setattr("agent.llm.local_openai_client.random.uniform", lambda *_: 0)

    class Response:
        def __init__(self, status_code, text="", data=None, headers=None):
            self.status_code = status_code
            self.text = text
            self._data = data or {}
            self.headers = headers or {}

        def json(self):
            return self._data

    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            return Response(429, "rate limit", headers={"Retry-After": "0.1"})
        return Response(200, data={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("agent.llm.local_openai_client.requests.post", fake_post)

    client = LocalOpenAIClient()

    assert client.query("ping") == "ok"
    assert len(calls) == 2


def test_local_client_retries_http_500(monkeypatch) -> None:
    monkeypatch.setattr("config.LOCAL_LLM_API_URL", "http://127.0.0.1:3333/v1")
    monkeypatch.setattr("config.LOCAL_LLM_API_KEY", "local")
    monkeypatch.setattr("config.LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr("config.LLM_REQUEST_TIMEOUT_SEC", 60)
    monkeypatch.setattr("config.LLM_RETRY_COUNT", 2)
    monkeypatch.setattr("config.LLM_RETRY_BASE_DELAY", 0.1)
    monkeypatch.setattr("agent.llm.local_openai_client.time.sleep", lambda _: None)
    monkeypatch.setattr("agent.llm.local_openai_client.random.uniform", lambda *_: 0)

    class Response:
        status_code = 500
        text = "temporary upstream error"
        headers = {}

        def json(self):
            return {}

    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    monkeypatch.setattr("agent.llm.local_openai_client.requests.post", fake_post)

    client = LocalOpenAIClient()

    assert client.query("ping") == ""
    assert len(calls) == 2


def test_local_client_caps_retry_after(monkeypatch) -> None:
    monkeypatch.setattr("config.LOCAL_LLM_API_URL", "http://127.0.0.1:3333/v1")
    monkeypatch.setattr("config.LOCAL_LLM_API_KEY", "local")
    monkeypatch.setattr("config.LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr("config.LLM_REQUEST_TIMEOUT_SEC", 60)
    monkeypatch.setattr("config.LLM_RETRY_COUNT", 2)
    monkeypatch.setattr("config.LLM_RETRY_BASE_DELAY", 0.1)
    monkeypatch.setattr("config.LLM_RETRY_MAX_DELAY", 5.0)
    sleeps = []
    monkeypatch.setattr("agent.llm.local_openai_client.time.sleep", sleeps.append)

    class Response:
        status_code = 429
        text = "rate limit"
        headers = {"Retry-After": "3600"}

    monkeypatch.setattr("agent.llm.local_openai_client.requests.post", lambda *_args, **_kwargs: Response())

    assert LocalOpenAIClient().query("ping") == ""
    assert sleeps == [5.0]


def test_local_client_throttles_failed_model_discovery(monkeypatch) -> None:
    monkeypatch.setattr("config.LOCAL_LLM_API_URL", "http://127.0.0.1:3333/v1")
    monkeypatch.setattr("config.LOCAL_LLM_API_KEY", "local")
    monkeypatch.setattr("config.LOCAL_LLM_MODEL", "")
    monkeypatch.setattr("config.LLM_REQUEST_TIMEOUT_SEC", 60)
    client = LocalOpenAIClient()
    calls = []
    monkeypatch.setattr(
        client,
        "_request_with_retry",
        lambda *_args, **_kwargs: calls.append(True) or None,
    )

    assert client._model() == "local-model"
    assert client._model() == "local-model"
    assert len(calls) == 1


def test_local_client_healthcheck_is_single_and_bounded(monkeypatch) -> None:
    monkeypatch.setattr("config.LOCAL_LLM_API_URL", "http://127.0.0.1:3333/v1")
    monkeypatch.setattr("config.LOCAL_LLM_API_KEY", "local")
    monkeypatch.setattr("config.LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr("config.LLM_REQUEST_TIMEOUT_SEC", 60)
    calls = []

    class Response:
        status_code = 429

    def fake_get(*_args, **kwargs):
        calls.append(kwargs)
        return Response()

    monkeypatch.setattr("agent.llm.local_openai_client.requests.get", fake_get)
    client = LocalOpenAIClient()

    assert client.healthcheck(timeout=30) is False
    assert len(calls) == 1
    assert calls[0]["timeout"] == 3.0
