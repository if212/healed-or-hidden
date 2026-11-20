"""pytest plugin of the runner: the ``page`` fixture, failure artifacts, the report.

``page`` is the one tab that is already open in the browser the run was given.
The plugin connects to it when a test first asks for it, puts the wait for a
quiet page in place, and at the end of the session lets go of the connection
without closing anything, so that whoever looks at the tab next finds it as
the tests left it.

When a test fails, a snapshot of the page and a screenshot go to the results
directory. At the end of the session a report of what was collected and how
each test ended goes to the output directory, for ``hoh_runner.entry``.

Loaded with ``-p hoh_runner.plugin``. The directories come from the
environment (``HOH_OUT_DIR``, ``HOH_RESULTS_DIR``), set by the entry point.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any

import pytest

# Imported here, before any test module is, so that the names stand for the
# installed package whatever files the tests directory holds.
from playwright.sync_api import sync_playwright

from hoh_runner import hooks, preflight, quiet
from hoh_runner.status import call_name

REPORT_NAME = "pytest-report.json"
SNAPSHOT_NAME = "page-snapshot.yml"
SCREENSHOT_NAME = "screenshot.png"

MESSAGE_LIMIT = 4000
PAGE_ERRORS_KEPT = 3
PAGE_ERROR_LIMIT = 500
# What Playwright says when its synchronous API is started in a thread that already runs an
# event loop, such as the one a test's own sync_playwright() started.
_ASYNC_LOOP = "using Playwright Sync API inside the asyncio loop"
_SNAPSHOT_TIMEOUT_MS = 5000
_SCREENSHOT_TIMEOUT_MS = 10000

# Read once, when the plugin is loaded: nothing a test file does later changes
# where the run connects to or where its record goes.
_ENV = {name: value for name, value in os.environ.items() if name.startswith("HOH_")}

# pytest leaves the frames of this module out of the failures it prints.
__tracebackhide__ = True


def _directory(name: str) -> Path | None:
    value = _ENV.get(name, "").strip()
    return Path(value) if value else None


class _Session:
    """What one pytest session connects to and what it records."""

    def __init__(self) -> None:
        self.out_dir = _directory("HOH_OUT_DIR")
        self.results_dir = _directory("HOH_RESULTS_DIR")
        self.playwright: Any = None
        self.browser: Any = None
        self.page: Any = None
        self.counters = hooks.Counters()
        self.env_error: str | None = None
        self.items: list[str] = []
        self.deselected = 0
        self.collect_errors = 0
        self.skipped_collectors = 0
        self.tests: dict[str, dict[str, Any]] = {}
        self.final_url: str | None = None
        self.artifacts_written = False
        self.releasing = False
        self.page_errors: list[str] = []

    # --- the tab

    def tab(self) -> Any:
        """The prepared tab, with the wait in place. Connects on first use."""
        if self.page is None:
            try:
                self._connect()
            except Exception as err:
                self.release()
                if _ASYNC_LOOP in str(err):
                    # The test started a Playwright (or an event loop) of its own in this
                    # thread, so the tab cannot be reached: the test's doing, and the same
                    # every time. It fails like any fixture error.
                    raise
                # Not a property of the test: the session stops and the entry
                # point reports an environment failure.
                self.env_error = f"{type(err).__name__}: {err}"
                pytest.exit("the browser is not available", returncode=3)
        return self.page

    def _connect(self) -> None:
        settings = preflight.Settings.from_env(_ENV)
        self.playwright = sync_playwright().start()
        self.browser = preflight.connect(
            self.playwright, settings.cdp_url, settings.preflight_timeout_s
        )
        page = preflight.only_tab(self.browser)
        # A browser that goes away or a page that crashes while the tests run is the
        # environment's doing, whatever the test then reports. (The benchmark's browser service
        # restarts by itself and logs in again, so a later check would find a fresh start page.)
        self.browser.on("disconnected", lambda _browser: self._lost("the browser went away"))
        page.on("crash", lambda _page: self._lost("the page crashed"))
        page.on("pageerror", self._page_error)
        quiet.install(page)
        self.counters = hooks.activate(page, settings.quiet_ms, settings.settle_timeout_ms)
        self.page = page

    def _page_error(self, error: Any) -> None:
        """An error the page's own script threw; an event handler, so no browser call."""
        self.page_errors.append(str(getattr(error, "message", error))[:PAGE_ERROR_LIMIT])

    def _lost(self, what: str) -> None:
        """Record that the browser or the page was lost; an event handler, so no browser call."""
        if not self.releasing and self.env_error is None:
            self.env_error = f"{what} during the run"

    def release(self) -> None:
        """Let go of the browser. The tab stays open, on the page it is on."""
        self.releasing = True
        hooks.deactivate()
        self.page = None
        preflight.disconnect(self.browser)
        self.browser = None
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

    # --- what was collected

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.collect_errors += 1
        elif report.skipped:
            self.skipped_collectors += 1

    def pytest_deselected(self, items: Sequence[pytest.Item]) -> None:
        self.deselected += len(items)

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.items = [item.nodeid for item in session.items]

    # --- how each test ended

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_makereport(
        self, item: pytest.Item, call: pytest.CallInfo
    ) -> Generator[None, pytest.TestReport, pytest.TestReport]:
        report = yield
        try:
            self._record(item, call, report)
        except Exception as err:
            # A fault of this plugin must not pass for a result of the test.
            self.env_error = f"record of {item.nodeid}: {type(err).__name__}: {err}"
        return report

    def _record(self, item: pytest.Item, call: pytest.CallInfo, report: pytest.TestReport) -> None:
        entry = self.tests.setdefault(
            item.nodeid,
            {
                "nodeid": item.nodeid,
                "outcome": None,
                "phase": None,
                "lineno": None,
                "exc_type": None,
                "call": None,
                "message": None,
                "duration_s": 0.0,
            },
        )
        entry["duration_s"] = round(entry["duration_s"] + report.duration, 3)

        expected_failure = hasattr(report, "wasxfail")
        if report.when == "setup":
            if report.failed:
                outcome = "error"
            elif report.skipped:
                outcome = "xfailed" if expected_failure else "skipped"
            else:
                return
        elif report.when == "call":
            if expected_failure:
                outcome = "xfailed" if report.skipped else "xpassed"
            else:
                outcome = report.outcome
        elif report.failed and entry["outcome"] not in ("failed", "error"):
            outcome = "error"  # the test's own steps were fine, cleaning up after it was not
        else:
            return

        entry["outcome"] = outcome
        entry["phase"] = report.when
        entry.update(self._details(item, call, report))
        if report.failed and report.when != "teardown":
            self._write_artifacts()

    def _details(
        self, item: pytest.Item, call: pytest.CallInfo, report: pytest.TestReport
    ) -> dict[str, Any]:
        excinfo = call.excinfo
        if excinfo is None:
            # Passed, or passed although marked as a strict expected failure.
            text = report.longrepr if isinstance(report.longrepr, str) else None
            return {"lineno": None, "exc_type": None, "call": None, "message": text}
        message = str(excinfo.value) or getattr(report, "wasxfail", "") or ""
        lineno = None
        for tb_entry in reversed(excinfo.traceback):
            if Path(str(tb_entry.path)) == item.path:
                lineno = tb_entry.lineno + 1
                break
        name = None
        if lineno is not None:
            try:
                name = call_name(item.path.read_text(encoding="utf-8"), lineno)
            except (OSError, UnicodeDecodeError):
                name = None
        return {
            "lineno": lineno,
            "exc_type": excinfo.type.__name__,
            "call": name,
            "message": message[:MESSAGE_LIMIT],
        }

    def _write_artifacts(self) -> None:
        """Snapshot and screenshot of the page as the first failing test left it."""
        if self.page is None or self.results_dir is None or self.artifacts_written:
            return
        self.artifacts_written = True
        hooks.wait()
        with hooks.paused():
            try:
                self.results_dir.mkdir(parents=True, exist_ok=True)
                snapshot = self.page.locator("body").aria_snapshot(timeout=_SNAPSHOT_TIMEOUT_MS)
                (self.results_dir / SNAPSHOT_NAME).write_text(snapshot + "\n", encoding="utf-8")
            except Exception:
                pass
            try:
                self.page.screenshot(
                    path=str(self.results_dir / SCREENSHOT_NAME), timeout=_SCREENSHOT_TIMEOUT_MS
                )
            except Exception:
                pass

    # --- the end of the session

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(
        self, session: pytest.Session, exitstatus: int
    ) -> Generator[None, None, None]:
        try:
            return (yield)
        finally:
            try:
                self._finish(int(exitstatus))
            except Exception as err:
                self._write_report(
                    {"schema": 1, "env_error": f"report: {type(err).__name__}: {err}"}
                )

    def _finish(self, exitstatus: int) -> None:
        try:
            if self.page is not None and not self.page.is_closed():
                hooks.wait()
                with hooks.paused():  # just waited: the address is read as it stands
                    self.final_url = self.page.url
        except Exception:
            self.final_url = None
        counters = self.counters.as_dict()
        self.release()
        self._write_report(
            {
                "schema": 1,
                "env_error": self.env_error,
                "exit_status": exitstatus,
                "collected": len(self.items) + self.deselected,
                "deselected": self.deselected,
                "skipped_collectors": self.skipped_collectors,
                "collect_errors": self.collect_errors,
                "items": self.items,
                "tests": [t for t in self.tests.values() if t["outcome"] is not None],
                "final_url": self.final_url,
                "settle": counters,
                "page_errors": {
                    "count": len(self.page_errors),
                    "first": self.page_errors[:PAGE_ERRORS_KEPT],
                },
            }
        )

    def _write_report(self, report: dict[str, Any]) -> None:
        """Write the report whole or not at all: the reader never sees half of it."""
        if self.out_dir is None:
            return
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            partial = self.out_dir / (REPORT_NAME + ".part")
            for leftover in (partial, self.out_dir / REPORT_NAME):  # whatever a test left
                if leftover.is_symlink() or leftover.is_file():
                    leftover.unlink()
                elif leftover.is_dir():
                    shutil.rmtree(leftover)
            partial.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            os.replace(partial, self.out_dir / REPORT_NAME)
        except OSError:
            pass


_KEY = pytest.StashKey[_Session]()


def pytest_configure(config: pytest.Config) -> None:
    # Gone from the pytest process before the first test module is imported: the tests have
    # no business with where the browser is or where the report goes. (Here, not at import:
    # the entry point imports this module for a name, and needs its settings.)
    for name in _ENV:
        os.environ.pop(name, None)
    session = _Session()
    config.stash[_KEY] = session
    config.pluginmanager.register(session)


@pytest.fixture
def page(request: pytest.FixtureRequest) -> Any:
    """The browser tab with the application open."""
    return request.config.stash[_KEY].tab()
