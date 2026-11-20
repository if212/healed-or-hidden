"""Run the TypeScript tests of a workspace once and write down how the run ended.

    python -m pwrun.entry --tests DIR --results DIR --out DIR [--target NODEID]
                          [--pwfix DIR] [--node COMMAND]

The TypeScript counterpart of ``hoh_runner.entry``, for the fidelity check (design doc 5.6).
``--tests`` is the directory with the ``.spec.ts`` files; Playwright Test runs in its parent
with the configuration of ``pwfix`` (``--pwfix``, in the image /opt/node/pwfix), whose test
directory is ``tests/`` and output directory ``test-results/`` there, so node ids read
``tests/<file>::<title>``. ``--results`` is that ``test-results/`` directory: it is emptied
first, Playwright Test keeps its artifacts there, and after the run it gets ``output.log``.
``--out`` receives ``result.json`` and the reporter's ``pw-report.json``.

The steps are the Python runner's: check that the environment is usable (the same
preflight); run Playwright Test in a child process, with our reporter; turn the report and
the exit code into a status with ``hoh_runner.status``, for the target found by its own title
(``own_title``: a describe block around it does not hide it). Playwright Test ends with 0 or 1; any
other end (a signal, an interrupted run) is ``broken``. An environment failure the fixture
recorded makes the run say nothing about the test, as one the preflight finds.

Exit code 0: ``result.json`` describes a run (``env_ok`` is true). Exit code 3: the
environment was not usable or this layer itself failed; the run should be repeated.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from hoh_runner import entry as python_entry
from hoh_runner import preflight
from hoh_runner.entry import EXIT_ENVIRONMENT, EXIT_OK, LOG_NAME, RESULT_NAME
from hoh_runner.status import run_status

REPORT_NAME = "pw-report.json"
PWFIX_DIR = "/opt/node/pwfix"

# As the Python runner's: a log past this keeps its first and last half of the limit.
LOG_LIMIT = python_entry.LOG_LIMIT

# Settings of the invoking shell that would change how Playwright Test runs: PWDEBUG turns the
# timeouts off and opens the inspector, NODE_OPTIONS can load code, DEBUG adds output.
_DROPPED = ("PWDEBUG", "NODE_OPTIONS", "DEBUG")
_DROPPED_PREFIXES = ("PW_TEST_", "PWTEST_", "PLAYWRIGHT_")

# Playwright Test's exit codes for a run that ended as a run: all passed (or no test), or not.
_NORMAL_EXIT = (0, 1)
# What hoh_runner.status counts as a run that could not be carried out.
_BROKEN_EXIT = 3


def _node_version(node: str) -> str | None:
    try:
        proc = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None


def _package_version(path: Path) -> str | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return None


def _versions(pwfix: Path, node: str) -> dict[str, str | None]:
    return {
        "python": platform.python_version(),
        "node": _node_version(node),
        "playwright_test": _package_version(_cli(pwfix).parent / "package.json"),
    }


def _blank(target: str | None, versions: dict[str, str | None]) -> dict[str, Any]:
    return {
        "schema": 1,
        "env_ok": True,
        "env_error": None,
        "status": None,
        "reason": None,
        "target": target,
        "target_nodeid": None,
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
        "records": [],
        "versions": dict(versions),
    }


def own_title(report: dict[str, Any], target: str | None) -> str | None:
    """The node id the run is judged on: ``target`` when the run has it;
    else the one test in the target's file whose own title, the last after any ``describe``
    blocks, is the target's, since a T' may put the test inside ``test.describe``; else None."""
    if target is None:
        return None
    nodeids = set(report.get("items") or ()) | {t.get("nodeid") for t in report.get("tests") or ()}
    if target in nodeids:
        return target
    file, _, title = target.partition("::")
    matches = [
        n for n in nodeids
        if isinstance(n, str) and n.partition("::")[0] == file
        and n.partition("::")[2].split(" > ")[-1] == title
    ]
    return matches[0] if len(matches) == 1 else None


def _cli(pwfix: Path) -> Path:
    return pwfix / "node_modules" / "@playwright" / "test" / "cli.js"


def _environment(pwfix: Path, out: Path) -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in _DROPPED and not name.startswith(_DROPPED_PREFIXES)
    }
    env.update(
        PWFIX_REPORT=str(out / REPORT_NAME),
        # `import ... from 'pwfix'` in a test resolves to the package, and the package's own
        # @playwright/test is the one that runs the tests.
        NODE_PATH=os.pathsep.join([str(pwfix.parent), str(pwfix / "node_modules")]),
        FORCE_COLOR="0",
    )
    return env


def _run_playwright(
    tests: Path, results: Path, out: Path, pwfix: Path, node: str, timeout_s: float
) -> tuple[int | None, bool]:
    """Run Playwright Test on the workspace. Returns (exit code, timed out).

    Its output goes to a log outside the results directory, which Playwright Test empties when
    it starts, and is copied there when the run is over.
    """
    cmd = [node, str(_cli(pwfix)), "test", "-c", str(pwfix / "playwright.config.js")]
    cmd.append("--pass-with-no-tests")
    with tempfile.TemporaryDirectory(prefix="pwrun-") as scratch:
        log_path = Path(scratch) / LOG_NAME
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(
                cmd,
                cwd=tests.parent,
                env=_environment(pwfix, out),
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
                python_entry._stop(proc)
                code = proc.wait()
            finally:
                python_entry._stop(proc)
        results.mkdir(parents=True, exist_ok=True)
        target = results / LOG_NAME
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        shutil.copyfile(log_path, target)
    python_entry._trim(target, LOG_LIMIT)
    return code, timed_out


def _run(args: argparse.Namespace, result: dict[str, Any]) -> None:
    tests = Path(args.tests).resolve()
    results = Path(args.results).resolve()
    out = Path(args.out).resolve()
    pwfix = Path(args.pwfix).resolve()

    python_entry._empty(results)
    for needed in (_cli(pwfix), pwfix / "playwright.config.js"):
        if not needed.is_file():
            raise preflight.Unusable(f"no {needed}: the image has no Playwright Test or pwfix")
    settings = preflight.Settings.from_env()
    preflight.check(settings)

    # A tests directory that is gone is a test that is gone: let Playwright Test find nothing.
    tests.mkdir(parents=True, exist_ok=True)
    code, timed_out = _run_playwright(
        tests, results, out, pwfix, args.node, settings.run_timeout_s
    )

    report = python_entry._load(out / REPORT_NAME)
    if report is not None and report.get("env_error"):
        raise preflight.Unusable(str(report["env_error"]))

    unusual = not timed_out and code not in _NORMAL_EXIT
    judged = dict(report or {}, exit_code=_BROKEN_EXIT if unusual else code, timed_out=timed_out)
    nodeid = own_title(judged, args.target)
    result.update(run_status(judged, args.target if nodeid is None else nodeid))
    result["target_nodeid"] = nodeid
    if unusual and result["status"] == "broken":
        result["reason"] = f"Playwright Test exit code {code}"
    result["exit_code"] = code
    result["timed_out"] = timed_out
    if report is not None:
        for key in ("collected", "deselected", "collect_errors", "tests", "final_url", "settle",
                    "page_errors", "records"):
            if key in report:
                result[key] = report[key]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pwrun.entry", description=__doc__)
    parser.add_argument("--tests", required=True, help="directory with the test files")
    parser.add_argument("--results", required=True, help="directory for the log and artifacts")
    parser.add_argument("--out", required=True, help="directory for result.json")
    parser.add_argument("--target", default=None, help="node id of the test the run is about")
    parser.add_argument("--pwfix", default=PWFIX_DIR, help="the fixture package's directory")
    parser.add_argument("--node", default="node", help="the Node.js command")
    args = parser.parse_args(argv)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for name in (RESULT_NAME, REPORT_NAME):
        (out / name).unlink(missing_ok=True)

    versions = _versions(Path(args.pwfix).resolve(), args.node)
    result = _blank(args.target, versions)
    try:
        _run(args, result)
    except preflight.Unusable as err:
        result.update(_blank(args.target, versions), env_ok=False, env_error=str(err))
    except Exception as err:
        # A fault of this layer is not a result of the test either.
        result.update(
            _blank(args.target, versions),
            env_ok=False,
            env_error=f"internal error: {type(err).__name__}: {err}",
        )
    python_entry._write(out / RESULT_NAME, result)
    return EXIT_OK if result["env_ok"] else EXIT_ENVIRONMENT


if __name__ == "__main__":
    sys.exit(main())
