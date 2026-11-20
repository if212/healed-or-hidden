"""The fidelity check's go rule: read every race of the checkpoint and say go or
no-go; or, for port validation, keep or replace each drawn port.

    uv run python -m hoh.fidelity.verdict --races build/fidelity/race \\
        --ports build/fidelity/ports.json --canned build/fidelity/canned/canned.json \\
        --validation results/validation --validation results/validation-1b \\
        --selection scenarios/selection.json [--json build/fidelity/verdict.json]

    uv run python -m hoh.fidelity.verdict --races build/fidelity/ports-race \\
        --ports build/fidelity/ports.json --validation results/validation \\
        --validation results/validation-1b --draw build/fidelity/draw.json \\
        --mcp build/fidelity/ports-mcp [--json build/fidelity/ports-verdict.json]

Run it from the directory the races ran from: the paths they recorded are relative to it.

The cells: for each dev test (the chosen test of each dev cluster in
``selection.json``): its port 10 times on A+M with the shadow oracle, red at the final assertion
every time; 3 times on A with the oracle, green with the oracle passing; 3 times on A+C, red at
the rename target; its reference fix 3 times on A+C, green, and 10 times on A+C+M with the
oracle, red at the final assertion. For each canned file to race (``hoh.fidelity.canned``): 10
times on A+M with the oracle, red at its line or indeterminate, and 3 times on A with the
oracle, green. Indeterminate is a run in which the fixture saw no call
on the tab or no settle; the fixture settles once more at the end of every run, so in practice
the calls on the tab decide. A red line is the line where the Python test turned red in stage
1a's or 1b's validation (its signature), mapped through the converter's line map; red means a
failed assertion or step at that line, and with the oracle, the oracle failing too.

Every judged run of every race of a cell counts; none is dropped. A run whose environment failed
is never judged (CLAUDE.md rule 9), and a cell needs a race of its full number of runs with no
such run. Each judged run must also have waited as the study waits (``TIMING``), let no call go
past the wait, met no native dialog, and run its test once. The checkpoint is a no-go when a
cell is, when the static check refuses a file that was raced or passes a canned file it must
refuse, when such a file was raced, or when the runner-ts image's source has changed since the
races.

Before it judges, the verdict ties what it reads together, and refuses (exit 2) when it cannot.
The files raced must be the files on disk, as ``ports.json`` and ``canned.json`` give them, with
the rules file and target each race recorded, and ``canned.json`` must list the files
``hoh.fidelity.canned`` makes from the port now; every race must name the same commit, images,
timing, environment and rules source. The ported tests and reference fixes must be the ones the
baselines of 1a and 1b admitted, with 1b's rename targets, and the Python runner's source the
one their baselines name. The stack differs from stage 1b's (``BASELINE_COMMIT``) by changes
made and reviewed since: the files a stack is assembled from may differ only in ``ALLOWED``,
and those exactly as main held them at ``REVIEWED_COMMIT``. This is checked at the races'
commit or, before any race, at HEAD, the commit a race from this tree would record. Those
changes add a role and declared mentions, so a dev test whose target the new role could touch,
whose target is an assertion target, or whose script declares a mention, is refused: its stack
would not be 1b's. The environment and rules digests differ from the baselines' by the same
changes; they are reported, not refused on.

Exit 0 go, 1 no-go, 2 refused.

Port validation (the fidelity plan's step 6, ``validate``) reads the draw's tests, its
``drawn`` and the ``failed`` it replaced, in place of the dev tests, with the same checks of
the inputs and the same cells but 3 runs each, and no canned file. It also reads each port's run
check through the healer's own ``test_run`` (``hoh.fidelity.spike --steps run``, its
``run_checks``), one on A+M and one on A+C, whose provenance must tie to the files as a race's
does; a run check that ended in an environment failure (an error its record gives the environment,
``error_environment``; a call the environment cut; a failed reset; an environment record:
``spike.decided``) decides nothing. Each reason has a class. The port's: a status, red line or
shadow-oracle result other than the Python original's, a native dialog, or a run check whose only
failed check is ``red`` and whose ``test_run`` answered a summary of one test that ran and did not
fail (``spike.not_red_by_the_port``); such a port has failed and is replaced. The harness's: a run
past the wait, a timing other than the study's, a test run more than once, a race with or without
the oracle against its cell, a run check that failed another check or whose answer was neither red
nor such a summary, a run check that ended in a fault of the spike's own code or in an error its
record does not tell apart; and, for the whole run, the static check refusing a port or its fix and
the runner-ts image's source changed since the races. These stop the step. Not complete: a cell with
no race of its runs free of environment failures, a variant with no run check that decided. A port
is ``replaced`` when the draw lists it as failed, else ``stop``, ``failed``, ``incomplete`` or
``kept`` by its reasons, in that order. Exit 0 when every drawn port is kept, 3 when the step stops,
1 otherwise (a port to replace or a cell to run again), 2 refused.

In both modes a verdict that could not be printed, or written to ``--json``, exits 2 too, with
``refused:`` on stderr: what it printed before the failure is not a verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hoh import docker_backend, envs
from hoh.benchmark import REPO_ROOT
from hoh.fidelity import canned as canned_code
from hoh.fidelity import spike, stack, validations
from hoh.fidelity.static import check_spec

APPS = ("bookstack", "invoiceninja")
TIMING = [{"quiet_ms": 800, "settle_timeout_ms": 5000}]
"""The study's quiet window (``envs.RUNNER_TIMING``) and settle cap, as a run's fixture names
them."""
BASELINE_COMMIT = "8f26e05564d7291b538b5bb199192639c59abaa8"
"""Stage 1b's commit, where its baseline was recorded."""
REVIEWED_COMMIT = "ac0936f3967d6e3284d24ab99cdd057546ba346e"
"""Main after the stack changes since 1b were reviewed and merged."""
ALLOWED = (
    "scenarios/runtime/script_mentions.yaml",
    "src/hoh/envs.py",
    "src/hoh/js/rules.js",
    "src/hoh/pagescripts.py",
    "src/hoh/rules.py",
)
ROLES_1B = ("link", "button", "tab", "menuitem", "heading")
"""The roles a rename rule handled at stage 1b."""
TARGET_KEYS = {"role", "name", "new_name", "exact"}
BENCHMARK = "third_party/webtestpilot-benchmark"
AGREE = ("commit", "source_digest", "runner_source_digest", "images", "timing", "environment",
         "rules_source_sha256")
