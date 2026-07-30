import csv
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import uuid_to_tubnum as core_exporter
from scripts import uuid_to_tubnum_browser as browser_exporter


UUID = "12345678-1234-1234-1234-123456789abc"
API_URL = (
    "https://addressbook.sigma.sbrf.ru/api/home/empInfoFull"
    f"?empId={UUID}"
)


class FakePage:
    def __init__(
        self,
        result=None,
        error=None,
        results=None,
        closed=False,
    ):
        self.result = result
        self.results = list(results or [])
        self.error = error
        self.closed = closed
        self.evaluate_calls = []

    def evaluate(self, expression, argument):
        self.evaluate_calls.append((expression, argument))
        if self.error is not None:
            raise self.error
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return self.result

    def is_closed(self):
        return self.closed


class FakeRoute:
    def __init__(self):
        self.continued = 0
        self.aborted = []

    def continue_(self):
        self.continued += 1

    def abort(self, reason):
        self.aborted.append(reason)


class FakeBrowserContext:
    def __init__(self):
        self.route_calls = []

    def route(self, pattern, handler):
        self.route_calls.append((pattern, handler))

    def new_context(self, **_kwargs):
        raise AssertionError("incognito context must not be created")


class FakeChromium:
    def __init__(self, context, executable_path="/missing/chromium"):
        self.context = context
        self.executable_path = executable_path
        self.persistent_calls = []

    def launch_persistent_context(self, **kwargs):
        self.persistent_calls.append(kwargs)
        return self.context

    def launch(self, **_kwargs):
        raise AssertionError("standalone browser must not be launched")


class NoWaitRateLimiter:
    def __init__(self):
        self.calls = 0

    def wait(self):
        self.calls += 1


