"""Run one TypeScript test file N times on one variant, each time in a fresh stack.

    uv run python -m hoh.fidelity.race --file PATH --app APP --task TASK --variant A+M
        --runs 10 --out build/fidelity/race/NAME --slot N --ports build/fidelity/ports.json
        [--canned build/fidelity/canned/canned.json] [--oracle] [--rules FILE]
        [--stacks-root DIR] [--project-prefix NAME] [--image-prefix NAME]

The fidelity check's checkpoint: does a port turn red or green as its Python
original does, run after run, with the wait in place? Each run hands the file to
``TsBackend`` as ``tests/<task>.spec.ts``, the name the target has, and keeps what the backend
writes under ``DIR/run-NN/attempt-K``. A run whose environment failed is run again, up to two
times, and then counts as ``env_failed``: never as a status (CLAUDE.md, rule 9).

``DIR/race.json`` holds, for every run, its status, the line and call a red run stopped at,
the fixture's counts (settles, browser calls on the tab, calls that went past the wait,
native dialogs, page errors) and the shadow oracle's status; a summary goes to the terminal.
A variant with a rule (A+C, A+C+M) needs the rules file, as in the study's runs.

The race runs only when no stage or validation of the study runs: inside
``hoh.fidelity.guard.hold``, which looks again before every attempt and stops the race when a
study stack has come up (the cells then run again later, from the start, in a new directory).
It never writes into a directory that holds anything, so no summary mixes runs. Each run names
the quiet window and settle cap its fixture waited with, and the attempts of its test.
``race.json`` also records where the race ran (``provenance``): the commit, which must hold
every change under ``src/``, ``docker/``, ``tools/``, ``scenarios/`` and ``third_party/``
(``uncommitted``; git reports the benchmark as changed when it is checked out at another commit
than the pinned one, or has changes or untracked files inside); the
image's source digest and the IDs of both runner images; the stack's timing settings; the
environment and ``src/hoh/rules.py`` digests; and the SHA-256 of the spec, ``ports.json``, the
canned files' ``canned.json`` and the rules file, with the rule's target. The go rule
(``hoh.fidelity.verdict``) compares these with the files and the validation baselines.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from hoh import docker_backend, envs
from hoh import rules as rules_module
from hoh.backend import Backend, EnvFailure, RunRecord, Variant
from hoh.benchmark import REPO_ROOT
from hoh.fidelity import guard, stack

Run = Callable[..., subprocess.CompletedProcess]

ATTEMPTS = 3
"""A run and two more tries when its environment fails, as the study's runs."""
MESSAGE_LIMIT = 200

_PARTS = {"A": {}, "A+M": {"bug": True}, "A+C": {"rule": "rename"},
          "A+C+M": {"rule": "rename", "bug": True}, "A+R": {"rule": "remove"}}
#: Where every change must be committed before a race (``uncommitted``), and where an untracked
#: file counts too. The review tools that are never committed live in tools/.
COMMITTED = ("src", "docker", "tools", "scenarios", "third_party")
UNTRACKED = ("src", "docker", "scenarios")


def parse_variant(app: str, task: str, code: str) -> Variant:
    """The variant the design doc names ``code`` (A, A+M, A+C, A+C+M or A+R)."""
    if code not in _PARTS:
        raise ValueError(f"variant {code!r}: one of {', '.join(_PARTS)}")
    return Variant(app, task, **_PARTS[code])


def _summary(record: RunRecord) -> dict[str, Any]:
    result = record.result
    stopped = next(
        (t for t in result.get("tests") or () if t.get("outcome") in ("failed", "error")), {}
    )
    message = str(stopped.get("message") or "").strip().splitlines()
    records = result.get("records") or []
    return {
        "status": record.status,
        "reason": result.get("reason"),
        "lineno": stopped.get("lineno"),
        "call": stopped.get("call"),
        "message": message[0][:MESSAGE_LIMIT] if message else None,
        "settle": result.get("settle"),
        "tab_calls": sum(int(r.get("tab_calls") or 0) for r in records),
        "unsettled": sorted({name for r in records for name in r.get("unsettled") or ()}),
        "dialogs": sum(int(r.get("dialogs") or 0) for r in records),
        "page_errors": int((result.get("page_errors") or {}).get("count") or 0),
        "oracle": (record.oracle or {}).get("status"),
        "timing": _timing(records),
        "test_attempts": max((int(t.get("attempts") or 1) for t in result.get("tests") or ()),
                             default=None),
    }