"""What every race's provenance must name alike."""
RED, GREEN, RED_OR_INDETERMINATE = "red", "green", "red or indeterminate"
PORT_RUNS = 3
"""Port validation's runs per cell, the red cells' too."""
RUN_CHECKS = ("A+M", "A+C")
"""The variants each drawn port's run check runs on: its sessions'."""
PORT, HARNESS, INCOMPLETE = "port", "harness", "incomplete"
"""The classes of port validation's reasons (module docstring)."""


class Refused(Exception):
    """What the verdict reads does not tie together, so it does not judge."""


@dataclass
class Cell:
    kind: str
    """``port``, ``fix`` (the reference fix's port) or ``canned``."""
    app: str
    task: str
    name: str | None
    """The canned file's name, for a canned cell."""
    variant: str
    runs: int
    oracle: bool
    expect: str
    line: int | None
    """The line it must be red at; None for a green cell."""
    path: Path
    races: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    @property
    def label(self) -> str:
        what = f"canned {self.name}" if self.kind == "canned" else self.kind
        return f"{self.app}/{self.task} {what} {self.variant}"


def dev_tests(selection: Path) -> list[tuple[str, str]]:
    """The chosen test of each dev cluster, app by app."""
    data = json.loads(Path(selection).read_text(encoding="utf-8"))
    return [(app, cluster["chosen"]) for app in APPS
            for cluster in (data.get(app) or {}).get("clusters", []) if cluster.get("dev")]


def _git(*args: str) -> str:
    done = subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True,
                          timeout=60, check=False)
    if done.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {(done.stderr or '').strip()}")
    return done.stdout


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _matches(path: Path, sha256: str | None) -> bool:
    return Path(path).is_file() and _sha256(path) == sha256


def _entry(entries: list[dict[str, Any]], kind: str, app: str, task: str) -> dict[str, Any]:
    found = [e for e in entries if (e.get("kind"), e.get("app"), e.get("task")) == (kind, app, task)
             and e.get("output")]
    if len(found) != 1:
        raise Refused(f"ports.json has {len(found)} ports of {app}/{task} in {kind}, not one")
    return found[0]


def _port_line(entry: dict[str, Any], python_line: int, what: str) -> int:
    line = (entry.get("lines") or {}).get(str(python_line))
    if line is None:
        raise Refused(f"ports.json maps no line of {entry['output']} to the Python line "
                      f"{python_line}, where {what}")
    return int(line)


def _red_line(vals: Sequence[validations.Validation], key: str, check: str) -> int:
    try:
        return validations.red_line(vals, key, check)
    except ValueError as exc:
        raise Refused(str(exc)) from None


def _scenario(vals: Sequence[validations.Validation],
              key: str) -> tuple[validations.Validation, dict[str, Any]]:
    try:
        found, _, baseline = validations.scenario(vals, key)
    except ValueError as exc:
        raise Refused(str(exc)) from None
    return found, baseline


def _check_target(app: str, task: str, target: Any) -> None:
    if not isinstance(target, dict) or set(target) != TARGET_KEYS:
        raise Refused(f"{app}/{task}: stage 1b's target {target} is not a rename target as 1b "
                      f"recorded them (an assertion target, or none)")
    if target["role"] not in ROLES_1B:
        raise Refused(f"{app}/{task}: the target's role {target['role']!r} is not one 1b's rules "
                      f"handled ({', '.join(ROLES_1B)}); its rules have changed since")


def _stop(run: dict[str, Any]) -> str:
    if run.get("status") in ("fail", "broken") and run.get("lineno") is not None:
        return f"{run['status']} at line {run['lineno']} ({run.get('call')})"
    return str(run.get("status"))


