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

    options = browser_options.build_browser_launch_options(engine_name="chromium", platform="darwin")

    assert options == {
        "headless": True,
        "slow_mo": 0,
        "args": ["--one", "--ignore-certificate-errors", "--use-mock-keychain"],
    }
