"""Pick one test per distinct bug, and set aside a dev split.

Several benchmark tests share the same bug. Running all of them would count
near-duplicates as independent evidence, so the study runs one test per
distinct bug payload (design doc section 5.2).
"""

from __future__ import annotations

import argparse
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path

from hoh.benchmark import BenchCase, list_tasks, load_case, payload_hash

DEV_SEED = 20261114
DEV_K = 3

_RENAME_ROLES = {"link", "button", "tab", "menuitem"}
_ROLE_CALL = re.compile(r'get_by_role\(\s*"(\w+)"\s*,\s*name\s*=\s*"((?:[^"\\]|\\.)+)"')


@dataclass
class Cluster:
    """All tests of one app that share one bug payload."""

    app: str
    payload: str
    tasks: list[str]
    chosen: str


def has_rename_target(tc: BenchCase) -> bool:
    """True if the test clicks a named link, button, tab or menu item whose name
    appears neither in the bug script nor in the final assertion.

    Such a control can be renamed or removed without touching the bug or the
    oracle, which is what the rename and removal scenarios need.
    """
    final = tc.assertion_check or ""
    for step in tc.steps:
        for role, name in _ROLE_CALL.findall(step.solution):
            if role in _RENAME_ROLES and name not in tc.bug_js and name not in final:
                return True
    return False


def select(app: str, root: Path | None = None) -> list[Cluster]:
    """Group an app's tests by bug payload and choose one test per group."""
    groups: dict[str, list[BenchCase]] = {}
    for task in list_tasks(app, root):
        tc = load_case(app, task, root)
        groups.setdefault(payload_hash(tc.bug_js), []).append(tc)

    clusters = []
    for payload, cases in groups.items():
        tasks = sorted(c.task for c in cases)
        with_target = sorted(c.task for c in cases if has_rename_target(c))
        clusters.append(
            Cluster(
                app=app,
                payload=payload,
                tasks=tasks,
                chosen=with_target[0] if with_target else tasks[0],
            )
        )
    return sorted(clusters, key=lambda c: c.chosen)


def dev_split(clusters: list[Cluster], k: int = DEV_K, seed: int = DEV_SEED) -> list[str]:
    """Payload hashes reserved for the pilot and for prompt tuning."""
    return random.Random(seed).sample(sorted(c.payload for c in clusters), k)


def build_selection(apps: list[str], root: Path | None = None) -> dict:
    out: dict = {}
    for app in apps:
        clusters = select(app, root)
        dev = set(dev_split(clusters))
        out[app] = {
            "n_tests": sum(len(c.tasks) for c in clusters),
            "n_distinct": len(clusters),
            "clusters": [
                {
                    "payload": c.payload,
                    "tasks": c.tasks,
                    "chosen": c.chosen,
                    "dev": c.payload in dev,
                }
                for c in clusters
            ],
        }
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apps", nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    selection = build_selection(args.apps)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(selection, indent=2) + "\n", encoding="utf-8")
    for app, info in selection.items():
        print(f"{app}: {info['n_tests']} tests, {info['n_distinct']} distinct bugs")


if __name__ == "__main__":
    main()