class FakeTransport:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.fetch_calls = []
        self.ensure_authenticated_calls = 0

    def fetch_tubnum(self, user_uuid, **kwargs):
        self.fetch_calls.append((user_uuid, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def ensure_authenticated(self):
        self.ensure_authenticated_calls += 1


def _parse_args(tmp_path: Path, *extra: str):
    return browser_exporter.parse_args(
        [
            "--input",
            str(tmp_path / "addr.csv"),
            "--output",
            str(tmp_path / "uuid_tubnum.csv"),
            *extra,
        ]
    )


def test_parse_args_has_conservative_browser_export_defaults(
    tmp_path: Path,
) -> None:
    args = _parse_args(tmp_path)

    assert args.id_column == 2
    assert args.rate == 1
    assert args.timeout == 30
    assert args.retries == 4
    assert args.max_backoff == 60
    assert args.max_consecutive_errors == 10
    assert args.limit is None
    assert args.progress_every == 100
    assert args.flush_every == 25
    assert args.login_timeout == 300
    assert args.browser_channel == "chromium"
    assert args.browser_executable is None
    assert args.ignore_https_errors is False
    assert args.dry_run is False


def test_client_certificates_use_exact_three_origins_and_pfx_password(
    tmp_path: Path,
) -> None:
    p12_path = tmp_path / "client cert.p12"
    certificate = core_exporter.ClientCertificate(p12_path, "top secret")

    entries = browser_exporter.build_client_certificates(certificate)

    assert entries == [
        {
            "origin": "https://addressbook.sigma.sbrf.ru",
            "pfxPath": str(p12_path),
            "passphrase": "top secret",
        },
        {
            "origin": "https://idp02.auth.sigma.sbrf.ru",
            "pfxPath": str(p12_path),
            "passphrase": "top secret",
        },
        {
            "origin": "https://alt.idp02.auth.sigma.sbrf.ru",
            "pfxPath": str(p12_path),
            "passphrase": "top secret",
        },
    ]


def test_client_certificate_environment_is_scrubbed_and_restored(
    monkeypatch,
) -> None:
    monkeypatch.setenv(core_exporter.CLIENT_CERT_ENV, "/private/client.p12")
    monkeypatch.setenv(
        core_exporter.CLIENT_CERT_PASSPHRASE_ENV, "top secret"
    )
    monkeypatch.setenv("UNRELATED_ENV", "kept")

    with browser_exporter.without_client_cert_environment():
        assert core_exporter.CLIENT_CERT_ENV not in os.environ
        assert core_exporter.CLIENT_CERT_PASSPHRASE_ENV not in os.environ
        assert os.environ["UNRELATED_ENV"] == "kept"
        os.environ[core_exporter.CLIENT_CERT_ENV] = "temporary"
        os.environ[core_exporter.CLIENT_CERT_PASSPHRASE_ENV] = "temporary"

    assert os.environ[core_exporter.CLIENT_CERT_ENV] == "/private/client.p12"
    assert (
        os.environ[core_exporter.CLIENT_CERT_PASSPHRASE_ENV] == "top secret"
    )
    assert os.environ["UNRELATED_ENV"] == "kept"


def test_client_certificate_environment_keeps_initially_missing_values_missing(
    monkeypatch,
) -> None:
    monkeypatch.delenv(core_exporter.CLIENT_CERT_ENV, raising=False)
    monkeypatch.delenv(
        core_exporter.CLIENT_CERT_PASSPHRASE_ENV, raising=False
    )

    with browser_exporter.without_client_cert_environment():
        os.environ[core_exporter.CLIENT_CERT_ENV] = "temporary"
        os.environ[core_exporter.CLIENT_CERT_PASSPHRASE_ENV] = "temporary"

    assert core_exporter.CLIENT_CERT_ENV not in os.environ
    assert core_exporter.CLIENT_CERT_PASSPHRASE_ENV not in os.environ


def test_launch_defaults_to_visible_bundled_chromium_and_safe_spnego_allowlist(
    tmp_path: Path,
) -> None:
    options = browser_exporter.build_launch_options(_parse_args(tmp_path))

    assert options["headless"] is False
    assert "channel" not in options
    assert "executable_path" not in options
    assert (
        "--auth-server-allowlist="
        "idp02.auth.sigma.sbrf.ru,alt.idp02.auth.sigma.sbrf.ru"
    ) in options["args"]
    assert "--ignore-certificate-errors" not in options["args"]
    assert not any("delegate" in item.casefold() for item in options["args"])


def test_browser_executable_override_takes_precedence_over_channel(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Google Chrome"
    executable.write_bytes(b"placeholder")
    args = _parse_args(
        tmp_path, "--browser-executable", str(executable)
    )

    options = browser_exporter.build_launch_options(args)

    assert options["headless"] is False
    assert options["executable_path"] == str(executable.resolve())
    assert "channel" not in options


def test_missing_bundled_chromium_is_installed_automatically(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executable = tmp_path / "playwright-chromium"
    chromium = FakeChromium(
        FakeBrowserContext(),
        executable_path=str(executable),
    )
    playwright = SimpleNamespace(chromium=chromium)
    args = _parse_args(tmp_path)
    captured = {}
    monkeypatch.setenv(
        core_exporter.CLIENT_CERT_ENV, "/private/client.p12"
    )
    monkeypatch.setenv(
        core_exporter.CLIENT_CERT_PASSPHRASE_ENV, "top secret"
    )

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        executable.write_bytes(b"browser")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(browser_exporter.subprocess, "run", fake_run)

    browser_exporter.ensure_bundled_chromium(playwright, args)

    assert captured["command"] == [
        browser_exporter.sys.executable,
        "-m",
        "playwright",
        "install",
        "chromium",
    ]
    assert core_exporter.CLIENT_CERT_ENV not in captured["env"]
    assert core_exporter.CLIENT_CERT_PASSPHRASE_ENV not in captured["env"]


def test_explicit_browser_channel_does_not_auto_install(
    tmp_path: Path,
    monkeypatch,
) -> None:
    args = _parse_args(tmp_path, "--browser-channel", "chrome")
    playwright = SimpleNamespace(
        chromium=FakeChromium(FakeBrowserContext())
    )

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("installer must not run for an explicit channel")

    monkeypatch.setattr(browser_exporter.subprocess, "run", unexpected_run)

    browser_exporter.ensure_bundled_chromium(playwright, args)


def test_launch_uses_ephemeral_persistent_context_not_incognito(
    tmp_path: Path,
) -> None:
    args = _parse_args(tmp_path)
    client_certificates = [{"origin": "https://example.test"}]
    context = FakeBrowserContext()
    chromium = FakeChromium(context)
    playwright = SimpleNamespace(chromium=chromium)
    profile = tmp_path / "ephemeral-profile"

    result = browser_exporter.launch_persistent_browser_context(
        playwright,
        args,
        client_certificates,
        profile,
    )

    assert result is context
    assert len(chromium.persistent_calls) == 1
    options = chromium.persistent_calls[0]
    assert options["user_data_dir"] == str(profile)
    assert options["client_certificates"] is client_certificates
    assert options["ignore_https_errors"] is False
    assert options["service_workers"] == "allow"
    assert options["headless"] is False
    assert "channel" not in options
    assert context.route_calls == []


@pytest.mark.parametrize(
    "url",
    [
        "https://addressbook.sigma.sbrf.ru/",
        "https://addressbook.sigma.sbrf.ru:443/api/home/empInfoFull",
        "https://idp02.auth.sigma.sbrf.ru/auth/realms/sigma",
        "https://alt.idp02.auth.sigma.sbrf.ru/",
    ],
)
def test_tls_bypass_allowlist_accepts_only_exact_https_origins(
    url: str,
) -> None:
    assert browser_exporter._allowed_browser_request(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://addressbook.sigma.sbrf.ru/",
        "https://addressbook.sigma.sbrf.ru:8443/",
        "https://addressbook.sigma.sbrf.ru.evil.example/",
        "https://addressbook.sigma.sbrf.ru@evil.example/",
        "https://evil.example/",
        "data:text/plain,secret",
        "wss://addressbook.sigma.sbrf.ru/socket",
    ],
)
def test_tls_bypass_allowlist_rejects_every_other_origin(url: str) -> None:
    assert browser_exporter._allowed_browser_request(url) is False


def test_tls_bypass_installs_guard_and_blocks_outside_allowlist(
    tmp_path: Path,
) -> None:
    args = _parse_args(tmp_path, "--ignore-https-errors")
    context = FakeBrowserContext()
    chromium = FakeChromium(context)
    playwright = SimpleNamespace(chromium=chromium)

    browser_exporter.launch_persistent_browser_context(
        playwright,
        args,
        [],
        tmp_path / "ephemeral-profile",
    )

    assert chromium.persistent_calls[0]["ignore_https_errors"] is True
    assert chromium.persistent_calls[0]["service_workers"] == "block"
    assert len(context.route_calls) == 1
    pattern, handler = context.route_calls[0]
    assert pattern == "**/*"

    for origin in browser_exporter.CLIENT_CERT_ORIGINS:
        route = FakeRoute()
        handler(route, SimpleNamespace(url=f"{origin}/allowed"))
        assert route.continued == 1
        assert route.aborted == []

    blocked_route = FakeRoute()
    handler(
        blocked_route,
        SimpleNamespace(url="https://evil.example/secret"),
    )
    assert blocked_route.continued == 0
    assert blocked_route.aborted == ["blockedbyclient"]


def test_evaluate_fetch_uses_fixed_api_origin_path_and_safe_fetch_options() -> None:
    expected_result = {
        "kind": "ok",
        "payload": {"uuid": UUID, "tubNum": "12345"},
    }
    page = FakePage(result=expected_result)

    result = browser_exporter._evaluate_fetch(
        page,
        API_URL,
        core_exporter.DEFAULT_EXPECTED_HOST,
        browser_exporter.ADDRESSBOOK_API_PATH,
        12.5,
    )

    assert result == expected_result
    assert len(page.evaluate_calls) == 1
    expression, argument = page.evaluate_calls[0]
    assert argument["url"] == API_URL
    assert argument["timeoutMs"] == 12500
    assert "same-origin" in expression
    assert "manual" in expression
    assert "no-store" in expression


@pytest.mark.parametrize(
    "url",
    [
        f"https://evil.example/api/home/empInfoFull?empId={UUID}",
        (
            "https://addressbook.sigma.sbrf.ru/api/home/getUserData"
            f"?empId={UUID}"
        ),
        (
            "https://addressbook.sigma.sbrf.ru:8443/api/home/empInfoFull"
            f"?empId={UUID}"
        ),
        (
            "http://addressbook.sigma.sbrf.ru/api/home/empInfoFull"
            f"?empId={UUID}"
        ),
    ],
)
def test_evaluate_fetch_rejects_non_api_origin_or_path_without_evaluating(
    url: str,
) -> None:
    page = FakePage(result={"kind": "ok"})

    with pytest.raises(core_exporter.ConfigError):
        browser_exporter._evaluate_fetch(
            page,
            url,
            core_exporter.DEFAULT_EXPECTED_HOST,
            browser_exporter.ADDRESSBOOK_API_PATH,
            10,
        )

    assert page.evaluate_calls == []


def test_evaluate_fetch_sanitizes_browser_exception() -> None:
    page = FakePage(
        error=browser_exporter.PlaywrightError(
            "cookie=secret; internal host detail"
        )
    )

    result = browser_exporter._evaluate_fetch(
        page,
        API_URL,
        core_exporter.DEFAULT_EXPECTED_HOST,
        browser_exporter.ADDRESSBOOK_API_PATH,
        10,
    )

    assert result == {"kind": "network_error"}
    assert "secret" not in repr(result)
    assert "internal host detail" not in repr(result)


def test_closed_page_raises_auth_expired_without_retry_or_raw_error() -> None:
    page = FakePage(
        error=browser_exporter.PlaywrightError(
            "cookie=secret; target page closed with internal details"
        ),
        closed=True,
    )
    transport = browser_exporter.BrowserTransport(page)
    limiter = NoWaitRateLimiter()

    with pytest.raises(core_exporter.AuthExpired) as error:
        transport.fetch_tubnum(
            UUID,
            retries=4,
            max_backoff=1,
            rate_limiter=limiter,
        )

    assert limiter.calls == 1
    assert len(page.evaluate_calls) == 1
    assert "браузера" in str(error.value)
    assert "secret" not in str(error.value)
    assert "internal details" not in str(error.value)


def test_transport_accepts_only_fixed_addressbook_host() -> None:
    with pytest.raises(core_exporter.ConfigError, match="allowlist"):
        browser_exporter.BrowserTransport(
            FakePage(), expected_host="evil.example"
        )


def test_transport_classifies_success_and_returns_matching_tubnum() -> None:
    page = FakePage(
        result={
            "kind": "ok",
            "payload": {"uuid": UUID, "tubNum": "12345"},
        }
    )
    transport = browser_exporter.BrowserTransport(
        page, timeout=7, login_timeout=9
    )
    limiter = NoWaitRateLimiter()

    assert (
        transport.fetch_tubnum(
            UUID,
            retries=0,
            max_backoff=3,
            rate_limiter=limiter,
        )
        == "12345"
    )
    assert limiter.calls == 1
    assert page.evaluate_calls[0][1]["url"] == API_URL
    assert page.evaluate_calls[0][1]["timeoutMs"] == 7000


def test_transport_accepts_real_addressbook_tabnum_field() -> None:
    page = FakePage(
        result={
            "kind": "ok",
            "payload": {"empId": UUID, "tabNum": "00456"},
        }
    )
    transport = browser_exporter.BrowserTransport(page)

    assert (
        transport.fetch_tubnum(
            UUID,
            retries=0,
            max_backoff=1,
            rate_limiter=NoWaitRateLimiter(),
        )
        == "00456"
    )


def test_safe_json_shape_contains_keys_and_types_but_not_values() -> None:
    payload = {
        "data": [
            {
                "employeeUuid": UUID,
                "displayName": "Secret Person",
                "dynamic-key": "private value",
            }
        ],
        "message": "secret token",
    }

    shape = browser_exporter.safe_json_shape(payload)

    assert "$.data:array(len=1)" in shape
    assert "$.data[].employeeUuid:string" in shape
    assert "$.data[].displayName:string" in shape
    assert "$.data[].<redacted-key>:string" in shape
    assert "$.message:string" in shape
    assert UUID not in shape
    assert "Secret Person" not in shape
    assert "private value" not in shape
    assert "secret token" not in shape


def test_safe_key_diagnostics_include_names_but_not_values() -> None:
    payload = {
        "employeeNumber": "private personnel value",
        "phones": [{"number": "private phone value"}],
        UUID: {"tabNumber": "private dynamic value"},
        "displayName": "Secret Person",
    }

    top_level = browser_exporter.safe_top_level_keys(payload)
    candidates = browser_exporter.safe_number_field_candidates(payload)

    assert "<redacted-key>" in top_level
    assert "displayName" in top_level
    assert "employeeNumber" in top_level
    assert "$.employeeNumber" in candidates
    assert "$.phones[].number" in candidates
    assert "$.<redacted-key>.tabNumber" in candidates
    combined = top_level + candidates
    assert UUID not in combined
    assert "private personnel value" not in combined
    assert "private phone value" not in combined
    assert "private dynamic value" not in combined
    assert "Secret Person" not in combined


def test_missing_tubnum_reports_only_safe_json_shape() -> None:
    page = FakePage(
        result={
            "kind": "ok",
            "payload": {
                "data": [],
                "message": "private backend message",
            },
        }
    )
    transport = browser_exporter.BrowserTransport(page)

    with pytest.raises(core_exporter.MissingTubNum) as error:
        transport.fetch_tubnum(
            UUID,
            retries=0,
            max_backoff=1,
            rate_limiter=NoWaitRateLimiter(),
        )

    message = str(error.value)
    assert "JSON shape:" in message
    assert "$.data:array(len=0)" in message
    assert "$.message:string" in message
    assert "top-level keys: data, message" in message
    assert "number-like key paths: <none>" in message
    assert "private backend message" not in message


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"kind": "auth", "status": 401}, "HTTP 401"),
        ({"kind": "auth", "status": 403}, "HTTP 403"),
        ({"kind": "auth"}, "redirect"),
    ],
)
def test_transport_classifies_auth_as_expired(result, message) -> None:
    transport = browser_exporter.BrowserTransport(FakePage(result=result))

    with pytest.raises(core_exporter.AuthExpired, match=message):
        transport.fetch_tubnum(
            UUID,
            retries=0,
            max_backoff=1,
            rate_limiter=NoWaitRateLimiter(),
        )


