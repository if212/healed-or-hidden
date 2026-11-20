"""Look at the tab once more, after the tests have finished with it.

    python -m hoh_runner.oracle --snippet FILE --out DIR

A separate process from the one that ran the tests. It attaches to the same
tab, waits for the page to go quiet, notes where the tab is and what its
``sessionStorage`` holds, and then executes the statements of the snippet one
after the other, with ``page``, ``expect`` and ``re`` at hand. It stops at the
first statement that raises, so the position of that statement is known. The
result goes to ``oracle.json`` in the output directory.

The snippet's own steps get the same wait for a quiet page as a test's.

``status`` is ``pass`` when every statement ran, ``fail`` when one raised an
``AssertionError``, ``error`` for anything else that keeps the statements from
giving an answer: another exception, a snippet that cannot be parsed, a tab
that is gone or no longer the only one.

Exit code 0: ``oracle.json`` holds a result. Exit code 3: the browser could
not be reached, or the wait could not be put in place (``env_ok`` is false).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any

from playwright.sync_api import Error, Page, expect, sync_playwright

from hoh_runner import hooks, preflight, quiet
from hoh_runner.status import call_name

ORACLE_NAME = "oracle.json"
MESSAGE_LIMIT = 4000

EXIT_OK = 0
EXIT_ENVIRONMENT = 3

_STORAGE = """() => {
  const items = {};
  for (let i = 0; i < sessionStorage.length; i++) {
    const key = sessionStorage.key(i);
    items[key] = sessionStorage.getItem(key);
  }
  return JSON.stringify(items);
}"""


def _blank() -> dict[str, Any]:
    return {
        "schema": 1,
        "env_ok": True,
        "env_error": None,
        "status": None,
        "stmt_index": None,
        "n_statements": 0,
        "exc_type": None,
        "call": None,
        "message": None,
        "url": None,
        "storage": {},
        "tabs": None,
        "settle": {"calls": 0, "timeouts": 0, "rechecks": 0},
    }


def _storage(page: Page) -> dict[str, str]:
    """Everything in the tab's ``sessionStorage``; empty when it cannot be read."""
    try:
        items = json.loads(quiet.read_text(page, _STORAGE))
    except (Error, ValueError):
        return {}
    return items if isinstance(items, dict) else {}


def _line_in(filename: str, err: BaseException) -> int | None:
    """The innermost line of ``filename`` on the way to where ``err`` was raised."""
    line = None
    tb = err.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == filename:
            line = tb.tb_lineno
        tb = tb.tb_next
    return line


def execute(source: str, filename: str, page: Page, result: dict[str, Any]) -> None:
    """Run the top-level statements of ``source`` in order; fill ``result`` in."""
    try:
        body = ast.parse(source, filename=filename).body
    except (SyntaxError, ValueError) as err:
        result.update(status="error", exc_type="SyntaxError", message=str(err)[:MESSAGE_LIMIT])
        return
    result["n_statements"] = len(body)
    scope: dict[str, Any] = {"page": page, "expect": expect, "re": re}
    for index, stmt in enumerate(body):
        try:
            module = ast.Module(body=[stmt], type_ignores=[])
            code = compile(module, filename, "exec", dont_inherit=True)
            exec(code, scope)
        except Exception as err:
            result.update(
                status="fail" if isinstance(err, AssertionError) else "error",
                stmt_index=index,
                exc_type=type(err).__name__,
                call=call_name(source, _line_in(filename, err) or stmt.lineno),
                message=str(err)[:MESSAGE_LIMIT],
            )
            return
    result["status"] = "pass"


def _look(settings: preflight.Settings, source: str, filename: str, result: dict[str, Any]) -> None:
    with sync_playwright() as playwright:
        browser = preflight.connect(playwright, settings.cdp_url, settings.preflight_timeout_s)
        try:
            pages = preflight.tabs(browser)
            result["tabs"] = len(pages)
            if len(pages) != 1:
                # The tab was alone when the tests began. What they left cannot
                # be looked at; that is an answer, not a fault of the environment.
                result.update(
                    status="error",
                    exc_type="LookupError",
                    message=f"expected the one tab the tests ran in, found {len(pages)} tabs",
                )
                return
            page = pages[0]
            try:
                quiet.install(page)
            except Error as err:
                result.update(
                    status="error",
                    exc_type=type(err).__name__,
                    message=f"the tab does not answer: {err}"[:MESSAGE_LIMIT],
                )
                return
            try:
                counters = hooks.activate(page, settings.quiet_ms, settings.settle_timeout_ms)
            except hooks.HookError as err:
                raise preflight.Unusable(str(err)) from None
            try:
                hooks.wait()
                with hooks.paused():  # just waited: the address is read as it stands
                    result["url"] = page.url
                result["storage"] = _storage(page)
                execute(source, filename, page, result)
            finally:
                result["settle"] = counters.as_dict()
                hooks.deactivate()
        finally:
            preflight.disconnect(browser)


def _write(path: Path, data: dict[str, Any]) -> None:
    """Write the file whole, whatever a test left at its path or its temporary name (a test
    runs before this, and can reach the directory)."""
    partial = path.with_name(path.name + ".part")
    for leftover in (partial, path):
        if leftover.is_symlink() or leftover.is_file():
            leftover.unlink()
        elif leftover.is_dir():
            shutil.rmtree(leftover)
    partial.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hoh_runner.oracle", description=__doc__)
    parser.add_argument("--snippet", required=True, help="file with the statements to execute")
    parser.add_argument("--out", required=True, help="directory for oracle.json")
    args = parser.parse_args(argv)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / ORACLE_NAME).unlink(missing_ok=True)

    result = _blank()
    try:
        settings = preflight.Settings.from_env()
        snippet = Path(args.snippet).resolve()
        source = textwrap.dedent(snippet.read_text(encoding="utf-8"))
        _look(settings, source, str(snippet), result)
    except preflight.Unusable as err:
        result = dict(_blank(), env_ok=False, env_error=str(err))
    except Exception as err:
        # A fault of this layer is not an answer about the page.
        result = dict(
            _blank(), env_ok=False, env_error=f"internal error: {type(err).__name__}: {err}"
        )
    _write(out / ORACLE_NAME, result)
    return EXIT_OK if result["env_ok"] else EXIT_ENVIRONMENT


if __name__ == "__main__":
    sys.exit(main())