def _problems(cell: Cell, run: dict[str, Any]) -> list[tuple[str, str]]:
    """What keeps a judged run from meeting its cell's rule, each with its class."""
    found = []
    if run.get("unsettled"):
        found.append((HARNESS, f"went past the wait: {', '.join(run['unsettled'])}"))
    if run.get("dialogs"):
        found.append((PORT, f"met {run['dialogs']} native dialog(s)"))
    if run.get("timing") != TIMING:
        found.append((HARNESS, f"waited with {run.get('timing')}"))
    if run.get("test_attempts") != 1:
        found.append((HARNESS, f"the test ran {run.get('test_attempts')} times"))
    oracle = run.get("oracle")
    if cell.expect == GREEN:
        if run.get("status") != "pass":
            found.append((PORT, f"not green: {_stop(run)}"))
        if cell.oracle and oracle != "pass":
            found.append((PORT, f"the shadow oracle {oracle}"))
        return found
    red = (run.get("status") == "fail" and run.get("lineno") == cell.line
           and (not cell.oracle or oracle == "fail"))
    unseen = not run.get("tab_calls") or not (run.get("settle") or {}).get("calls")
    if not red and not (cell.expect == RED_OR_INDETERMINATE and unseen):
        what = "the rename target" if cell.variant == "A+C" else "the final assertion"
        found.append((PORT, f"not red at {what} (line {cell.line}): {_stop(run)}, the shadow "
                            f"oracle {oracle}"))
    return found


def _judge(cell: Cell) -> list[tuple[str, str]]:
    """The cell's reasons, each with its class (module docstring)."""
    if not cell.races:
        return [(INCOMPLETE, "not raced")]
    reasons = []
    for name, race in cell.races:
        if bool(race.get("oracle")) != cell.oracle:
            reasons.append((HARNESS, f"{name}: raced {'without' if cell.oracle else 'with'} the "
                                     f"shadow oracle"))
        for run in race.get("results") or []:
            if run.get("status") != "env_failed":
                reasons += [(kind, f"{name} run {run.get('run')}: {text}")
                            for kind, text in _problems(cell, run)]
    if not any(len(race.get("results") or []) >= cell.runs
               and all(r.get("status") != "env_failed" for r in race["results"])
               for _, race in cell.races):
        reasons.append((INCOMPLETE, f"no race of {cell.runs} runs without an environment "
                                    f"failure"))
    return reasons


def port_result(kinds: set[str], *, replaced: bool) -> str:
    """A drawn port's result from its reasons' classes (module docstring)."""
    if replaced:
        return "replaced"
    for kind, result in ((HARNESS, "stop"), (PORT, "failed"), (INCOMPLETE, "incomplete")):
        if kind in kinds:
            return result
    return "kept"


def ports_verdict(results: Sequence[str], run_reasons: Sequence[str]) -> str:
    """Port validation's verdict: ``stop`` on a reason of the whole run or a port's,
    ``go`` when every port left is kept, ``no-go`` otherwise."""
    if run_reasons or "stop" in results:
        return "stop"
    return "go" if all(r in ("kept", "replaced") for r in results) else "no-go"


def _drawn_tests(draw: Path) -> tuple[list[tuple[str, str]], set[str]]:
    """The draw's tests, its drawn ones and then those it replaced, and the replaced ones'
    names."""
    try:
        data = json.loads(Path(draw).read_text(encoding="utf-8"))
        chosen = [(app, task) for app in APPS for task in data["drawn"].get(app) or ()]
        failed = list(data.get("failed") or ())
    except (OSError, KeyError, TypeError, AttributeError, ValueError) as exc:
        raise Refused(f"the draw {draw} cannot be read: {exc!r}") from None
    if not chosen:
        raise Refused(f"the draw {draw} has no drawn test")
    replaced = []
    for name in failed:
        app, _, task = str(name).partition("/")
        if app not in APPS or not task:
            raise Refused(f"the draw's failed {name!r} is not app/task")
        replaced.append((app, task))
    both = sorted(f"{a}/{t}" for a, t in set(chosen) & set(replaced))
    if both:
        raise Refused(f"the draw has {', '.join(both)} both drawn and failed")
    return chosen + replaced, {f"{a}/{t}" for a, t in replaced}


def decide(
    races_dir: Path,
    ports: Path,
    canned: Path,
    validation_dirs: Sequence[Path],
    selection: Path,
    *,
    git: Callable[..., str] | None = None,
    source_digest: Callable[[], str] | None = None,
    runner_digest: Callable[[], str] | None = None,
    mentions: Callable[[], dict[tuple[str, str, str], Any]] | None = None,
    make_canned: Callable[[str, int], Sequence[tuple[Any, str]]] | None = None,
) -> dict[str, Any]:
    """The checkpoint's verdict on the races under ``races_dir`` (module docstring). Raises
    Refused."""
    try:
        tests = dev_tests(selection)
    except (OSError, KeyError, ValueError) as exc:
        raise Refused(f"the inputs cannot be read: {exc}") from None
    if not tests:
        raise Refused(f"{selection} has no dev test")
    return _decide(races_dir, ports, validation_dirs, tests, runs=10, canned=canned, git=git,
                   source_digest=source_digest, runner_digest=runner_digest, mentions=mentions,
                   make_canned=make_canned)


