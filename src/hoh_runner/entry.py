"""Run the tests of a workspace once and write down how the run ended.

    python -m hoh_runner.entry --tests DIR --results DIR --out DIR [--target NODEID]

``--tests`` is the directory with the test files; pytest runs in its parent,
so with a directory named ``tests`` the node ids read ``tests/test_x.py::test_x``.
``--results`` receives what the author of the tests gets to see: ``output.log``
and, after a failure, ``page-snapshot.yml`` and ``screenshot.png``. It is
emptied first. ``--out`` receives ``result.json``; nothing in it is meant for
the author of the tests.

The steps: check that the environment is usable; run pytest in a child
process with the runner's plugin; turn its report and exit code into a status.
pytest runs in a child so that nothing a test does to its own process (leave
it, hang it, crash it) keeps ``result.json`` from being written.

Exit code 0: ``result.json`` describes a run (``env_ok`` is true). Exit code
3: the environment was not usable or this layer itself failed (``env_ok`` is
false); the run says nothing about the tests and should be repeated.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

from hoh_runner import preflight
from hoh_runner.plugin import REPORT_NAME
from hoh_runner.status import run_status

RESULT_NAME = "result.json"
LOG_NAME = "output.log"

# A log is a few kilobytes. One that has grown past this keeps its first and
# its last half of the limit; the middle is left out and the log says so.
LOG_LIMIT = 256 * 1024

EXIT_OK = 0
EXIT_ENVIRONMENT = 3

# Settings of the invoking shell that would change how pytest runs.
_DROPPED = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")


def _versions() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "pytest": metadata.version("pytest"),
        "playwright": metadata.version("playwright"),
    }


def _blank(target: str | None) -> dict[str, Any]:
    return {
        "schema": 1,
        "env_ok": True,
        "env_error": None,
        "status": None,
        "reason": None,
        "target": target,
        "target_found": False,
        "deleted": False,
        "exit_code": None,
        "timed_out": False,
        "collected": 0,
        "deselected": 0,
        "collect_errors": 0,
        "tests": [],
        "final_url": None,
        "settle": {"calls": 0, "timeouts": 0, "rechecks": 0},
        "page_errors": {"count": 0, "first": []},
        "versions": _versions(),
    }


def _empty(directory: Path) -> None:
    """Make ``directory`` an existing, empty directory."""
    directory.mkdir(parents=True, exist_ok=True)
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


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


def _stop(proc: subprocess.Popen) -> None:
    """End the pytest process and everything it started."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _run_pytest(tests: Path, results: Path, out: Path, timeout_s: float) -> tuple[int | None, bool]:
    """Run pytest on ``tests``; its output goes to the log. Returns (exit code, timed out)."""
    cmd = [sys.executable, "-m", "pytest", tests.name]
    # Without this an ini file next to the tests would move the root there and
    # change every node id.
    cmd += ["--rootdir", str(tests.parent)]
    cmd += ["-p", "hoh_runner.plugin", "-p", "no:cacheprovider", "--color=no"]

    env = {name: value for name, value in os.environ.items() if name not in _DROPPED}
    env.update(
        HOH_OUT_DIR=str(out),
        HOH_RESULTS_DIR=str(results),
        # Only pytest's own plugins and the runner's: the same set whatever is installed.
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        # Leave nothing behind in the tests directory.
        PYTHONDONTWRITEBYTECODE="1",
        # The log is UTF-8 whatever locale the process was started in.
        PYTHONUTF8="1",
    )
    with open(results / LOG_NAME, "wb") as log:
        proc = subprocess.Popen(
            cmd,
            cwd=tests.parent,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            code: int | None = proc.wait(timeout=timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop(proc)
            code = proc.wait()
        finally:
            _stop(proc)
    _trim(results / LOG_NAME)
    return code, timed_out


def _trim(log: Path, limit: int = LOG_LIMIT) -> None:
    """Cut the middle out of a log that is larger than ``limit`` bytes."""
    size = log.stat().st_size
    if size <= limit:
        return
    half = limit // 2
    with open(log, "rb") as stream:
        head = stream.read(half)
        stream.seek(size - half)
        tail = stream.read()
    note = f"\n\n... [{size - 2 * half} bytes of output left out] ...\n\n".encode()
    log.write_bytes(head + note + tail)


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _run(args: argparse.Namespace, result: dict[str, Any]) -> None:
    tests = Path(args.tests).resolve()
    results = Path(args.results).resolve()
    out = Path(args.out).resolve()

    _empty(results)
    settings = preflight.Settings.from_env()
    preflight.check(settings)

    # A tests directory that is gone is a test that is gone: let pytest find nothing.
    tests.mkdir(parents=True, exist_ok=True)
    code, timed_out = _run_pytest(tests, results, out, settings.run_timeout_s)

    report = _load(out / REPORT_NAME)
    if report is not None and report.get("env_error"):
        raise preflight.Unusable(str(report["env_error"]))

    judged = dict(report or {}, exit_code=code, timed_out=timed_out)
    result.update(run_status(judged, args.target))
    result["exit_code"] = code
    result["timed_out"] = timed_out
    if report is not None:
        for key in ("collected", "deselected", "collect_errors", "tests", "final_url", "settle",
                    "page_errors"):
            if key in report:
                result[key] = report[key]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hoh_runner.entry", description=__doc__)
    parser.add_argument("--tests", required=True, help="directory with the test files")
    parser.add_argument("--results", required=True, help="directory for the log and artifacts")
    parser.add_argument("--out", required=True, help="directory for result.json")
    parser.add_argument("--target", default=None, help="node id of the test the run is about")
    args = parser.parse_args(argv)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for name in (RESULT_NAME, REPORT_NAME):
        (out / name).unlink(missing_ok=True)

    result = _blank(args.target)
    try:
        _run(args, result)
    except preflight.Unusable as err:
        result.update(_blank(args.target), env_ok=False, env_error=str(err))
    except Exception as err:
        # A fault of this layer is not a result of the test either.
        result.update(
            _blank(args.target),
            env_ok=False,
            env_error=f"internal error: {type(err).__name__}: {err}",
        )
    _write(out / RESULT_NAME, result)
    return EXIT_OK if result["env_ok"] else EXIT_ENVIRONMENT


if __name__ == "__main__":
    sys.exit(main())
