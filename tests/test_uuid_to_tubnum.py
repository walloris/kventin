import csv
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from scripts import uuid_to_tubnum as exporter


UUID = "12345678-1234-1234-1234-123456789abc"


class FakeResponse:
    def __init__(
        self, status_code, payload=None, headers=None, text="", url=""
    ):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text
        self.url = url

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class FlowSession(FakeSession):
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class MountSession:
    def __init__(self):
        self.mounts = []

    def mount(self, prefix, adapter):
        self.mounts.append((prefix, adapter))


def endpoint() -> exporter.Endpoint:
    return exporter.Endpoint("https://example.test/api", [], "empId")


def oidc_url(state="state-1") -> str:
    query = urlencode(
        {
            "client_id": "addressbook",
            "redirect_uri": (
                "https://addressbook.sigma.sbrf.ru"
                "/openid-connect-auth/redirect_uri"
            ),
            "response_type": "code",
            "scope": "openid",
            "state": state,
            "nonce": "nonce-1",
        }
    )
    return (
        "https://idp02.auth.sigma.sbrf.ru"
        "/auth/realms/sigma/protocol/openid-connect/auth?"
        f"{query}"
    )


def action_url() -> str:
    return (
        "https://idp02.auth.sigma.sbrf.ru"
        "/auth/realms/sigma/login-actions/authenticate?"
        "session_code=one&execution=two&client_id=addressbook"
        "&tab_id=three&client_data=four"
    )


def alt_action_url() -> str:
    return (
        "https://alt.idp02.auth.sigma.sbrf.ru"
        "/auth/realms/sigma/login-actions/authenticate?"
        "client_id=addressbook&tab_id=three&client_data=four"
    )


def callback_url(state="state-1") -> str:
    return (
        "https://addressbook.sigma.sbrf.ru"
        "/openid-connect-auth/redirect_uri?"
        f"code=temporary&state={state}&session_state=session"
    )


def test_custom_ca_bundle(tmp_path: Path) -> None:
    ca_bundle = tmp_path / "corp.pem"
    ca_bundle.write_text(
        "-----BEGIN CERTIFICATE-----\nplaceholder\n"
        "-----END CERTIFICATE-----\n",
        encoding="ascii",
    )

    verify = exporter.resolve_tls_verify(ca_bundle)
    assert verify == str(ca_bundle.resolve())

    with pytest.raises(exporter.ConfigError):
        exporter.resolve_tls_verify(tmp_path / "missing.pem")


def test_client_certificate_uses_two_env_variables_and_hides_password(
    tmp_path: Path,
) -> None:
    p12_path = tmp_path / "client cert.p12"
    p12_path.write_bytes(b"placeholder")
    certificate = exporter.resolve_client_certificate(
        {
            exporter.CLIENT_CERT_ENV: str(p12_path),
            exporter.CLIENT_CERT_PASSPHRASE_ENV: "top secret",
        }
    )

    assert certificate is not None
    assert certificate.path == p12_path.resolve()
    assert certificate.password == "top secret"
    assert "top secret" not in repr(certificate)

    with pytest.raises(exporter.ConfigError, match="PASSPHRASE"):
        exporter.resolve_client_certificate(
            {exporter.CLIENT_CERT_ENV: str(p12_path)}
        )
    with pytest.raises(exporter.ConfigError, match="CLIENT_CERT"):
        exporter.resolve_client_certificate(
            {exporter.CLIENT_CERT_PASSPHRASE_ENV: "top secret"}
        )


def test_client_certificate_is_mounted_only_on_allowed_hosts(
    tmp_path: Path, monkeypatch
) -> None:
    certificate = exporter.ClientCertificate(
        tmp_path / "client.p12", "top secret"
    )
    adapters = []

    def fake_adapter(received_certificate):
        adapter = object()
        adapters.append((received_certificate, adapter))
        return adapter

    monkeypatch.setattr(exporter, "_pkcs12_adapter", fake_adapter)
    session = MountSession()

    exporter.mount_client_certificate(
        session,
        certificate,
        [
            exporter.DEFAULT_EXPECTED_HOST,
            exporter.DEFAULT_PRIMARY_IDP_HOST,
            exporter.DEFAULT_ALT_IDP_HOST,
            exporter.DEFAULT_EXPECTED_HOST,
        ],
    )

    assert {prefix for prefix, _ in session.mounts} == {
        "https://addressbook.sigma.sbrf.ru/",
        "https://addressbook.sigma.sbrf.ru:443/",
        "https://idp02.auth.sigma.sbrf.ru/",
        "https://idp02.auth.sigma.sbrf.ru:443/",
        "https://alt.idp02.auth.sigma.sbrf.ru/",
        "https://alt.idp02.auth.sigma.sbrf.ru:443/",
    }
    assert all(item[0] is certificate for item in adapters)
    assert len(adapters) == 3
    assert len(session.mounts) == 6

    untrusted_session = MountSession()
    with pytest.raises(exporter.ConfigError, match="allowlist"):
        exporter.mount_client_certificate(
            untrusted_session,
            certificate,
            ["untrusted.example"],
        )
    assert untrusted_session.mounts == []