def validate(
    races_dir: Path,
    ports: Path,
    validation_dirs: Sequence[Path],
    draw: Path,
    mcp_dir: Path,
    *,
    git: Callable[..., str] | None = None,
    source_digest: Callable[[], str] | None = None,
    runner_digest: Callable[[], str] | None = None,
    mentions: Callable[[], dict[tuple[str, str, str], Any]] | None = None,
) -> dict[str, Any]:
    """Port validation's verdict on the drawn ports (module docstring). Raises Refused."""
    tests, replaced = _drawn_tests(draw)
    return _decide(races_dir, ports, validation_dirs, tests, runs=PORT_RUNS, canned=None,
                   mcp_dir=Path(mcp_dir), replaced=replaced, git=git,
                   source_digest=source_digest, runner_digest=runner_digest, mentions=mentions)


def _decide(
    races_dir: Path,
    ports: Path,
    validation_dirs: Sequence[Path],
    tests: Sequence[tuple[str, str]],
    *,
    runs: int,
    canned: Path | None,
    mcp_dir: Path | None = None,
    replaced: set[str] | None = None,
    git: Callable[..., str] | None = None,
    source_digest: Callable[[], str] | None = None,
    runner_digest: Callable[[], str] | None = None,
    mentions: Callable[[], dict[tuple[str, str, str], Any]] | None = None,
    make_canned: Callable[[str, int], Sequence[tuple[Any, str]]] | None = None,
) -> dict[str, Any]:
    """The verdict on ``tests``: the checkpoint's with ``canned``, port validation's with
    ``mcp_dir`` (module docstring). Raises Refused."""
    git = git or _git
    source_digest = source_digest or stack.source_digest
    runner_digest = runner_digest or docker_backend.runner_source_digest
    mentions = mentions or envs.load_script_mentions
    make_canned = make_canned or canned_code.make
    races_dir, ports = Path(races_dir), Path(ports)
    try:
        vals = [validations.load(d) for d in validation_dirs]
        entries = json.loads(ports.read_text(encoding="utf-8"))["ports"]
        made = json.loads(Path(canned).read_text(encoding="utf-8")) if canned else None
        declared = mentions()
    except (OSError, KeyError, ValueError) as exc:
        raise Refused(f"the inputs cannot be read: {exc}") from None

    cells: list[Cell] = []
    files: dict[Path, tuple[str, str, str, str | None]] = {}
    targets: dict[tuple[str, str], dict[str, Any]] = {}
    am_lines: dict[tuple[str, str], int] = {}
    port_shas: dict[tuple[str, str], str] = {}
    environments: dict[str, set[str]] = {"1a": set(), "1b": set()}
    runners: dict[str, set[str]] = {"1a": set(), "1b": set()}
    rules_sources: set[str] = set()
    for app, task in tests:
        test_entry = _entry(entries, "tests", app, task)
        fix_entry = _entry(entries, "reference_fixes", app, task)
        port, fix = ports.parent / test_entry["output"], ports.parent / fix_entry["output"]
        for path, entry in ((port, test_entry), (fix, fix_entry)):
            if not _matches(path, entry.get("output_sha256")):
                raise Refused(f"{path}: the SHA-256 in {ports} differs from the file")
        found_1a, base_1a = _scenario(vals, f"b_oracle/{app}/{task}")
        found_1b, base_1b = _scenario(vals, f"c_rename/{app}/{task}")
        for stage, base in (("1a", base_1a), ("1b", base_1b)):
            if base.get("test_sha256") != test_entry.get("source_sha256"):
                raise Refused(f"{app}/{task}: stage {stage}'s baseline test_sha256 is not the "
                              f"SHA-256 of the test the port was made from")
        if not (base_1b.get("reference_fix_sha256") == test_entry.get("reference_fix_sha256")
                == fix_entry.get("source_sha256")):
            raise Refused(f"{app}/{task}: stage 1b's baseline reference_fix_sha256 is not the "
                          f"SHA-256 of the reference fix the port was made from")
        _check_target(app, task, base_1b.get("target"))
        named = sorted(n for (a, t, n) in declared if (a, t) == (app, task))
        if named:
            raise Refused(f"scenarios/runtime/script_mentions.yaml declares {', '.join(named)} "
                          f"for the test {app}/{task}: its stack would not be 1b's")
        targets[(app, task)] = base_1b["target"]
        environments["1a"].add(found_1a.baseline.get("environment"))
        environments["1b"].add(found_1b.baseline.get("environment"))
        runners["1a"].add(found_1a.baseline.get("runner"))
        runners["1b"].add(found_1b.baseline.get("runner"))
        rules_sources.add(base_1b.get("rules_source_sha256"))
        am = _port_line(test_entry, _red_line(vals, f"b_oracle/{app}/{task}", "A-M"),
                        "the test turned red on A+M")
        ac = _port_line(test_entry, _red_line(vals, f"c_rename/{app}/{task}", "A-C"),
                        "the test turned red on A+C")
        acm = _port_line(fix_entry, _red_line(vals, f"c_rename/{app}/{task}", "fix.A-C-M"),
                         "the reference fix turned red on A+C+M")
        am_lines[(app, task)], port_shas[(app, task)] = am, test_entry["output_sha256"]
        files[port.resolve()] = ("port", app, task, None)
        files[fix.resolve()] = ("fix", app, task, None)
        cells += [
            Cell("port", app, task, None, "A+M", runs, True, RED, am, port),
            Cell("port", app, task, None, "A", 3, True, GREEN, None, port),
            Cell("port", app, task, None, "A+C", 3, False, RED, ac, port),
            Cell("fix", app, task, None, "A+C", 3, False, GREEN, None, fix),
            Cell("fix", app, task, None, "A+C+M", runs, True, RED, acm, fix),
        ]

    refused_files: dict[Path, str] = {}
    where = None
    if canned is not None:
        where = _canned_cells(Path(canned), made, ports, entries, am_lines, port_shas, files,
                              refused_files, cells, make_canned)
    by_key = {(c.kind, c.app, c.task, c.name, c.variant): c for c in cells}
    checkpoint, provenances = _read_races(races_dir, ports, canned, files, refused_files, where,
                                          targets, by_key)
    checks_by_test = (_read_run_checks(mcp_dir, ports, files, targets, tests, provenances)
                      if mcp_dir is not None else {})
    bound_at, changed, checked, raced = _bound(provenances, runners, git, source_digest,
                                               runner_digest, checkpoint)

    for path in sorted({c.path.resolve() for c in cells}):
        problems = check_spec(path.read_text(encoding="utf-8"))
        if problems:
            checkpoint.append(f"the static check refuses {path}: {'; '.join(problems)}")
    for path, name in sorted(refused_files.items()):
        if not check_spec(path.read_text(encoding="utf-8")):
            checkpoint.append(f"{name} is not refused by the static check, which must refuse it")

    judged = []
    kinds_of: dict[tuple[str, str], list[tuple[str, str]]] = {test: [] for test in tests}
    for cell in cells:
        reasons = _judge(cell)
        if (cell.app, cell.task) in kinds_of:
            kinds_of[(cell.app, cell.task)] += [(k, f"{cell.label}: {t}") for k, t in reasons]
        runs_seen = [r for _, race in cell.races for r in race.get("results") or []
                     if r.get("status") != "env_failed"]
        judged.append({
            "cell": cell.label, "runs": cell.runs, "oracle": cell.oracle, "expect": cell.expect,
            "line": cell.line, "verdict": "no-go" if reasons else "go",
            "reasons": [text for _, text in reasons], "races": len(cell.races),
            "settle_timeouts": sum(int((r.get("settle") or {}).get("timeouts") or 0)
                                   for r in runs_seen),
            "page_errors": sum(int(r.get("page_errors") or 0) for r in runs_seen),
        })
    report: dict[str, Any] = {
        "verdict": ("go" if not checkpoint and all(c["verdict"] == "go" for c in judged)
                    else "no-go"),
        "commit": raced.get("commit"),
        "bound_checked_at": bound_at,
        "cells": judged,
        "checkpoint_reasons": checkpoint,
        "reported": {
            "environment": {"races": raced.get("environment"),
                            "1a": sorted(environments["1a"]), "1b": sorted(environments["1b"])},
            "rules_source_sha256": {"races": raced.get("rules_source_sha256"),
                                    "1b": sorted(rules_sources)},
            "runner_source_digest": {"checked": checked, "1a": sorted(runners["1a"]),
                                     "1b": sorted(runners["1b"])},
            "changed_since_1b": changed,
            "allowed": list(ALLOWED),
        },
    }
    if mcp_dir is None:
        return report
    found = []
    for app, task in tests:
        reasons = kinds_of[(app, task)] + checks_by_test.get((app, task), [])
        result = port_result({k for k, _ in reasons}, replaced=f"{app}/{task}" in replaced)
        found.append({"port": f"{app}/{task}", "result": result,
                      "reasons": [{"class": k, "reason": t} for k, t in reasons]})
    report["ports"] = found
    report["verdict"] = ports_verdict([p["result"] for p in found], checkpoint)
    return report


