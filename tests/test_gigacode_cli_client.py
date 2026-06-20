from src.gigacode_cli_client import GigacodeCliClient


def _client() -> GigacodeCliClient:
    client = GigacodeCliClient.__new__(GigacodeCliClient)
    client.json_result_key = "result"
    return client


def test_parse_output_prefers_configured_json_key() -> None:
    assert _client()._parse_output('{"result": " ok "}') == "ok"


def test_parse_output_supports_structured_output_object() -> None:
    assert _client()._parse_output('{"structured_output": {"action": "click"}}') == '{"action": "click"}'


def test_parse_output_returns_raw_text_when_not_json() -> None:
    assert _client()._parse_output("plain answer") == "plain answer"
