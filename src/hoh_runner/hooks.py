"""Make every browser call wait for a quiet page, and assertions hold on one.

Two things are put in place while a run is active:

1. Every call of Playwright's synchronous API ends up in ``SyncBase._sync``.
   That method is wrapped so that the page is given time to go quiet before
   the call is made. The test's code needs no change and cannot leave the wait
   out by writing its steps differently.

   ``Page.url`` and ``Frame.url``, which never reach the browser, get the
   same wait.

2. An ``expect(...)`` assertion that has to wait for content passes as soon as
   the content is there, which can be before the page has finished changing.
   Every assertion method is wrapped: the page's stamp is taken before and
   after; if it differs (something arrived, or the page moved on), the page is
   left to go quiet and the assertion is made again, at most ``MAX_PASSES``
   times. A failed assertion is raised as it is. The methods in ``READS`` get
   the same treatment: a value that a test reads and then compares with a
   plain ``assert`` is an assertion as well.

``SyncBase._sync`` is not part of Playwright's public interface, so
``activate`` ends by checking that a real call did pass through the wrapper,
and refuses to go on when it did not.
"""

from __future__ import annotations

import contextlib
import functools
import re
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from typing import Any

import greenlet
from playwright._impl._sync_base import SyncBase
from playwright.sync_api import (
    Dialog,
    Error,
    Frame,
    Locator,
    LocatorAssertions,
    Page,
    PageAssertions,
    expect,
)

from hoh_runner import quiet

# pytest leaves the frames of this module out of the failures it prints.
__tracebackhide__ = True

MAX_PASSES = 3

# Reads that return what the page holds at one moment: those that wait for their
# element and return what it holds the moment it is there, and those that do not
# wait at all. On ``Locator``, and on ``Page`` and ``Frame`` where those take a
# selector. Calls that may change the page (``evaluate``, say) are not made again.
READS = (
    "all_inner_texts",
    "all_text_contents",
    "aria_snapshot",
    "count",
    "get_attribute",
    "inner_html",
    "inner_text",
    "input_value",
    "is_checked",
    "is_disabled",
    "is_editable",
    "is_enabled",
    "is_hidden",
    "is_visible",
    "text_content",
)


class HookError(RuntimeError):
    """The wait could not be put in place. A run without it must not happen."""


@dataclass
class Counters:
    calls: int = 0
    """Waits for a quiet page."""
    timeouts: int = 0
    """Waits that ended at the cap, with the page still not quiet."""
    rechecks: int = 0
    """Assertions and reads made again because the page changed while they ran."""

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class _State:
    page: Page
    quiet_ms: float
    timeout_ms: float
    home: greenlet.greenlet
    counters: Counters = field(default_factory=Counters)
    rechecked: int = 0
    busy: bool = False
    replaced: list[tuple[type, str, Any]] = field(default_factory=list)


_state: _State | None = None


def _applies(state: _State | None) -> bool:
    """Whether the code running now is the test itself.

    Not while a wait is already under way (its own calls must go straight
    through), and not in an event handler: handlers run in greenlets of their
    own, in the middle of some other call.
    """
    return state is not None and not state.busy and greenlet.getcurrent() is state.home


def _settle(state: _State) -> quiet.Stamp | None:
    """One counted wait for a quiet page; returns the stamp of the quiet moment."""
    state.counters.calls += 1
    try:
        ok, stamp = quiet.settle_stamped(state.page, state.quiet_ms, state.timeout_ms)
    except Exception:
        ok, stamp = False, None
    if not ok:
        state.counters.timeouts += 1
    return stamp


def _waiting(original: Callable[..., Any]) -> Callable[..., Any]:
    """``SyncBase._sync`` with a wait for a quiet page in front of it."""

    def _sync(self: Any, coro: Any) -> Any:
        __tracebackhide__ = True
        state = _state
        # A dialog stops all script in its page until it is answered: the call
        # that answers it must not wait for that page.
        if _applies(state) and not isinstance(self, Dialog):
            state.busy = True
            try:
                _settle(state)
            except BaseException:
                coro.close()
                raise
            finally:
                state.busy = False
        return original(self, coro)

    return _sync


def _rechecked(original: Callable[..., Any]) -> Callable[..., Any]:
    """An assertion or a read that is made again when the page changed under it."""

    @functools.wraps(original)
    def method(self: Any, *args: Any, **kwargs: Any) -> Any:
        __tracebackhide__ = True
        state = _state
        if not _applies(state):
            return original(self, *args, **kwargs)
        state.rechecked += 1
        attempt = 1
        while True:
            state.busy = True
            try:
                before = _settle(state)
                result = original(self, *args, **kwargs)
                after = quiet.stamp(state.page)
            finally:
                state.busy = False
            if attempt == MAX_PASSES or (before is not None and before == after):
                return result
            attempt += 1
            state.counters.rechecks += 1

    return method


_FILE_URL = re.compile(r"\s*(?:view-source:)?file:", re.IGNORECASE)