def _canned_cells(canned: Path, made: dict[str, Any], ports: Path,
                  entries: list[dict[str, Any]], am_lines: dict[tuple[str, str], int],
                  port_shas: dict[tuple[str, str], str],
                  files: dict[Path, tuple[str, str, str, str | None]],
                  refused_files: dict[Path, str], cells: list[Cell],
                  make_canned: Callable[[str, int], Sequence[tuple[Any, str]]],
                  ) -> tuple[str, str]:
    """The canned files' cells, added to ``cells``, ``files`` and ``refused_files``; returns the
    dev test they are made from."""
    where = (made.get("app"), made.get("task"))
    if where not in am_lines:
        raise Refused(f"{canned} is for {where[0]}/{where[1]}, not a dev test")
    if (made.get("port") or {}).get("sha256") != port_shas[where]:
        raise Refused(f"{canned} was made from a port other than the one in {ports}")
    for item in made.get("files") or []:
        path = canned.parent / item["path"]
        if not _matches(path, item.get("sha256")):
            raise Refused(f"{path}: the SHA-256 in {canned} differs from the file")
        if item.get("final_line") != am_lines[where]:
            raise Refused(f"{canned} gives line {item.get('final_line')} for {item['name']}; the "
                          f"port turns red on A+M at line {am_lines[where]}")
        if item.get("expected") == "refused":
            refused_files[path.resolve()] = item["name"]
            continue
        if item.get("expected") != "raced":
            raise Refused(f"{canned}: {item['name']} is expected {item.get('expected')!r}")
        files[path.resolve()] = ("canned", *where, item["name"])
        cells += [
            Cell("canned", *where, item["name"], "A+M", 10, True, RED_OR_INDETERMINATE,
                 am_lines[where], path),
            Cell("canned", *where, item["name"], "A", 3, True, GREEN, None, path),
        ]
    # canned.json is a build output: made before a change to the routes, it would give other
    # cells, or fewer. Its files must be the ones the code makes from the port now.
    port_text = (ports.parent / _entry(entries, "tests", *where)["output"]).read_text(
        encoding="utf-8")
    try:
        now = {item.name: (item.expected, hashlib.sha256(text.encode("utf-8")).hexdigest())
               for item, text in make_canned(port_text, am_lines[where])}
    except ValueError as exc:
        raise Refused(f"the canned files cannot be made from the port again: {exc}") from None
    listed = {item.get("name"): (item.get("expected"), item.get("sha256"))
              for item in made.get("files") or []}
    if listed != now:
        differ = sorted(str(n) for n in listed.keys() | now.keys() if listed.get(n) != now.get(n))
        raise Refused(f"{canned} does not list the canned files hoh.fidelity.canned makes from "
                      f"the port now ({', '.join(differ)}): make them again")
    return where


