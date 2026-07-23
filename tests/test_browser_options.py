import agent.browser.browser_options as browser_options


def test_build_start_url_candidates_deduplicates_and_keeps_order() -> None:
    assert browser_options.build_start_url_candidates(
        "https://example.test/app/",
        ["https://example.test/app", "https://example.test/login"],
    ) == [
        "https://example.test/app/",
        "https://example.test/app",
        "https://example.test/login",
    ]


def test_is_too_many_redirects_error_matches_browser_errors() -> None:
    assert browser_options.is_too_many_redirects_error(Exception("net::ERR_TOO_MANY_REDIRECTS")) is True
    assert browser_options.is_too_many_redirects_error(Exception("timeout")) is False


def test_build_browser_launch_options_adds_chromium_cert_flags(monkeypatch) -> None:
    monkeypatch.setattr(browser_options, "BROWSER_CHROMIUM_ARGS", ["--one"])
    monkeypatch.setattr(browser_options, "BROWSER_SUPPRESS_CERT_PROMPT", True)
    monkeypatch.setattr(browser_options, "HEADLESS", True)
    monkeypatch.setattr(browser_options, "BROWSER_SLOW_MO", 0)
    monkeypatch.setattr(browser_options, "BROWSER_CHANNEL", "")
    monkeypatch.setattr(browser_options, "BROWSER_EXECUTABLE_PATH", "")

    options = browser_options.build_browser_launch_options(engine_name="chromium", platform="darwin")

    assert options == {
        "headless": True,
        "slow_mo": 0,
        "args": ["--one", "--ignore-certificate-errors", "--use-mock-keychain"],
    }


def test_build_browser_launch_options_uses_configured_channel(monkeypatch) -> None:
    monkeypatch.setattr(browser_options, "BROWSER_CHROMIUM_ARGS", [])
    monkeypatch.setattr(browser_options, "BROWSER_SUPPRESS_CERT_PROMPT", False)
    monkeypatch.setattr(browser_options, "BROWSER_CHANNEL", "chrome")
    monkeypatch.setattr(browser_options, "BROWSER_EXECUTABLE_PATH", "")

    options = browser_options.build_browser_launch_options(engine_name="chromium")

    assert options["channel"] == "chrome"


def test_browser_executable_path_takes_precedence_over_channel(monkeypatch) -> None:
    monkeypatch.setattr(browser_options, "BROWSER_CHROMIUM_ARGS", [])
    monkeypatch.setattr(browser_options, "BROWSER_SUPPRESS_CERT_PROMPT", False)
    monkeypatch.setattr(browser_options, "BROWSER_CHANNEL", "chrome")
    monkeypatch.setattr(browser_options, "BROWSER_EXECUTABLE_PATH", "/opt/browser")

    options = browser_options.build_browser_launch_options(engine_name="chromium")

    assert options["executable_path"] == "/opt/browser"
    assert "channel" not in options
