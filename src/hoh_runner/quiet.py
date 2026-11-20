"""Wait until a page has gone quiet.

A page is quiet when its document has finished loading, no request it made is
still open, and, for a given time, nothing has happened in it: no DOM change,
no finished request, no load event. ``quiet.js`` keeps the record inside the
page; this module asks it. The open requests are counted on this side, from
Playwright's events: a page sees a request only when it ends, so a page whose
data (and whatever acts on that data) is still on its way would otherwise look
quiet. A request open longer than ``STALE_MS`` no longer counts (a stream or a
long poll), nor does a WebSocket or an event stream.

Every question is put with ``page.wait_for_function``. That call polls inside
the page, carries on in the new document when the page navigates, works where
the page forbids eval, and is given up by Playwright itself when the time is
over, also when the page cannot run script at all (an open dialog). Nothing
here can wait longer than it was told to.
"""

from __future__ import annotations

import math
import time
import weakref
from pathlib import Path
from typing import Any

from playwright.sync_api import Error, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

QUIET_MS = 500
TIMEOUT_MS = 5000
STALE_MS = 10_000
"""A request open longer than this stops counting: a stream or a long poll, not data."""
_IGNORED_REQUESTS = frozenset({"websocket", "eventsource"})

SOURCE = Path(__file__).with_name("quiet.js").read_text(encoding="utf-8")
"""The tracker, an init script."""

KEY = "__pageActivity__"
"""Name of the tracker's global in the page (the one ``quiet.js`` defines)."""

# Calls one method of the tracker, after making sure the document has one: a
# document that got none when it was created (the one that was open when the
# tracker was registered, or an error page) starts being tracked here.
_CALL = "([name, arg]) => {\n" + SOURCE + "\nreturn window." + KEY + "[name](arg);\n}"

_POLL_MS = 25
_READ_TIMEOUT_MS = 1000
_RETRY_PAUSE_S = 0.05

Stamp = tuple[str, int]


def read_text(page: Page, expression: str, arg: object = None, timeout_ms: float = 5000) -> str:
    """Ask the page a question whose answer is a non-empty string.

    ``expression`` is a JavaScript function; the wait ends when it returns
    something truthy. Raises ``playwright.sync_api.TimeoutError`` after
    ``timeout_ms`` (also when the page cannot run script at all), and
    ``Error`` when the function throws or the page is gone.

    The answer travels as a string because Playwright hands a string over
    together with the handle: reading it needs no second trip to the page,
    which could not be given a time limit.
    """
    handle = page.wait_for_function(
        expression, arg=arg, timeout=max(1.0, timeout_ms), polling=_POLL_MS
    )
    text = str(handle)
    try:
        handle.dispose()
    except Error:
        pass
    return text


def _ask(page: Page, name: str, arg: float | None, timeout_ms: float) -> Stamp:
    """Wait until the tracker's method ``name`` returns a stamp, and return it."""
    text = read_text(page, _CALL, [name, arg], timeout_ms)
    doc, sep, count = text.rpartition(":")
    if not sep or not doc or not count.isdigit():
        raise Error(f"unexpected answer from the page: {text[:80]!r}")
    return doc, int(count)


class Requests:
    """The requests of one page that have started and not ended, from Playwright's events.

    The handlers only keep records; they make no call to the browser.
    """

    def __init__(self, page: Page) -> None:
        self._open: dict[Any, float] = {}
        self._last_end = -math.inf
        page.on("request", self._started)
        page.on("requestfinished", self._ended)
        page.on("requestfailed", self._ended)

    def _started(self, request: Any) -> None:
        try:
            kind = request.resource_type
        except Exception:  # noqa: BLE001 - a request that cannot say what it is still counts
            kind = None
        if kind not in _IGNORED_REQUESTS:
            self._open[request] = time.monotonic()

    def _ended(self, request: Any) -> None:
        if self._open.pop(request, None) is not None:
            self._last_end = time.monotonic()

    def idle_ms(self) -> float:
        """0 while a request is open; else how long ago the last one ended (inf if none did)."""
        now = time.monotonic()
        if any((now - started) * 1000 < STALE_MS for started in self._open.values()):
            return 0.0
        return (now - self._last_end) * 1000


_requests: weakref.WeakKeyDictionary[Page, Requests] = weakref.WeakKeyDictionary()


def install(page: Page) -> None:
    """Track activity in the page's current document and in every later one, and the
    requests the page makes from now on.

    Raises ``playwright.sync_api.Error`` when the page cannot be reached.
    """
    if page not in _requests:
        _requests[page] = Requests(page)
    page.context.add_init_script(SOURCE)
    _ask(page, "stamp", None, TIMEOUT_MS)


def stamp(page: Page) -> Stamp | None:
    """Document id and activity count of the page right now; None if it cannot be read.

    Two equal stamps mean: the same document, and nothing happened in between.
    """
    if page.is_closed():
        return None
    try:
        return _ask(page, "stamp", None, _READ_TIMEOUT_MS)
    except Error:
        return None


def settle_stamped(
    page: Page, quiet_ms: float = QUIET_MS, timeout_ms: float = TIMEOUT_MS
) -> tuple[bool, Stamp | None]:
    """Like ``settle``; also returns the page's stamp at the moment it was found quiet.

    When the cap was hit, the stamp is read afterwards and may be None.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    requests = _requests.get(page)
    while not page.is_closed():
        left_ms = (deadline - time.monotonic()) * 1000
        if left_ms <= 0:
            break
        try:
            found = _ask(page, "quiet", quiet_ms, left_ms)
        except PlaywrightTimeout:
            break
        except Error:
            # The page could not be asked just now (it is being replaced, say).
            time.sleep(_RETRY_PAUSE_S)
            continue
        idle_ms = math.inf if requests is None else requests.idle_ms()
        if idle_ms >= quiet_ms:
            return True, found
        # A request is open, or ended a moment ago: give it time, then ask the page again. A
        # call to the browser, not a sleep, so that Playwright delivers the request events.
        left_ms = (deadline - time.monotonic()) * 1000
        if left_ms <= 0:
            break
        try:
            page.wait_for_timeout(min(max(quiet_ms - idle_ms, _POLL_MS), left_ms))
        except Error:
            time.sleep(_RETRY_PAUSE_S)
    return False, stamp(page)


def settle(page: Page, quiet_ms: float = QUIET_MS, timeout_ms: float = TIMEOUT_MS) -> bool:
    """Wait until the page is quiet: loaded, and no activity for ``quiet_ms``.

    A navigation during the wait starts the wait again in the new document.
    Returns False when the page did not go quiet within ``timeout_ms`` in total
    (or cannot be reached at all). Never raises for anything the page does.
    """
    return settle_stamped(page, quiet_ms, timeout_ms)[0]