def _read_races(races_dir: Path, ports: Path, canned: Path | None,
                files: dict[Path, tuple[str, str, str, str | None]],
                refused_files: dict[Path, str], where: tuple[str, str] | None,
                targets: dict[tuple[str, str], dict[str, Any]],
                by_key: dict[tuple[Any, ...], Cell]) -> tuple[list[str], list[dict[str, Any]]]:
    """Each race under ``races_dir``, tied to its cell (added to the cell's races); returns the
    reasons of the whole run it found and the races' provenances."""
    checkpoint: list[str] = []
    provenances: list[dict[str, Any]] = []
    paths = sorted(races_dir.glob("**/race.json")) if races_dir.is_dir() else []
    for path in paths:
        name = path.parent.relative_to(races_dir).as_posix()
        try:
            race = json.loads(path.read_text(encoding="utf-8"))
            where_raced = race.get("provenance")
            if not where_raced:
                raise Refused(f"{name}: the race recorded no provenance")
            spec = Path(where_raced["spec"]["path"])
            if not _matches(spec, where_raced["spec"].get("sha256")):
                raise Refused(f"{name}: the SHA-256 the race recorded for {spec} differs from the "
                              f"file")
            if (where_raced.get("ports") or {}).get("sha256") != _sha256(ports):
                raise Refused(f"{name}: raced with a ports.json other than {ports}")
            refused = refused_files.get(spec.resolve())
            key = files.get(spec.resolve())
            if key is None and refused is None:
                raise Refused(f"{name}: {spec} matches no cell: it is not a port, a reference fix "
                              f"or a canned file of these inputs")
            kind, app, task, cname = key or ("canned", *(where or (None, None)), refused)
            recorded = where_raced.get("canned")
            if kind == "canned" and recorded is None:
                raise Refused(f"{name}: races the canned file {spec} without canned.json in its "
                              f"provenance")
            if kind == "canned" and (canned is None or recorded.get("sha256") != _sha256(canned)):
                raise Refused(f"{name}: raced with a canned.json other than {canned}")
            if kind != "canned" and recorded is not None:
                raise Refused(f"{name}: races {spec}, which is not a canned file, with canned.json "
                              f"in its provenance")
            if (race.get("app"), race.get("task")) != (app, task):
                raise Refused(f"{name}: names {race.get('app')}/{race.get('task')}, but {spec} is "
                              f"{app}/{task}")
            provenances.append(where_raced)
            if refused is not None:
                checkpoint.append(f"{refused} was raced ({name}), though it is there for the "
                                  f"static check to refuse")
                continue
            cell = by_key.get((kind, app, task, cname, race.get("variant")))
            if cell is None:
                raise Refused(f"{name}: {app}/{task} {kind} {race.get('variant')} matches no cell")
            _check_rules(name, cell.variant, where_raced.get("rules"), targets[(app, task)])
            cell.races.append((name, race))
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise Refused(f"{name}: not a race.json as hoh.fidelity.race writes it: "
                          f"{exc!r}") from None
    return checkpoint, provenances


def _check_rules(name: str, variant: str, rules: dict[str, Any] | None,
                 target: dict[str, Any]) -> None:
    """A variant with C was run with the rules file and stage 1b's target; one without, with
    neither."""
    if ("C" in variant.split("+")) != (rules is not None):
        raise Refused(f"{name}: {variant} {'needs' if rules is None else 'has no'} rules file")
    if rules is not None:
        if not _matches(Path(rules["path"]), rules.get("sha256")):
            raise Refused(f"{name}: the SHA-256 the race recorded for {rules['path']} differs "
                          f"from the file")
        if rules.get("target") != target:
            raise Refused(f"{name}: the rule's target {rules.get('target')} is not stage 1b's "
                          f"target {target}")


