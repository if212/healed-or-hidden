"""The ten tests of the fidelity check, drawn before any race.

    uv run python -m hoh.fidelity.draw [--failed APP/TASK ...] [--json build/fidelity/draw.json]

The pool is stage 1b's tests, the tests with a target in the rename rules file, all of them
chosen tests of the selection and so each with a B-oracle bug too, less the dev split. For each
app in the order BookStack, Invoice Ninja, the app's tests sorted by task name are shuffled with
one ``random.Random(20261116)``; the first five of each order are drawn. Nothing here reads a
label of any arm: only the selection, the rules file and the declared mentions.

A test whose stack would not be stage 1b's is ineligible, as the verdict refuses such a dev test:
its target's role is not one 1b's rules handled, its target is an assertion target, or its change
script declares a mention (``scenarios/runtime/script_mentions.yaml``). It keeps its place in the
order, so the orders do not depend on it, and is passed over as a failed port is. In the pool
only BookStack's ``create_page`` is one, the last of its order.

Before the sessions, each drawn port is validated as the checkpoint races the dev tests, with 3
runs per cell (the port on A, A+M and A+C; its reference fix on A+C and A+C+M), and once through
the healer's own ``test_run``. Its statuses are compared with its Python original's in the
validations of stage 1a (A, A+M) and stage 1b (A+C, and the fix on A+C and A+C+M). A port whose
statuses differ, or that opens a native dialog, has failed (``--failed``): it is replaced, in its
place, by the next test of its app's order that has neither failed nor is ineligible. A draw
written over an earlier one (``--json``) must name every test that failed so far, so that none
comes back, and must read the same inputs; otherwise the command refuses (exit 2) and leaves the
file as it was.

Each drawn test gets two sessions, its B-oracle variant (A+M) and its C-rename variant (A+C).
While both apps keep five tests, the order alternates app at every session and every even-length
prefix holds as many B-oracle as C-rename sessions and as many of each app, so a stop leaves
balanced data; an app left with fewer tests has its places left out, and the other app's
sessions then follow one another.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from hoh.benchmark import REPO_ROOT
from hoh.envs import load_script_mentions
from hoh.fidelity.verdict import ROLES_1B
from hoh.rules import load_rules

SEED = 20261116
APPS = ("bookstack", "invoiceninja")
PER_APP = 5
SELECTION = Path("scenarios") / "selection.json"
RULES = Path("scenarios") / "rename" / "rules.yaml"
MENTIONS = Path("scenarios") / "runtime" / "script_mentions.yaml"


def pool(selection: Mapping[str, Any], rules: Any) -> dict[str, list[str]]:
    """Per app, sorted: the chosen tests outside the dev split that have a target in ``rules``
    (a ``hoh.rules.RuleSet``)."""
    found = {}
    for app in APPS:
        targets = (rules.targets or {}).get(app) or {}
        found[app] = sorted(cluster["chosen"]
                            for cluster in (selection.get(app) or {}).get("clusters") or ()
                            if cluster.get("dev") is not True and cluster.get("chosen") in targets)
    return found


def orders(tests: Mapping[str, Sequence[str]], seed: int = SEED) -> dict[str, list[str]]:
    """Each app's tests sorted by name, then shuffled, one generator for both apps in turn."""
    rng = random.Random(seed)
    found = {}
    for app in APPS:
        order = sorted(tests.get(app) or ())
        rng.shuffle(order)
        found[app] = order
    return found


def ineligible(rules: Any, mentions: Mapping[tuple[str, str, str], Any]) -> dict[str, list[str]]:
    """Per app, sorted: the tests of ``rules`` whose stack would not be stage 1b's, which the
    verdict refuses as dev tests: a target role 1b's rules did not handle (``ROLES_1B``), an
    assertion target, or a change script with a declared mention (``mentions``, keyed by app,
    task and name, as ``hoh.envs.load_script_mentions`` gives them)."""
    declared = {(app, task) for app, task, _ in mentions}
    found = {}
    for app in APPS:
        targets = (rules.targets or {}).get(app) or {}
        found[app] = sorted(task for task, target in targets.items()
                            if target.role not in ROLES_1B or target.assertion
                            or (app, task) in declared)
    return found