def test_client_certificate_error_does_not_reveal_password(
    tmp_path: Path, monkeypatch
) -> None:
    certificate = exporter.ClientCertificate(
        tmp_path / "client.p12", "top secret"
    )

    def failing_adapter(**kwargs):
        raise ValueError(f"bad password: {kwargs['pkcs12_password']}")

    monkeypatch.setitem(
        sys.modules,
        "requests_pkcs12",
        SimpleNamespace(Pkcs12Adapter=failing_adapter),
    )

    with pytest.raises(exporter.ConfigError) as error:
        exporter._pkcs12_adapter(certificate)
    assert "top secret" not in str(error.value)


def test_rtf_converter_does_not_inherit_client_certificate_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    rtf_path = tmp_path / "request.rtf"
    rtf_path.write_text(r"{\rtf1 placeholder}", encoding="ascii")
    monkeypatch.setenv(exporter.CLIENT_CERT_ENV, "/private/client.p12")
    monkeypatch.setenv(
        exporter.CLIENT_CERT_PASSPHRASE_ENV, "top secret"
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return SimpleNamespace(
            returncode=0,
            stdout=b"converted request",
            stderr=b"",
        )

    monkeypatch.setattr(exporter.subprocess, "run", fake_run)

    assert exporter.rtf_to_text(rtf_path) == "converted request"
    assert exporter.CLIENT_CERT_ENV not in captured["env"]
    assert exporter.CLIENT_CERT_PASSPHRASE_ENV not in captured["env"]
    assert "textutil" in captured["command"][0]


def test_ssl_reason_is_safely_classified(monkeypatch) -> None:
    class FakeSSLError(Exception):
        pass

    monkeypatch.setattr(exporter, "RequestsSSLError", FakeSSLError)
    error = FakeSSLError(
        "HTTPSConnectionPool UUID TOKEN: [SSL: CERTIFICATE_VERIFY_FAILED]"
    )

    assert (
        exporter.safe_transport_error(error)
        == "SSLError[CERTIFICATE_VERIFY_FAILED]"
    )


def test_reauth_follows_observed_spnego_oidc_flow() -> None:
    login_form = (
        "<html><form method='post' "
        f"action='{action_url().replace('&', '&amp;')}'></form></html>"
    )
    session = FlowSession(
        [
            FakeResponse(302, headers={"Location": oidc_url()}),
            FakeResponse(200, text=login_form),
            FakeResponse(302, headers={"Location": alt_action_url()}),
            FakeResponse(302, headers={"Location": callback_url()}),
            FakeResponse(302, headers={"Location": "/"}),
            FakeResponse(200, text="<html></html>"),
            FakeResponse(200, {"user": {"authenticated": True}}),
        ]
    )
    negotiate_auth = object()

    exporter.drive_addressbook_reauth(
        session,
        negotiate_auth,
        exporter.DEFAULT_EXPECTED_HOST,
        timeout=1,
    )

    assert [call[0] for call in session.calls] == [
        "GET",
        "GET",
        "POST",
        "GET",
        "GET",
        "GET",
        "GET",
    ]
    assert session.calls[1][2]["auth"] is negotiate_auth
    assert session.calls[2][2]["auth"] is negotiate_auth
    assert session.calls[3][2]["auth"] is negotiate_auth
    assert "auth" not in session.calls[4][2]
    assert "auth" not in session.calls[5][2]
    assert session.calls[2][2]["data"] == b""
    assert session.calls[2][2]["headers"] == {
        "Accept": exporter.NAVIGATION_ACCEPT,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    assert all(
        call[2]["headers"]["Accept"] == exporter.NAVIGATION_ACCEPT
        for call in session.calls[:-1]
    )
    assert session.calls[-1][2]["headers"] == {
        "Accept": exporter.JSON_ACCEPT
    }
    assert all(
        call[2]["allow_redirects"] is False for call in session.calls
    )


def test_reauth_rejects_oidc_state_mismatch() -> None:
    session = FlowSession(
        [
            FakeResponse(302, headers={"Location": oidc_url()}),
            FakeResponse(
                302, headers={"Location": callback_url("different-state")}
            ),
        ]
    )

    with pytest.raises(exporter.AuthExpired, match="state"):
        exporter.drive_addressbook_reauth(
            session,
            object(),
            exporter.DEFAULT_EXPECTED_HOST,
            timeout=1,
        )
    assert len(session.calls) == 2


def test_reauth_does_not_convert_307_redirect_to_get() -> None:
    session = FlowSession(
        [
            FakeResponse(302, headers={"Location": oidc_url()}),
            FakeResponse(307, headers={"Location": callback_url()}),
        ]
    )

    with pytest.raises(exporter.AuthExpired, match="HTTP 307"):
        exporter.drive_addressbook_reauth(
            session,
            object(),
            exporter.DEFAULT_EXPECTED_HOST,
            timeout=1,
        )
    assert len(session.calls) == 2


def test_reauth_rejects_redirect_to_untrusted_host() -> None:
    session = FlowSession(
        [
            FakeResponse(
                302,
                headers={
                    "Location": (
                        "https://untrusted.example/"
                        "auth/realms/sigma/protocol/openid-connect/auth"
                    )
                },
            )
        ]
    )

    with pytest.raises(exporter.AuthExpired, match="разрешённого маршрута"):
        exporter.drive_addressbook_reauth(
            session,
            object(),
            exporter.DEFAULT_EXPECTED_HOST,
            timeout=1,
        )
    assert len(session.calls) == 1


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
    with pytest.raises(exporter.ConfigError, match="HTTPS-порт"):
        exporter.endpoint_from_request(
            exporter.CurlRequest(
                "https://addressbook.sigma.sbrf.ru:8443/api",
                "GET",
                {},
                {"A": "one"},
            ),
            exporter.DEFAULT_EXPECTED_HOST,
            "empId",
        )


def test_tubnum_is_associated_with_requested_uuid() -> None:
    assert (
        exporter.find_tubnum(
            {"empId": UUID, "data": {"person": {"tubNum": "00123"}}},
            UUID,
        )
        == "00123"
    )
    assert (
        exporter.find_tubnum(
            {"empId": UUID, "data": {"person": {"tabNum": "00456"}}},
            UUID,
        )
        == "00456"
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


def test_run_keeps_renewed_session_after_missing_tubnum(
    tmp_path: Path, monkeypatch
) -> None:
    second_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    input_path = tmp_path / "addr.csv"
    with input_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "uuid"])
        writer.writerow(["first", UUID])
        writer.writerow(["second", second_uuid])

    request_path = tmp_path / "request.txt"
    request_path.write_text(
        "curl 'https://addressbook.sigma.sbrf.ru/api/home/empInfoFull"
        "?empId=00000000-0000-0000-0000-000000000000' "
        "-H 'Accept: application/json' -b 'SESSION=stale'",
        encoding="utf-8",
    )
    output_path = tmp_path / "result.csv"
    args = exporter.parse_args(
        [
            "--input",
            str(input_path),
            "--request-rtf",
            str(request_path),
            "--output",
            str(output_path),
            "--rate",
            "0",
            "--retries",
            "0",
            "--auto-reauth",
        ]
    )

    expired_session = FakeSession([])
    renewed_session = FakeSession([])
    p12_path = tmp_path / "client.p12"
    p12_path.write_bytes(b"placeholder")
    monkeypatch.setenv(exporter.CLIENT_CERT_ENV, str(p12_path))
    monkeypatch.setenv(
        exporter.CLIENT_CERT_PASSPHRASE_ENV, "top secret"
    )
    create_certificates = []
    fetch_calls = []
    reauth_calls = []

    def fake_create_session(*args, **kwargs):
        create_certificates.append(kwargs["client_certificate"])
        return expired_session

    monkeypatch.setattr(exporter, "create_session", fake_create_session)

    def fake_reauth(*args, **kwargs):
        reauth_calls.append(kwargs["client_certificate"])
        return renewed_session

    def fake_fetch(session, endpoint, user_uuid, **kwargs):
        fetch_calls.append((session, user_uuid))
        if session is expired_session:
            raise exporter.AuthExpired("expired")
        if user_uuid == UUID:
            raise exporter.MissingTubNum("missing")
        return "77"

    monkeypatch.setattr(
        exporter, "reauthenticate_addressbook", fake_reauth
    )
    monkeypatch.setattr(exporter, "fetch_tubnum", fake_fetch)

    assert exporter.run(args) == 1
    assert len(create_certificates) == 1
    assert create_certificates[0].path == p12_path.resolve()
    assert create_certificates[0].password == "top secret"
    assert reauth_calls == create_certificates
    assert fetch_calls == [
        (expired_session, UUID),
        (renewed_session, UUID),
        (renewed_session, second_uuid),
    ]
    assert expired_session.closed
    assert renewed_session.closed

    with output_path.open("r", encoding="utf-8-sig", newline="") as handle:
        assert list(csv.DictReader(handle)) == [
            {"uuid": second_uuid, "tubNum": "77"}
        ]