def _read_run_checks(mcp_dir: Path, ports: Path,
                     files: dict[Path, tuple[str, str, str, str | None]],
                     targets: dict[tuple[str, str], dict[str, Any]],
                     tests: Sequence[tuple[str, str]],
                     provenances: list[dict[str, Any]],
                     ) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """Each run check under ``mcp_dir`` (``spike.json``), tied to its port as a race is (its
    provenance added to ``provenances``); returns each test's reasons from them, with their
    classes (module docstring)."""
    decided: dict[tuple[str, str, str], list[tuple[str, dict[str, Any]]]] = {}
    faults: dict[tuple[str, str], list[str]] = {}
    paths = sorted(mcp_dir.glob("**/spike.json")) if mcp_dir.is_dir() else []
    for path in paths:
        name = path.parent.relative_to(mcp_dir).as_posix()
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("steps") != "run":
                raise Refused(f"{name}: not a run check (spike --steps run)")
            variant = record["variant"]
            app, task, code = variant["app"], variant["task"], variant["code"]
            if (app, task) not in tests or code not in RUN_CHECKS:
                raise Refused(f"{name}: {app}/{task} {code} matches no drawn port's run check")
            where = record.get("provenance")
            error = record.get("error")
            if error is not None and record.get("error_environment") is not True:
                # a fault of the spike's own code, or an error the record does not tell apart:
                # the harness's, whatever a later check of the variant decides
                faults.setdefault((app, task), []).append(
                    f"run check {name}: a fault of the spike's own code, {str(error)[:200]}")
            if not where and error is not None:
                continue  # it ended before it recorded what it ran: it decides nothing
            if not where:
                raise Refused(f"{name}: the run check recorded no provenance")
            spec = Path(where["spec"]["path"])
            if not _matches(spec, where["spec"].get("sha256")):
                raise Refused(f"{name}: the SHA-256 the run check recorded for {spec} differs "
                              f"from the file")
            if files.get(spec.resolve()) != ("port", app, task, None):
                raise Refused(f"{name}: {spec} is not the port of {app}/{task}")
            if (where.get("ports") or {}).get("sha256") != _sha256(ports):
                raise Refused(f"{name}: run with a ports.json other than {ports}")
            if where.get("canned") is not None:
                raise Refused(f"{name}: a run check names a canned.json")
            _check_rules(name, code, where.get("rules"), targets[(app, task)])
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise Refused(f"{name}: not a spike.json as hoh.fidelity.spike writes it: "
                          f"{exc!r}") from None
        provenances.append(where)
        if spike.decided(record):  # one the environment cut decides nothing
            decided.setdefault((app, task, code), []).append((name, record))

    found: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for app, task in tests:
        reasons = found.setdefault((app, task), [])
        reasons += [(HARNESS, fault) for fault in faults.get((app, task), [])]
        for code in RUN_CHECKS:
            records = decided.get((app, task, code), [])
            if not records:
                reasons.append((INCOMPLETE, f"{code}: no run check that decided"))
            for name, record in records:
                failed = [(check, seen) for check, ok, seen in spike.run_checks(record) if not ok]
                if not failed:
                    continue
                # a replacement cannot be undone: only a summary of one test that ran and did
                # not fail is the port's; any other answer that is not red is the harness's
                only_red = [check for check, _ in failed] == ["red"]
                kind = PORT if only_red and spike.not_red_by_the_port(record) else HARNESS
                reasons.append((kind, f"run check {name}: " + "; ".join(
                    f"{check}: {seen[:200]}" for check, seen in failed)))
    return found