def _no_files(goto: Callable[..., Any]) -> Callable[..., Any]:
    """``goto`` that refuses file: addresses, as the browser refuses them to a web page."""

    @functools.wraps(goto)
    def method(self: Any, url: str, *args: Any, **kwargs: Any) -> Any:
        __tracebackhide__ = True
        if isinstance(url, str) and _FILE_URL.match(url):
            raise Error(f"{type(self).__name__}.goto: net::ERR_ACCESS_DENIED at {url}")
        return goto(self, url, *args, **kwargs)

    return method


def _settled(getter: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """A property getter with a wait for a quiet page in front of it."""

    def get(self: Any) -> Any:
        state = _state
        if _applies(state):
            state.busy = True
            try:
                _settle(state)
            finally:
                state.busy = False
        return getter(self)

    return get


def wait() -> bool:
    """Wait for a quiet page now, as before a browser call. False when not active."""
    state = _state
    if not _applies(state):
        return False
    state.busy = True
    try:
        before = state.counters.timeouts
        _settle(state)
        return state.counters.timeouts == before
    finally:
        state.busy = False


@contextlib.contextmanager
def paused() -> Iterator[None]:
    """Let the calls made inside go straight to the browser, without a wait.

    For calls that are not the test's: the snapshot and the screenshot taken
    after a failure, right after one wait of their own.
    """
    state = _state
    if state is None or state.busy:
        yield
        return
    state.busy = True
    try:
        yield
    finally:
        state.busy = False


def active() -> bool:
    return _state is not None


def activate(
    page: Page, quiet_ms: float = quiet.QUIET_MS, timeout_ms: float = quiet.TIMEOUT_MS
) -> Counters:
    """Put the wait in place for ``page``, for the whole process, until ``deactivate``.

    Raises ``HookError`` when a browser call or an assertion does not pass
    through the wrappers; nothing stays wrapped in that case. The counters
    returned start at zero after that check.
    """
    global _state
    if _state is not None:
        raise HookError("the wait is already in place")

    state = _State(page, quiet_ms, timeout_ms, greenlet.getcurrent())
    sync = vars(SyncBase).get("_sync")
    if not callable(sync):
        raise HookError("the wait cannot be put in place: SyncBase._sync is not there")
    state.replaced.append((SyncBase, "_sync", sync))
    for cls in (LocatorAssertions, PageAssertions):
        for name, member in vars(cls).items():
            if name.startswith(("to_", "not_to_")) and callable(member):
                state.replaced.append((cls, name, member))
    if len(state.replaced) < 3:
        raise HookError("the wait cannot be put in place: no assertion methods found")
    for name in READS:
        if not callable(vars(Locator).get(name)):
            raise HookError(f"the wait cannot be put in place: Locator.{name} is not there")
        for cls in (Locator, Page, Frame):
            member = vars(cls).get(name)
            if callable(member):
                state.replaced.append((cls, name, member))

    _state = state
    SyncBase._sync = _waiting(sync)
    for cls, name, member in state.replaced[1:]:
        setattr(cls, name, _rechecked(member))
    # The address is kept on this side and never reaches the browser, so it gets the wait
    # in front of it here: read right after a step, it would otherwise be the address from
    # before a late navigation.
    for cls in (Page, Frame):
        prop = vars(cls).get("url")
        if not isinstance(prop, property) or prop.fget is None:
            deactivate()
            raise HookError(f"the wait cannot be put in place: {cls.__name__}.url is not there")
        state.replaced.append((cls, "url", prop))
        cls.url = property(_settled(prop.fget), doc=prop.__doc__)
    # The browser's own files (its start-up script, among others) are not the application's:
    # a page from the application cannot open file: addresses, and neither can the test.
    for cls in (Page, Frame):
        goto = vars(cls).get("goto")
        if not callable(goto):
            deactivate()
            raise HookError(f"the wait cannot be put in place: {cls.__name__}.goto is not there")
        state.replaced.append((cls, "goto", goto))
        cls.goto = _no_files(goto)
    try:
        _check(state)
    except BaseException:
        deactivate()
        raise
    state.counters = Counters()
    return state.counters


def _check(state: _State) -> None:
    """One harmless call and one harmless assertion must be seen by the wrappers."""
    try:
        seen = state.counters.calls
        state.page.title()
        if state.counters.calls <= seen:
            raise HookError("the wait is not in place: a browser call went past it")
        seen = state.rechecked
        expect(state.page).to_have_title(re.compile(".*"))  # holds for any title
        if state.rechecked <= seen:
            raise HookError("the wait is not in place: an assertion went past it")
    except HookError:
        raise
    except Exception as err:
        raise HookError(f"the wait could not be checked: {type(err).__name__}: {err}") from err


def deactivate() -> None:
    """Take the wrappers off again. Does nothing when they are not on."""
    global _state
    state, _state = _state, None
    if state is None:
        return
    for cls, name, member in state.replaced:
        setattr(cls, name, member)
