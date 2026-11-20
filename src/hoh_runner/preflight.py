"""Is the environment usable?

A run needs a browser that answers on its DevTools address and holds exactly
one tab: the one that was prepared for the test (signed in, start page open).
When that is not so, the run is not a result of the test. It ends as an
environment failure and whoever started it tries again with a fresh
environment.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass

from playwright.sync_api import Browser, Error, Page, Playwright, sync_playwright

from hoh_runner import quiet

_RETRY_PAUSE_S = 0.25
_CONNECT_TIMEOUT_S = 10.0
_READ_TIMEOUT_MS = 5000


class Unusable(Exception):
    """The environment cannot be used. This says nothing about the test."""


@dataclass(frozen=True)
class Settings:
    """What the environment variables say about a run."""

    cdp_url: str
    """HOH_CDP_URL: DevTools address of the browser, for example http://browser:9222."""
    start_url: str | None = None
    """HOH_START_URL: a prefix the tab's address must have before the run."""
    ready_key: str | None = None
    """HOH_READY_KEY: a ``sessionStorage`` key that must hold "true" before the run."""
    quiet_ms: float = quiet.QUIET_MS
    """HOH_QUIET_MS: how long a page must show no activity to count as quiet."""
    settle_timeout_ms: float = quiet.TIMEOUT_MS
    """HOH_SETTLE_TIMEOUT_MS: the longest a single wait for a quiet page may take."""
    preflight_timeout_s: float = 60.0
    """HOH_PREFLIGHT_TIMEOUT_S: how long to keep trying to reach the browser."""
    run_timeout_s: float = 600.0
    """HOH_RUN_TIMEOUT_S: the longest the pytest process may run before it is stopped."""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        def number(name: str, default: float) -> float:
            raw = env.get(name, "").strip()
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError:
                raise Unusable(f"{name} is not a number: {raw!r}") from None
            if value < 0:
                raise Unusable(f"{name} is negative: {raw!r}")
            return value

        cdp_url = env.get("HOH_CDP_URL", "").strip()
        if not cdp_url:
            raise Unusable("HOH_CDP_URL is not set")
        return cls(
            cdp_url=cdp_url,
            start_url=env.get("HOH_START_URL", "").strip() or None,
            ready_key=env.get("HOH_READY_KEY", "").strip() or None,
            quiet_ms=number("HOH_QUIET_MS", cls.quiet_ms),
            settle_timeout_ms=number("HOH_SETTLE_TIMEOUT_MS", cls.settle_timeout_ms),
            preflight_timeout_s=number("HOH_PREFLIGHT_TIMEOUT_S", cls.preflight_timeout_s),
            run_timeout_s=number("HOH_RUN_TIMEOUT_S", cls.run_timeout_s),
        )


def _first_line(err: BaseException) -> str:
    text = str(err).strip()
    return text.splitlines()[0] if text else type(err).__name__


def _leave_dialog(dialog: object) -> None:
    """Do nothing about a dialog; see ``connect``."""


def connect(playwright: Playwright, cdp_url: str, timeout_s: float) -> Browser:
    """Connect to the browser as one more DevTools client.

    Keeps trying until ``timeout_s`` has passed, then raises ``Unusable``.

    The process that prepared the tab stays connected, and its Playwright
    driver dismisses every JavaScript dialog the moment it opens. A second
    driver doing the same would find the dialog gone, and a driver whose answer
    is refused ("No dialog is showing") ends: on this side the calls of the run
    would never return, on the other side the page scripts registered there
    would be gone for the rest of the run. So this side listens for dialogs,
    which stops its driver from answering them, and leaves them alone.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        attempt_s = min(_CONNECT_TIMEOUT_S, max(1.0, deadline - time.monotonic()))
        try:
            browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=attempt_s * 1000)
            for context in browser.contexts:
                context.on("dialog", _leave_dialog)
            return browser
        except Error as err:
            last = err
        left = deadline - time.monotonic()
        if left <= 0:
            raise Unusable(
                f"no browser at {cdp_url} after {timeout_s:g} s: {_first_line(last)}"
            ) from None
        time.sleep(min(_RETRY_PAUSE_S, left))


def tabs(browser: Browser) -> list[Page]:
    """Every tab the browser has open."""
    return [page for context in browser.contexts for page in context.pages]


def only_tab(browser: Browser) -> Page:
    """The browser's one tab; ``Unusable`` when there is none or more than one."""
    pages = tabs(browser)
    if len(pages) != 1:
        raise Unusable(f"expected exactly one tab, found {len(pages)} tabs")
    return pages[0]


def disconnect(browser: Browser | None) -> None:
    """Drop the connection. The browser and its tabs stay as they are."""
    if browser is None:
        return
    try:
        browser.close()
    except Error:
        pass


def session_value(page: Page, key: str) -> str | None:
    """The value of one ``sessionStorage`` key in the tab, or None when it is absent."""
    answer = quiet.read_text(
        page,
        "(key) => { const v = sessionStorage.getItem(key); return v === null ? '-' : '+' + v; }",
        key,
        _READ_TIMEOUT_MS,
    )
    return answer[1:] if answer.startswith("+") else None


def check(settings: Settings) -> None:
    """Raise ``Unusable`` unless the prepared tab is there and in its starting state."""
    with sync_playwright() as playwright:
        browser = connect(playwright, settings.cdp_url, settings.preflight_timeout_s)
        try:
            page = only_tab(browser)
            if settings.start_url and not page.url.startswith(settings.start_url):
                raise Unusable(f"the tab is at {page.url}, not under {settings.start_url}")
            if settings.ready_key:
                try:
                    value = session_value(page, settings.ready_key)
                except Error as err:
                    raise Unusable(
                        f"cannot read session key {settings.ready_key!r}: {_first_line(err)}"
                    ) from None
                if value != "true":
                    raise Unusable(f"session key {settings.ready_key!r} is {value!r}, not 'true'")
        finally:
            disconnect(browser)