def _timing(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The distinct quiet windows and settle caps the fixture's records name, in order."""
    found: list[dict[str, Any]] = []
    for r in records:
        pair = {"quiet_ms": r.get("quiet_ms"), "settle_timeout_ms": r.get("settle_timeout_ms")}
        if pair not in found:
            found.append(pair)
    return found


def _stop(run: dict[str, Any]) -> str:
    if run["status"] in ("fail", "broken") and run["lineno"] is not None:
        return f"{run['status']} at line {run['lineno']} ({run['call']})"
    return str(run["status"])


def race(
    spec: Path,
    variant: Variant,
    runs: int,
    out_dir: Path,
    backend: Backend,
    *,
    oracle: bool = False,
    guard: Any = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run ``spec`` ``runs`` times on ``variant``; write and return the summary.

    ``guard`` (what ``hoh.fidelity.guard.hold`` yields) looks before every attempt and raises
    ``Overlap`` to stop the race; then no summary is written. ``provenance`` goes into the
    summary as it is. Raises ``FileExistsError`` when ``out_dir`` holds anything."""
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} holds an earlier race's files: move it aside; a race "
                              f"never mixes its runs with another's")
    tests = out_dir / "input" / "tests"
    tests.mkdir(parents=True)
    shutil.copyfile(spec, tests / f"{variant.task}.spec.ts")

    results = []
    for number in range(1, runs + 1):
        failures: list[str] = []
        run: dict[str, Any] | None = None
        for attempt in range(1, ATTEMPTS + 1):
            where = out_dir / f"run-{number:02d}" / f"attempt-{attempt}"
            if guard is not None:
                guard.before_run([f"{variant.app}/{variant.task} {variant.code} run {number} "
                                  f"attempt {attempt}"])
            try:
                record = backend.run(tests, variant, where, oracle=oracle)
            except EnvFailure as exc:
                failures.append(str(exc))
                continue
            run = _summary(record)
            break
        if run is None:
            run = {"status": "env_failed", "reason": failures[-1], "lineno": None, "call": None,
                   "message": None, "settle": None, "tab_calls": 0, "unsettled": [],
                   "dialogs": 0, "page_errors": 0, "oracle": None, "timing": [],
                   "test_attempts": None}
        results.append({"run": number, "attempts": len(failures) + (run["status"] != "env_failed"),
                        "env_failures": failures, **run})

    summary = {
        "file": str(spec),
        "app": variant.app,
        "task": variant.task,
        "variant": variant.code,
        "runs": runs,
        "oracle": oracle,
        "counts": dict(sorted(Counter(r["status"] for r in results).items())),
        "stops": dict(sorted(Counter(_stop(r) for r in results).items())),
        "results": results,
        "provenance": provenance,
    }
    (out_dir / "race.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file(path: Path | None) -> dict[str, str] | None:
    return None if path is None else {"path": str(path), "sha256": _sha256(path)}


def _git(run: Run, repo_root: Path, *args: str) -> str:
    done = run(["git", "-C", str(repo_root), *args], capture_output=True, text=True, timeout=60,
               check=False)
    if done.returncode != 0:
        raise ValueError(f"git {' '.join(args)} failed: {(done.stderr or '').strip()}")
    return done.stdout


def uncommitted(repo_root: Path = REPO_ROOT, run: Run = subprocess.run) -> list[str]:
    """The changed files under ``COMMITTED`` that the commit does not hold, and the untracked
    ones under ``UNTRACKED``: a race records its commit, so it runs only from a tree whose code
    that commit holds."""
    status = _git(run, repo_root, "status", "--porcelain=v1", "--untracked-files=all", "--",
                  *COMMITTED)
    found = []
    for line in status.splitlines():
        code, path = line[:2], line[3:].split(" -> ")[-1].strip()
        top = path.split("/", 1)[0]
        if (code == "??" and top in UNTRACKED) or (code != "??" and top in COMMITTED):
            found.append(path)
    return found


def _image_id(run: Run, image: str) -> str:
    done = run(["docker", "image", "inspect", "--format", "{{.Id}}", image], capture_output=True,
               text=True, timeout=60, check=False)
    if done.returncode != 0 or not done.stdout.strip():
        raise ValueError(f"the image {image} could not be inspected: {(done.stderr or '').strip()}")
    return done.stdout.strip()


def provenance(
    spec: Path,
    variant: Variant,
    *,
    ports: Path,
    canned: Path | None = None,
    rules: Path | None = None,
    image_prefix: str = "hoh",
    repo_root: Path = REPO_ROOT,
    run: Run = subprocess.run,
) -> dict[str, Any]:
    """Where a race runs (module docstring). ``rules`` is read for a variant with a rule only;
    its target for the test must be there."""
    target = None
    if variant.rule is not None:
        if rules is None:
            raise ValueError(f"{variant.code} has a rule: the rules file is needed")
        found = rules_module.load_rules(rules).target(variant.app, variant.task)
        if found is None:
            raise ValueError(f"{rules}: no target for {variant.app}/{variant.task}")
        target = asdict(found)
    images = (f"{image_prefix}/{envs.RUNNER_IMAGE}", f"{image_prefix}/{stack.RUNNER_TS_IMAGE}")
    return {
        "commit": _git(run, repo_root, "rev-parse", "HEAD").strip(),
        "source_digest": stack.source_digest(repo_root),
        "runner_source_digest": docker_backend.runner_source_digest(repo_root),
        "images": {image: _image_id(run, image) for image in images},
        "timing": dict(envs.RUNNER_TIMING),
        "environment": envs.environment_digest(repo_root),
        "rules_source_sha256": rules_module.rules_source_digest(),
        "spec": _file(spec),
        "ports": _file(ports),
        "canned": _file(canned),
        "rules": None if target is None else {**_file(rules), "target": target},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hoh.fidelity.race", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True, type=Path, help="the .spec.ts file to run")
    parser.add_argument("--app", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--variant", required=True, help="A, A+M, A+C, A+C+M or A+R")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--out", required=True, type=Path, help="directory for the runs")
    parser.add_argument("--oracle", action="store_true", help="run the shadow oracle after it")
    parser.add_argument("--rules", type=Path, help="the rules file of a variant with a rule")
    parser.add_argument("--slot", type=int, required=True, help="the slot, named each time")
    parser.add_argument("--ports", required=True, type=Path, help="the converter's ports.json")
    parser.add_argument("--canned", type=Path, help="canned.json, for a canned file")
    parser.add_argument("--stacks-root", type=Path, default=Path("/Users/Shared/hoh-fid"))
    parser.add_argument("--project-prefix", default="hohfid")
    parser.add_argument("--image-prefix", default="hoh")
    args = parser.parse_args(argv)

    variant = parse_variant(args.app, args.task, args.variant)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if variant.rule and args.rules is None:
        parser.error(f"{variant.code} has a rule: give the rules file with --rules")
    for path in (args.file, args.ports, args.canned, args.rules):
        if path is not None and not path.is_file():
            parser.error(f"{path} is not a file")
    changed = uncommitted()
    if changed:
        print("not started: changes under src/, docker/, tools/, scenarios/ or third_party/ "
              f"that the commit does not hold: {', '.join(changed)}", file=sys.stderr)
        return 2
    for problem in (docker_backend.runner_image_problem(args.image_prefix),
                    stack.image_problem(args.image_prefix)):
        if problem:
            print(problem, file=sys.stderr)
            return 2
    try:
        where = provenance(args.file, variant, ports=args.ports, canned=args.canned,
                           rules=args.rules if variant.rule else None,
                           image_prefix=args.image_prefix)
    except ValueError as exc:
        print(f"not started: {exc}", file=sys.stderr)
        return 2
    backend = stack.TsBackend(
        args.stacks_root,
        args.slot,
        project_prefix=args.project_prefix,
        rules=args.rules if variant.rule else None,
    )
    command = f"race {variant.app}/{variant.task} {variant.code} slot {args.slot}"
    try:
        with guard.hold(command) as held:
            summary = race(args.file, variant, args.runs, args.out, backend, oracle=args.oracle,
                           guard=held, provenance=where)
    except FileExistsError as exc:
        print(f"not started: {exc}", file=sys.stderr)
        return 2
    except (guard.Refused, guard.Overlap) as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return 3
    print(f"{summary['task']} {summary['variant']}: {summary['runs']} runs")
    for stop, count in summary["stops"].items():
        print(f"  {count:3d}  {stop}")
    escaped = sorted({name for r in summary["results"] for name in r["unsettled"]})
    if escaped:
        print(f"  calls that went past the wait: {', '.join(escaped)}")
    print(f"summary: {args.out / 'race.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
