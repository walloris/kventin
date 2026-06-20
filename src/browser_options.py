"""Small browser/session helpers used by the main agent loop."""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

from config import (
    BROWSER_AUTO_SELECT_CERT_PATTERNS,
    BROWSER_CHROMIUM_ARGS,
    BROWSER_CLIENT_CERT_CERT_PATH,
    BROWSER_CLIENT_CERT_KEY_PATH,
    BROWSER_CLIENT_CERT_ORIGIN,
    BROWSER_CLIENT_CERT_ORIGINS,
    BROWSER_CLIENT_CERT_PASSPHRASE,
    BROWSER_CLIENT_CERT_PFX_PATH,
    BROWSER_SLOW_MO,
    BROWSER_SUPPRESS_CERT_PROMPT,
    BROWSER_USER_DATA_DIR,
    HEADLESS,
    START_URL_FALLBACKS,
)


def is_too_many_redirects_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "err_too_many_redirects" in text or "too many redirects" in text


def build_start_url_candidates(primary: str, fallbacks: Optional[List[str]] = None) -> List[str]:
    """
    Return page.goto candidates for redirect-loop fallback.

    Keep the sequence conservative: original URL, the same URL without trailing slash,
    then explicit configured fallbacks. This avoids silently jumping to a different
    corporate portal area.
    """
    out: List[str] = []
    seen = set()

    def add(url: str) -> None:
        url = (url or "").strip()
        if not url or url in seen:
            return
        seen.add(url)
        out.append(url)

    add(primary)
    stripped = (primary or "").rstrip("/")
    if stripped and stripped != primary:
        add(stripped)
    for fallback in fallbacks if fallbacks is not None else START_URL_FALLBACKS:
        add(fallback)
    return out


def build_browser_launch_options(
    *,
    engine_name: str,
    user_data_dir: str = BROWSER_USER_DATA_DIR,
    platform: str = sys.platform,
) -> Dict[str, Any]:
    """Build Playwright launch options from config without touching the filesystem."""
    use_chromium = engine_name == "chromium" or bool(user_data_dir)
    chromium_args = list(BROWSER_CHROMIUM_ARGS)
    if use_chromium and BROWSER_SUPPRESS_CERT_PROMPT:
        chromium_args.append("--ignore-certificate-errors")
        if platform == "darwin":
            chromium_args.append("--use-mock-keychain")

    launch_options: Dict[str, Any] = {"headless": HEADLESS, "slow_mo": BROWSER_SLOW_MO}
    if use_chromium and chromium_args:
        launch_options["args"] = chromium_args
    return launch_options


def build_client_certificates() -> List[Dict[str, str]]:
    """Build Playwright client_certificates entries from configured certificate files."""
    origins = ([BROWSER_CLIENT_CERT_ORIGIN] if BROWSER_CLIENT_CERT_ORIGIN else []) + list(BROWSER_CLIENT_CERT_ORIGINS)
    origins = [origin for origin in origins if origin]
    if not origins:
        return []

    client_certs: List[Dict[str, str]] = []
    if BROWSER_CLIENT_CERT_CERT_PATH and BROWSER_CLIENT_CERT_KEY_PATH:
        cert_path = os.path.abspath(BROWSER_CLIENT_CERT_CERT_PATH)
        key_path = os.path.abspath(BROWSER_CLIENT_CERT_KEY_PATH)
        if os.path.isfile(cert_path) and os.path.isfile(key_path):
            for origin in origins:
                client_certs.append({"origin": origin, "certPath": cert_path, "keyPath": key_path})
    elif BROWSER_CLIENT_CERT_PFX_PATH and os.path.isfile(BROWSER_CLIENT_CERT_PFX_PATH):
        pfx_path = os.path.abspath(BROWSER_CLIENT_CERT_PFX_PATH)
        for origin in origins:
            entry = {"origin": origin, "pfxPath": pfx_path}
            if BROWSER_CLIENT_CERT_PASSPHRASE:
                entry["passphrase"] = BROWSER_CLIENT_CERT_PASSPHRASE
            client_certs.append(entry)
    return client_certs


def should_write_auto_select_cert_policy(engine_name: str, user_data_dir: str = BROWSER_USER_DATA_DIR) -> bool:
    return bool(user_data_dir and BROWSER_AUTO_SELECT_CERT_PATTERNS and engine_name == "chromium")
