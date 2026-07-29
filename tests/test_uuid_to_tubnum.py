import csv
from pathlib import Path

import pytest

from scripts import uuid_to_tubnum as exporter


UUID = "12345678-1234-1234-1234-123456789abc"


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def endpoint() -> exporter.Endpoint:
    return exporter.Endpoint("https://example.test/api", [], "empId")


def test_parse_curl_and_protect_cookie_host() -> None:
    parsed = exporter.parse_curl(
        "curl 'https://addressbook.sigma.sbrf.ru/api/home/empInfoFull"
        "?empId=00000000-0000-0000-0000-000000000000' "
        "-H 'Accept: application/json' -b 'A=one; B=two'"
    )

    assert parsed.method == "GET"
    assert parsed.cookies == {"A": "one", "B": "two"}
    assert exporter.endpoint_from_request(
        parsed, "addressbook.sigma.sbrf.ru", "empId"
    ).static_query == []

    with pytest.raises(exporter.ConfigError):
        exporter.endpoint_from_request(parsed, "wrong.example", "empId")


def test_tubnum_is_associated_with_requested_uuid() -> None:
    assert (
        exporter.find_tubnum(
            {"empId": UUID, "data": {"person": {"tubNum": "00123"}}},
            UUID,
        )
        == "00123"
    )

    with pytest.raises(exporter.MissingTubNum):
        exporter.find_tubnum(
            {
                "empId": UUID,
                "tubNum": None,
                "manager": {
                    "empId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "tubNum": "manager-number",
                },
            },
            UUID,
        )

    with pytest.raises(exporter.AmbiguousTubNum):
        exporter.find_tubnum(
            {"people": [{"tubNum": "1"}, {"tubNum": "2"}]},
            UUID,
        )


def test_fetch_retries_429_and_returns_tubnum(monkeypatch) -> None:
    monkeypatch.setattr(exporter.time, "sleep", lambda _: None)
    session = FakeSession(
        [
            FakeResponse(429, headers={"Retry-After": "0"}),
            FakeResponse(200, {"data": {"tubNum": "77"}}),
        ]
    )

    assert (
        exporter.fetch_tubnum(
            session,
            endpoint(),
            UUID,
            timeout=1,
            retries=1,
            max_backoff=1,
            rate_limiter=exporter.RateLimiter(0),
        )
        == "77"
    )
    assert len(session.calls) == 2


@pytest.mark.parametrize("status_code", [401, 403, 302])
def test_fetch_stops_when_authentication_is_lost(status_code) -> None:
    session = FakeSession([FakeResponse(status_code)])

    with pytest.raises(exporter.AuthExpired):
        exporter.fetch_tubnum(
            session,
            endpoint(),
            UUID,
            timeout=1,
            retries=0,
            max_backoff=1,
            rate_limiter=exporter.RateLimiter(0),
        )


@pytest.mark.parametrize("status_code", [204, 404])
def test_fetch_does_not_accept_missing_record(status_code) -> None:
    session = FakeSession([FakeResponse(status_code)])

    with pytest.raises(exporter.MissingTubNum):
        exporter.fetch_tubnum(
            session,
            endpoint(),
            UUID,
            timeout=1,
            retries=0,
            max_backoff=1,
            rate_limiter=exporter.RateLimiter(0),
        )


def test_resume_rejects_incomplete_main_csv(tmp_path: Path) -> None:
    output = tmp_path / "output.csv"
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["uuid", "tubNum"])
        writer.writerow([UUID, "00123"])

    assert exporter.load_processed(output) == {UUID}

    with output.open("a", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow(
            ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", ""]
        )

    with pytest.raises(exporter.ConfigError):
        exporter.load_processed(output)
