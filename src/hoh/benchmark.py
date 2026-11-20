"""Read test cases and bug scripts from the pinned WebTestPilot benchmark.

The benchmark lives in a git submodule and is never copied into this repo.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_ROOT = Path(
    os.environ.get("HOH_BENCH_ROOT", REPO_ROOT / "third_party" / "webtestpilot-benchmark")
)


class BenchmarkNotFound(RuntimeError):
    """The benchmark checkout is missing or incomplete."""


@dataclass(frozen=True)
class Step:
    action: str
    expectation: str
    solution: str
    action_check: str | None
    url: str | None


@dataclass(frozen=True)
class BenchCase:
    app: str
    task: str
    name: str
    setup_function: str
    steps: tuple[Step, ...]
    assertion_check: str | None
    bug_js: str


def _app_dir(app: str, root: Path | None) -> Path:
    base = Path(root) if root is not None else BENCH_ROOT
    app_dir = base / app
    if not (app_dir / "test_cases").is_dir():
        raise BenchmarkNotFound(
            f"No benchmark test cases at {app_dir / 'test_cases'}. "
            "Run `git submodule update --init` (or `just setup`), "
            "or point HOH_BENCH_ROOT at a checkout of the benchmark."
        )
    return app_dir


def list_tasks(app: str, root: Path | None = None) -> list[str]:
    """Task names of one app, sorted."""
    return sorted(p.stem for p in (_app_dir(app, root) / "test_cases").glob("*.yaml"))


def load_case(app: str, task: str, root: Path | None = None) -> BenchCase:
    app_dir = _app_dir(app, root)
    data = yaml.safe_load((app_dir / "test_cases" / f"{task}.yaml").read_text(encoding="utf-8"))
    steps = tuple(
        Step(
            action=s.get("action") or "",
            expectation=s.get("expectation") or "",
            solution=s.get("solution") or "",
            action_check=s.get("action_check") or None,
            url=s.get("url") or None,
        )
        for s in data["steps"]
    )
    return BenchCase(
        app=app,
        task=task,
        name=data.get("name") or task,
        setup_function=data.get("setup_function") or "",
        steps=steps,
        assertion_check=data.get("assertion_check") or None,
        bug_js=(app_dir / "bugs" / f"{task}.js").read_text(encoding="utf-8"),
    )


def payload_hash(bug_js: str) -> str:
    """Identify what a bug script does to the page, ignoring how it is written up.

    Two scripts with the same hash apply the same mutation. Comments, whitespace,
    console output and everything before the mutation body are ignored.
    """
    start = bug_js.find("onConditionMet")
    body = bug_js[start:] if start >= 0 else bug_js
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    body = re.sub(r"console\.\w+\([^;]*\);?", "", body)
    body = re.sub(r"\s+", "", body)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