@pytest.mark.parametrize("status", [204, 404])
def test_transport_classifies_missing_record(status: int) -> None:
    transport = browser_exporter.BrowserTransport(
        FakePage(result={"kind": "missing", "status": status})
    )

    with pytest.raises(core_exporter.MissingTubNum, match=f"HTTP {status}"):
        transport.fetch_tubnum(
            UUID,
            retries=0,
            max_backoff=1,
            rate_limiter=NoWaitRateLimiter(),
        )


def test_transport_retries_retryable_status_then_succeeds(
    monkeypatch,
) -> None:
    page = FakePage(
        results=[
            {
                "kind": "retry",
                "status": 429,
                "retryAfter": 99,
            },
            {
                "kind": "ok",
                "payload": {"uuid": UUID, "tubNum": "12345"},
            },
        ]
    )
    transport = browser_exporter.BrowserTransport(page)
    limiter = NoWaitRateLimiter()
    sleeps = []
    monkeypatch.setattr(browser_exporter.time, "sleep", sleeps.append)

    assert (
        transport.fetch_tubnum(
            UUID,
            retries=1,
            max_backoff=3,
            rate_limiter=limiter,
        )
        == "12345"
    )
    assert limiter.calls == 2
    assert sleeps == [3]


@pytest.mark.parametrize("kind", ["timeout", "network_error"])
def test_transport_network_failure_does_not_expose_raw_browser_text(
    kind: str,
) -> None:
    page = FakePage(
        result={
            "kind": kind,
            "error": "cookie=secret; raw browser exception",
        }
    )
    transport = browser_exporter.BrowserTransport(page)

    with pytest.raises(core_exporter.FetchError) as error:
        transport.fetch_tubnum(
            UUID,
            retries=0,
            max_backoff=1,
            rate_limiter=NoWaitRateLimiter(),
        )

    assert kind in str(error.value)
    assert "secret" not in str(error.value)
    assert "raw browser exception" not in str(error.value)


def _write_input(path: Path) -> None:
    path.write_text(
        f"name,uuid\nExample,{UUID}\n",
        encoding="utf-8",
    )


def _read_result(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_run_export_reauthenticates_once_and_retries_same_uuid(
    tmp_path: Path,
) -> None:
    args = _parse_args(tmp_path)
    _write_input(args.input)
    transport = FakeTransport(
        [core_exporter.AuthExpired("expired"), "12345"]
    )

    assert browser_exporter.run_export(args, transport) == 0

    assert transport.ensure_authenticated_calls == 1
    assert [item[0] for item in transport.fetch_calls] == [UUID, UUID]
    assert _read_result(args.output) == [
        {"uuid": UUID, "tubNum": "12345"}
    ]


def test_run_export_does_not_reauthenticate_twice_for_same_uuid(
    tmp_path: Path,
) -> None:
    args = _parse_args(tmp_path)
    _write_input(args.input)
    transport = FakeTransport(
        [
            core_exporter.AuthExpired("first"),
            core_exporter.AuthExpired("second"),
        ]
    )

    assert browser_exporter.run_export(args, transport) == 3

    assert transport.ensure_authenticated_calls == 1
    assert len(transport.fetch_calls) == 2
    assert _read_result(args.output) == []