def _bound(provenances: list[dict[str, Any]], runners: dict[str, set[str]],
           git: Callable[..., str], source_digest: Callable[[], str],
           runner_digest: Callable[[], str], checkpoint: list[str],
           ) -> tuple[str, list[str], str, dict[str, Any]]:
    """The races' agreement, the stack's files against stage 1b's and the reviewed changes, and
    the Python runner (module docstring); adds to ``checkpoint`` when the runner-ts image's
    source changed since the races. Returns the commit it checked at, the files changed since
    1b, the runner checked and the races' provenance."""
    for key in AGREE:
        seen = sorted({json.dumps(p.get(key), sort_keys=True) for p in provenances})
        if len(seen) > 1:
            raise Refused(f"the races disagree on {key}: {', '.join(seen)}")
    raced = provenances[0] if provenances else {}
    # Before any race, at HEAD: a race records the commit of the tree it runs from, and
    # checking that commit first saves a race this check would refuse afterwards.
    bound_at = raced["commit"] if provenances else git("rev-parse", "HEAD").strip()
    changed = sorted(set(git("diff", "--name-only", BASELINE_COMMIT, bound_at, "--",
                             *envs.ENVIRONMENT_FILES, "src/hoh/rules.py", BENCHMARK).split()))
    outside = [p for p in changed if p not in ALLOWED]
    if outside:
        raise Refused(f"the files a stack is assembled from differ from stage 1b's commit "
                      f"outside the reviewed changes: {', '.join(outside)}")
    since = sorted(set(git("diff", "--name-only", REVIEWED_COMMIT, bound_at, "--",
                           *ALLOWED).split()))
    if since:
        raise Refused(f"{', '.join(since)} changed after {REVIEWED_COMMIT[:7]}, where the "
                      f"changes since 1b were reviewed")
    # The Python runner (its preflight, the wait's page script, the shadow oracle) must be the
    # one 1a and 1b ran with: the races', or, before any race, this checkout's.
    expected = runners["1a"] | runners["1b"]
    if len(expected) != 1 or None in expected:
        raise Refused(f"the baselines of 1a and 1b do not name one Python runner: "
                      f"{', '.join(sorted(str(r) for r in expected))}")
    [runner] = expected
    checked = raced.get("runner_source_digest") if provenances else runner_digest()
    if checked != runner:
        raise Refused(f"the Python runner {'the races ran with' if provenances else 'here'} "
                      f"({checked}) is not the one stages 1a and 1b ran with ({runner})")
    if provenances:
        now = source_digest()
        if now != raced.get("source_digest"):
            checkpoint.append(f"the runner-ts image's source has changed since the race "
                              f"({raced.get('source_digest')} then, {now} now)")
    return bound_at, changed, checked, raced


def _print(report: dict[str, Any]) -> None:
    what = "port validation" if "ports" in report else "checkpoint"
    print(f"{what}: {report['verdict'].upper()}")
    reported = report["reported"]
    print(f"commit {report['commit']}; environment {reported['environment']['races']} "
          f"(1a {', '.join(reported['environment']['1a'])}; 1b "
          f"{', '.join(reported['environment']['1b'])}); rules source "
          f"{reported['rules_source_sha256']['races']} (1b "
          f"{', '.join(reported['rules_source_sha256']['1b'])})")
    runner = reported["runner_source_digest"]
    print(f"python runner {runner['checked']} (1a {', '.join(runner['1a'])}; 1b "
          f"{', '.join(runner['1b'])})")
    where = report["bound_checked_at"] + ("" if report["commit"] else " (HEAD, no race yet)")
    print(f"changed since stage 1b ({BASELINE_COMMIT[:7]}) at {where}: "
          f"{', '.join(reported['changed_since_1b']) or 'nothing'}")
    for cell in report["cells"]:
        print(f"{cell['verdict']:6} {cell['cell']}  ({cell['races']} race(s); settle timeouts "
              f"{cell['settle_timeouts']}, page errors {cell['page_errors']})")
        for reason in cell["reasons"]:
            print(f"         {reason}")
    for port in report.get("ports") or []:
        print(f"{port['result']:6} {port['port']}")
        for reason in port["reasons"]:
            print(f"         {reason['class']}: {reason['reason']}")
    for reason in report["checkpoint_reasons"]:
        print(f"{'stop' if 'ports' in report else 'no-go':6} {reason}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hoh.fidelity.verdict", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--races", required=True, type=Path, help="the races' directory")
    parser.add_argument("--ports", required=True, type=Path, help="the converter's ports.json")
    parser.add_argument("--canned", type=Path, help="the canned files' map (the checkpoint)")
    parser.add_argument("--validation", required=True, type=Path, action="append",
                        help="a validation directory (results/validation, results/validation-1b)")
    parser.add_argument("--selection", type=Path,
                        help="scenarios/selection.json (the checkpoint's dev tests)")
    parser.add_argument("--draw", type=Path, help="the draw (port validation)")
    parser.add_argument("--mcp", type=Path, help="the run checks' directory (port validation)")
    parser.add_argument("--json", type=Path, help="where to write the verdict as JSON")
    args = parser.parse_args(argv)
    checkpoint = {"--selection": args.selection, "--canned": args.canned}
    ports = {"--draw": args.draw, "--mcp": args.mcp}
    wrong = ([f"{k} is for the checkpoint" for k, v in checkpoint.items() if v] if args.draw
             else [f"{k} is for port validation" for k, v in ports.items() if v])
    needed = ports if args.draw else checkpoint
    wrong += [f"{k} is needed" for k, v in needed.items() if not v]
    if wrong:
        print(f"refused: {'; '.join(wrong)}", file=sys.stderr)
        return 2
    try:
        if args.draw:
            report = validate(args.races, args.ports, args.validation, args.draw, args.mcp)
        else:
            report = decide(args.races, args.ports, args.canned, args.validation,
                            args.selection)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - it judged nothing, and exit 1 would read as a no-go
        traceback.print_exc()
        print("refused: the verdict failed before it judged", file=sys.stderr)
        return 2
    try:
        _print(report)
        if args.json is not None:
            args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 - a verdict not shown or not kept is none, and exit 1 is a no-go
        traceback.print_exc()
        print("refused: the verdict could not be printed or written", file=sys.stderr)
        return 2
    return {"go": 0, "no-go": 1, "stop": 3}[report["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