def drawn(order: Sequence[str], failed: Collection[str], k: int = PER_APP,
          ineligible: Collection[str] = ()) -> list[str]:
    """The first ``k`` of ``order``, each failed or ineligible one replaced in its place by the
    next test of the order that is neither; a place with no test left stays empty."""
    unknown = sorted(set(failed) - set(order))
    if unknown:
        raise ValueError(f"{', '.join(unknown)} not in the order")
    passed_over = set(failed) | set(ineligible)
    spare = iter(t for t in order[k:] if t not in passed_over)
    places = [t if t not in passed_over else next(spare, None) for t in order[:k]]
    return [t for t in places if t is not None]


def sessions(chosen: Mapping[str, Sequence[str]]) -> list[tuple[str, str, str]]:
    """(app, task, variant) in the order the sessions run: per place, BookStack's B-oracle,
    Invoice Ninja's C-rename, BookStack's C-rename, Invoice Ninja's B-oracle."""
    bookstack, invoiceninja = (list(chosen.get(app) or ()) for app in APPS)
    found = []
    for i in range(max(len(bookstack), len(invoiceninja))):
        block = [("bookstack", bookstack, "A+M"), ("invoiceninja", invoiceninja, "A+C"),
                 ("bookstack", bookstack, "A+C"), ("invoiceninja", invoiceninja, "A+M")]
        found += [(app, tasks[i], variant) for app, tasks, variant in block if i < len(tasks)]
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hoh.fidelity.draw", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--failed", action="extend", nargs="+", default=[], metavar="APP/TASK",
                        help="a drawn test whose port failed its validation (repeatable)")
    parser.add_argument("--json", type=Path, help="where to write the draw")
    args = parser.parse_args(argv)

    inputs = {str(p): hashlib.sha256((REPO_ROOT / p).read_bytes()).hexdigest()
              for p in (SELECTION, RULES, MENTIONS)}
    if args.json is not None and args.json.exists():
        earlier = json.loads(args.json.read_text(encoding="utf-8"))
        changed = sorted(k for k in inputs.keys() | (earlier.get("inputs") or {}).keys()
                         if inputs.get(k) != (earlier.get("inputs") or {}).get(k))
        dropped = sorted(set(earlier.get("failed") or ()) - set(args.failed))
        problems = []
        if changed:
            problems.append(f"{args.json} was drawn from other inputs: {', '.join(changed)}")
        if dropped:
            problems.append(f"{args.json} has failed {', '.join(dropped)}, which --failed "
                            f"leaves out")
        for problem in problems:
            print(f"refused: {problem}", file=sys.stderr)
        if problems:
            return 2
    rules = load_rules(REPO_ROOT / RULES)
    tests = pool(json.loads((REPO_ROOT / SELECTION).read_text(encoding="utf-8")), rules)
    excluded = ineligible(rules, load_script_mentions(REPO_ROOT / MENTIONS))
    found = orders(tests)
    failed: dict[str, set[str]] = {app: set() for app in APPS}
    for name in args.failed:
        app, _, task = name.partition("/")
        failed.setdefault(app, set()).add(task)
    try:
        if set(failed) - set(APPS):
            raise ValueError(f"{', '.join(sorted(set(failed) - set(APPS)))} not in the order")
        chosen = {app: drawn(found[app], failed[app], ineligible=excluded[app]) for app in APPS}
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    order = sessions(chosen)
    for app in APPS:
        print(f"{app}: drawn {', '.join(chosen[app])}")
        print(f"  order {', '.join(found[app])}")
        if excluded[app]:
            print(f"  ineligible {', '.join(excluded[app])}")
    print(f"{len(order)} sessions:")
    for number, (app, task, variant) in enumerate(order, 1):
        print(f"  {number:2d}  {app}/{task} {variant}")
    if args.json:
        record = {"seed": SEED, "inputs": inputs, "pool": tests, "orders": found,
                  "failed": sorted(args.failed),
                  "ineligible": [f"{app}/{task}" for app in APPS for task in excluded[app]],
                  "drawn": chosen,
                  "sessions": [list(s) for s in order]}
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
