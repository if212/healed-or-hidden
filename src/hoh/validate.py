"""The validation run: which scenarios are deterministic, and the baseline the judge compares with.

    python -m hoh.validate --config FILE [--only APP/TASK ...] [--repeats N] [--slots N]

Before any agent runs, the test of every scenario is run several times on each variant its
family needs (design doc 5.10). For B-oracle that is the generated test (the one handed to
agents) three times on the clean app (A) and three times on the app with the benchmark's change
(A+M), each A+M run followed by the shadow oracle (the benchmark's final assertion, checked again
on the page the test left). B-remove, C-rename, C-assert and Mixed are validated as their
``plan_`` functions say; C-rename, C-assert and Mixed also run the scenario's reference fix
(``hoh.rules``). What comes out:

- the verdict on every scenario (admitted, excluded, or incomplete) with the reasons in words;
- for the admitted ones, the judge's baseline (``baseline.json``, the format ``hoh.worker`` reads)
  and the test-results of the first red run, which every session of the scenario starts from
  (``initial/``);
- the measured red signature of each scenario (design doc 5.3: the step, the line, the exception
  type and the matcher), the baseline flake rate of T on A, and the timings.

Admission (design doc 5.10), with N repetitions (default 3):

1. Every run of a check that must be green (B-oracle and B-remove: A; C-rename, C-assert and
   Mixed: the reference fix on A+C) passed.
2. Every run of a check that must be red (A+M) failed, each at a recorded line with a recorded
   exception type, and all of them at the same line, with the same exception type and the same
   matcher call.
3. After every run with the shadow oracle (A+M) the oracle failed (status fail or error) with a
   complete result, and every result is the same failure as the first: the same status,
   statement, exception type and call, on the same URL path. This is the judge's own comparison
   (``hoh.judge.classify_shadow`` says ``bug_seen``), so the later judging of the scenario takes
   its baseline red for what it is. A check with ``Check.shadow_of`` (the generated test on
   A+C+M of C-rename, C-assert and Mixed) instead has its oracle results classified against the
   first of another check's (the reference fix on A+C+M), and every run must come out the same.
   A C-rename or Mixed test may stop at the renamed control, before the final page, so its
   shadow need not be ``bug_seen``; a C-assert test stops inside the benchmark's final
   assertion, on the final page, where its shadow is expected to be ``bug_seen``.
4. On a variant with a renamed or removed control, the page threw no error after the change.

A rule that one run breaks stays broken whatever the runs still missing do: the scenario is
excluded. A scenario that breaks no rule but lacks runs is incomplete: a run not made yet, a run
whose environment failed in every attempt, a run the harness failed on, or a run of another
version of the test (the generated test changed since). Otherwise it is admitted.

Runs. One job of the queue (``hoh.queue.run_queue``) is one run: (family, app, task, check,
repetition), with the job id ``runs/<family>/<app>/<task>/<check>/<rep>``; a check is a variant
with a test and an expectation, and its name is the variant's code with ``-`` for ``+`` (``A``,
``A-M``), after ``fix.`` when it runs the reference fix (``fix.A-C``). The order is
``hoh.jobs.order``: repetition first, and inside a repetition one task of each app in turn with
the checks of a task together, so that a stop at any moment leaves comparable data. The queue
skips finished jobs, so the same command resumes. Each attempt gets its own directory,
``<output>/runs/.../<rep>/attempt-<k>``, holding

    tests/test_<task>.py   the generated test, or the reference fix, copied there for this run
                           (the runner needs the directory to be called tests)
    out/                   the backend's output directory, kept as it is (result.json,
                           oracle.json, results/, environment.log, backend.json, ...)
    run.json               this run's record (below)

An environment failure (``EnvFailure`` from the backend, a result or an oracle result that says
the environment was unusable, or no oracle result after a run that asked for one: the generated
test cannot close the tab the oracle looks at) is never a run (CLAUDE.md, rule 9). The queue
tries the job again before the next repetition starts, up to ``MAX_ENV_RETRIES`` (2) times;
after that the job stays ``env_failed`` and the same command tries it again. A run that raises
anything else is recorded as ``error`` and not retried: remove its job directory to run it
again.

``run.json``: schema, job {job_id, family, app, task, check, rep}, variant, expect,
oracle_requested, attempt, state (done | env_failed | error), error, slot, started_at (UTC),
wall_s, seconds (the phases of the backend's backend.json, when it wrote one), test {file,
sha256}, rules (the rename or remove rules of the variant, ``hoh.pagescripts.RuleSpec`` as a
mapping each; empty without a rule), runner, environment and rules_source (the digests of the
runner code, of what the stacks are assembled from and of src/hoh/rules.py), status, result
(the runner's result.json), oracle (the oracle's oracle.json), results (the test-results
directory, relative to the attempt directory).
A run of another test, runner, environment or rule than the current one is stale, and so is
a run of a rule made with another ``src/hoh/rules.py`` (``hoh.rules.rules_source_digest``).

Reports, rewritten from what is on disk at the end of every command (also after Ctrl-C), for
every scenario of the configuration whatever ``--only`` said:

    validation.json   schema, written_at, config, repeats, slots, backend, rule, counts
                      {scenarios, admitted, excluded, incomplete}, baseline_flake_rate {a_runs,
                      not_green, rate, scenarios_affected}, env_failures (attempts), and per
                      scenario "<family>/<app>/<task>": family, app, task, test {path, sha256},
                      reference_fix {path, sha256}, target (the rules file's) and rules_source
                      {path, sha256} (src/hoh/rules.py) for the families with rules, verdict,
                      reasons,
                      a_not_green, env_failures, variants {<label>:
                      {check, expect, oracle, as_expected, signature, oracle_signature, shadow
                      and shadow_of (a check with Check.shadow_of), runs: [{rep,
                      state (done | pending | env_failed | error | stale), detail, status,
                      reason, lineno, exc_type, call, step, oracle {status, stmt_index,
                      n_statements, exc_type, call, url}, start_s, test_s, oracle_s, wall_s,
                      slot, started_at, attempts, env_failures, dir}]}}
    baseline.json     {"schema": 1, "about": ..., "runner", "environment", "scenarios":
                      {"<family>/<app>/<task>": {"oracle": {...}, "cells": {...},
                      "test_sha256", and for the families with rules "target",
                      "rules_source_sha256" and (C-rename, C-assert, Mixed)
                      "reference_fix_sha256"}}},
                      admitted scenarios only; the
                      stage configuration's ``baseline``
    initial/          initial/<family>/<app>/<task>/: the test-results files of the first red
                      run, admitted scenarios only; the stage configuration's
                      ``initial_results``
    summary.md        a table per family, ready to paste into docs/runs/validation.md
    summary.json      the queue's progress (hoh.queue)

The configuration (YAML; ``stages/validate-bookstack.yaml`` is an example). Unknown keys are
refused; relative paths are relative to the repository root:

    families     {family: {app: [task, ...] | "selection"} | "selection"}, as in a stage
                 configuration; "selection" takes the chosen tasks of the selection file.
                 every family is validated
    repeats      runs per check and scenario (default 3; --repeats overrides it)
    slots        runs at once (default 1; --slots overrides it)
    backend      {factory: "package.module:callable", options: {...}}, as
                 hoh.fake_backend.load_backend reads it; in the options the string "{slot}" is
                 the slot number (an integer when it is the whole value) and "{family}" the
                 family, as in a stage configuration. One backend is made per run
    output_dir   default results/validation (ignored by git)
    tests_root   <tests_root>/<app>/test_<task>.py, the generated tests (default build/tests);
                 a missing one is generated before anything runs
    selection    default scenarios/selection.json
    rules        the rules file (``hoh.rules``), required with b_remove, c_rename, c_assert or
                 mixed: a
                 target for each of their scenarios. Their backends get it as the option
                 ``rules`` (never give it under backend.options)
    reference_fixes_root
                 <root>/<app>/test_<task>.py, the reference fixes (default
                 build/reference_fixes), made from the rules file before anything runs

Families. ``PLANS`` maps each family to a function that, for one scenario, lists the checks to
run and says which baseline entries to write once the scenario is admitted (``Plan``).
Every family is implemented.

Exit codes: 0 every run this command asked for has a result; 1 some have none (the environment
failed in every attempt, the harness failed, or the test changed): see the reasons, then run
the same command again; 2 the configuration or the inputs are not usable, or another validation
runs on the same output directory (nothing ran); 130 stopped with Ctrl-C (run the same command
again to continue).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import statistics
import sys
import textwrap
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from hoh.backend import Backend, EnvFailure, RunRecord, Variant
from hoh.benchmark import REPO_ROOT, BenchCase, load_case
from hoh.docker_backend import runner_image_problem, runner_source_digest
from hoh.envs import environment_digest
from hoh.fake_backend import load_backend
from hoh.generator import _indent, strip_comments, write_tests
from hoh.jobs import Job, order
from hoh.judge import FAMILIES, classify_shadow
from hoh.queue import Outcome, atomic_write_json, disk_problem, run_queue
from hoh.rules import (
    DEFAULT_FIXES_ROOT,
    RULE_FAMILIES,
    RULES_SOURCE,
    RuleSet,
    fix_path,
    load_rules,
    rules_source_digest,
    write_fix,
)

SCHEMA = 1
BASELINE_SCHEMA = 1
DOCKER_FACTORY = "hoh.docker_backend:DockerBackend"
STAGE = "runs"
"""The first part of every job id, and the directory under the output directory that holds the
runs."""
RUN_RECORD = "run.json"
LOCK_NAME = ".validate.lock"
DEFAULT_OUTPUT = "results/validation"
DEFAULT_TESTS_ROOT = "build/tests"
DEFAULT_SELECTION = "scenarios/selection.json"
DEFAULT_REPEATS = 3
DEFAULT_SLOTS = 1
MAX_ENV_RETRIES = 2
"""Attempts of a run after the first whose environment failed, per command (the queue's rule)."""
EXIT_OK, EXIT_MISSING, EXIT_REFUSED, EXIT_STOPPED = 0, 1, 2, 130
VERDICTS = ("admitted", "excluded", "incomplete")
RULE: tuple[str, ...] = (
    "every run of a check that must be green passed",
    "every run of a check that must be red failed, at a recorded line with a recorded exception "
    "type, and all of them at the same line with the same exception type and matcher call",
    "after every run with the shadow oracle, the oracle failed with a complete result, and every "
    "result is the same failure as the first (status, statement, exception type and call, on "
    "the same URL path: hoh.judge.classify_shadow says bug_seen)",
    "a check with shadow_of has its shadows classified against the first oracle result of the "
    "check it names, and every run comes out the same",
    "on a variant with a renamed or removed control the page threw no error after the change",
    "a rule one run breaks excludes the scenario; a scenario that breaks none but lacks runs is "
    "incomplete",
)
_ATTEMPT = re.compile(r"attempt-(\d+)")
_ORACLE_FIELDS = ("status", "stmt_index", "n_statements", "exc_type", "call", "url")
TESTS = ("generated", "reference_fix")


class ValidationRefused(ValueError):
    """The configuration, ``--only`` or the inputs keep the validation from starting."""


class ValidationBusy(RuntimeError):
    """Another validation is running on the same output directory."""


# --- what a family validates


@dataclass(frozen=True)
class Check:
    """One kind of run a family's validation makes, ``repeats`` times per scenario."""

    name: str
    """The job's arm and its directory: the variant's code with "-" for "+" ("A", "A-M")."""
    variant: Variant
    expect: str
    """"pass": green in every run. "fail": red in every run, at one place."""
    oracle: bool
    """The shadow oracle runs after each run, and must fail the same way every time."""
    test: str = "generated"
    """The test that runs: "generated" is the test handed to agents, "reference_fix" the
    scenario's reference fix (``hoh.rules``)."""
    shadow_of: str | None = None
    """For a check with the oracle whose test may stop before the final page: the check whose
    first oracle result its shadows are classified against (``hoh.judge.classify_shadow``).
    They need not be failures, but every run must come out the same."""

    def __post_init__(self) -> None:
        if self.test not in TESTS:
            raise ValueError(f"test must be one of {', '.join(TESTS)}, got {self.test!r}")

    @property
    def label(self) -> str:
        """How the reports name the check: the variant's code, after "fix" for the reference
        fix ("A+M", "fix A+C")."""
        return self.variant.code if self.test == "generated" else f"fix {self.variant.code}"


@dataclass(frozen=True)
class Plan:
    """What a family validates for one scenario, and what it writes once it is admitted."""

    checks: tuple[Check, ...]
    baseline: Callable[[Mapping[str, Sequence[dict]]], dict]
    """The judge's baseline entry ({"oracle": ..., "cells": ...}) from the run records of the
    admitted scenario: check name -> run.json of each repetition, in order."""
    initial: str
    """The check whose first run's test-results every session starts from: the variant the
    family hands out."""


def plan_b_oracle(app: str, task: str) -> Plan:
    """B-oracle: the generated test on A (green) and on A+M with the shadow oracle (red).

    Baseline: ``oracle["A+M"]`` is the oracle result of the first red run, the failure the judge
    compares every later shadow with; the test as handed out is red with the change seen on A+M
    (``cells["A+M"]``) and green on A (``cells["A"]``). Sessions are handed A+M, so they start
    from the first red run's test-results.
    """
    clean = Check("A", Variant(app, task), expect="pass", oracle=False)
    changed = Check("A-M", Variant(app, task, bug=True), expect="fail", oracle=True)

    def baseline(records: Mapping[str, Sequence[dict]]) -> dict:
        return {
            "oracle": {changed.variant.code: records[changed.name][0]["oracle"]},
            "cells": {
                changed.variant.code: {"status": "fail", "shadow": "bug_seen"},
                clean.variant.code: {"status": "pass"},
            },
        }

    return Plan(checks=(clean, changed), baseline=baseline, initial=changed.name)


def plan_b_remove(app: str, task: str) -> Plan:
    """B-remove: the generated test on A (green) and on A+R (red at one place; no oracle, the
    removal is always in effect). The control removed is the scenario's target in the rules file
    (``hoh.rules``). Baseline: ``cells["A+R"]`` fail and ``cells["A"]`` pass, no oracle entry.
    Sessions are handed A+R, so they start from the first red run's test-results.
    """
    clean = Check("A", Variant(app, task), expect="pass", oracle=False)
    removed = Check("A-R", Variant(app, task, rule="remove"), expect="fail", oracle=False)

    def baseline(records: Mapping[str, Sequence[dict]]) -> dict:
        return {"cells": {removed.variant.code: {"status": "fail"},
                          clean.variant.code: {"status": "pass"}}}

    return Plan(checks=(clean, removed), baseline=baseline, initial=removed.name)


def plan_c_rename(app: str, task: str) -> Plan:
    """C-rename: the reference fix on A+C (green) and on A+C+M with the oracle (red; its first
    oracle result is ``oracle["A+C+M"]``, because the test handed out may stop before the final
    page on A+C+M); the generated test on A+C (red: ``cells["A+C"]`` and the initial test-results,
    since C-rename hands out A+C) and on A+C+M with the oracle (red: ``cells["A+C+M"]``, whose
    shadow is each run's oracle result classified against the reference fix's first, and must
    come out the same in every run).

    The rename is the scenario's target in the rules file and the reference fix is made from it
    (``hoh.rules``); the oracle of a renamed variant checks the reference fix's final assertion
    (the Docker backend's rules file gives ``RuleSet.oracle_snippet_for``).
    """
    renamed = Variant(app, task, rule="rename")
    both = Variant(app, task, bug=True, rule="rename")
    fix_green = Check("fix.A-C", renamed, expect="pass", oracle=False, test="reference_fix")
    fix_red = Check("fix.A-C-M", both, expect="fail", oracle=True, test="reference_fix")
    handed = Check("A-C", renamed, expect="fail", oracle=False)
    handed_both = Check("A-C-M", both, expect="fail", oracle=True, shadow_of=fix_red.name)

    def baseline(records: Mapping[str, Sequence[dict]]) -> dict:
        reference = records[fix_red.name][0]["oracle"]
        shadow = classify_shadow(records[handed_both.name][0]["oracle"], reference)
        return {
            "oracle": {both.code: reference},
            "cells": {
                renamed.code: {"status": "fail"},
                both.code: {"status": "fail", "shadow": shadow},
            },
        }

    return Plan(checks=(fix_green, fix_red, handed, handed_both), baseline=baseline,
                initial=handed.name)


def plan_c_assert(app: str, task: str) -> Plan:
    """C-assert: C-rename's four checks, admission rules and baseline entry. Its target in the
    rules file says ``assertion: true``, so the page rule rewords the text and the reference fix
    and the shadow oracle change the expected value that checks it instead of a locator
    (``hoh.rules``); the checks are the same. C-assert hands out A+C, as C-rename does. Its test
    stops inside the benchmark's final assertion, on the final page, so the shadow of the
    generated test on A+C+M is expected to be ``bug_seen``; it is classified against the
    reference fix's first oracle result all the same, as in C-rename."""
    return plan_c_rename(app, task)


def plan_mixed(app: str, task: str) -> Plan:
    """Mixed: C-rename's four checks, admission rules and baseline entry, on the same target; only
    the variant handed out differs. Mixed hands out A+C+M, so every session starts from the
    test-results of the generated test's first red run on A+C+M (``initial``), not on A+C. The
    judge reads what C-rename's entry gives: ``cells["A+C"]``, ``cells["A+C+M"]`` with its shadow,
    and ``oracle["A+C+M"]`` from the reference fix's first red run on A+C+M."""
    return replace(plan_c_rename(app, task), initial="A-C-M")


PLANS: dict[str, Callable[[str, str], Plan]] = {
    "b_oracle": plan_b_oracle,
    "b_remove": plan_b_remove,
    "c_rename": plan_c_rename,
    "c_assert": plan_c_assert,
    "mixed": plan_mixed,
}
IMPLEMENTED: tuple[str, ...] = ("b_oracle", "b_remove", "c_rename", "c_assert", "mixed")


# --- the configuration


@dataclass(frozen=True)
class ValidationConfig:
    """A validation, as ``load_config`` reads it (see the module docstring)."""

    families: Mapping[str, Mapping[str, tuple[str, ...]]]
    repeats: int
    slots: int
    backend: Mapping[str, Any]
    output_dir: Path
    tests_root: Path
    source: Path | None = None
    rules: Path | None = None
    reference_fixes_root: Path = REPO_ROOT / DEFAULT_FIXES_ROOT

    @cached_property
    def ruleset(self) -> RuleSet | None:
        """The rules file, read once; None without one."""
        return load_rules(self.rules) if self.rules is not None else None

    def scenarios(self) -> list[tuple[str, str, str]]:
        """Every (family, app, task), in the order of the configuration."""
        return [
            (family, app, task)
            for family, apps in self.families.items()
            for app, tasks in apps.items()
            for task in tasks
        ]


_KEYS = {"families", "repeats", "slots", "backend", "output_dir", "tests_root", "selection",
         "rules", "reference_fixes_root"}
_REQUIRED = {"families", "backend"}


def _check_keys(data: Any, allowed: set[str], required: set[str], where: str) -> dict:
    if not isinstance(data, dict):
        raise ValueError(f"{where} must be a mapping")
    unknown = sorted(str(k) for k in data if k not in allowed)
    if unknown:
        raise ValueError(
            f"{where}: unknown key(s) {', '.join(unknown)}; "
            f"the keys are {', '.join(sorted(allowed))}"
        )
    missing = sorted(required - set(data))
    if missing:
        raise ValueError(f"{where}: missing key(s) {', '.join(missing)}")
    return data


def _path(value: Any, where: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where} must be a path, got {value!r}")
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def _positive(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{where} must be a positive integer, got {value!r}")
    return value


def _families(value: Any, selection: Path) -> dict[str, dict[str, tuple[str, ...]]]:
    """Resolve ``families``; the selection file is read only when a family asks for it."""
    loaded: dict | None = None

    def selected() -> dict:
        nonlocal loaded
        if loaded is None:
            data = json.loads(selection.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"families: {selection} is not a selection file")
            loaded = data
        return loaded

    if not isinstance(value, dict) or not value:
        raise ValueError("families must map each family to its apps and tasks")
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for family, apps in value.items():
        if family not in FAMILIES:
            raise ValueError(
                f"families: unknown family {family!r}; the families are {', '.join(FAMILIES)}"
            )
        if family not in IMPLEMENTED:
            raise ValueError(
                f"families: {family} is not validated yet (see hoh.validate.plan_{family}); "
                f"the families validated are {', '.join(IMPLEMENTED)}"
            )
        if apps == "selection":
            apps = {app: "selection" for app in selected()}
        if not isinstance(apps, dict) or not apps:
            raise ValueError(f"families.{family} must map apps to tasks, or be 'selection'")
        result[family] = {}
        for app, tasks in apps.items():
            if tasks == "selection":
                if app not in selected():
                    raise ValueError(f"families: the selection {selection} has no app {app!r}")
                tasks = [cluster["chosen"] for cluster in selected()[app]["clusters"]]
            if (
                not isinstance(tasks, list)
                or not tasks
                or not all(isinstance(t, str) and t for t in tasks)
            ):
                raise ValueError(
                    f"families.{family}.{app} must be a list of tasks or 'selection', got {tasks!r}"
                )
            if len(set(tasks)) != len(tasks):
                raise ValueError(f"families.{family}.{app} lists a task twice")
            result[family][app] = tuple(tasks)
    return result


def load_config(path: Path) -> ValidationConfig:
    """Read and check a validation configuration (YAML). Raises ValueError on any problem."""
    path = Path(path)
    data = _check_keys(
        yaml.safe_load(path.read_text(encoding="utf-8")), _KEYS, _REQUIRED, str(path)
    )
    selection = _path(data.get("selection", DEFAULT_SELECTION), "selection")
    families = _families(data["families"], selection)
    backend = _check_keys(data["backend"], {"factory", "options"}, {"factory"}, "backend")
    if not isinstance(backend["factory"], str) or ":" not in backend["factory"]:
        raise ValueError("backend.factory must be 'package.module:callable'")
    options = backend.get("options", {})
    if not isinstance(options, dict):
        raise ValueError("backend.options must be a mapping")
    if "rules" in options:
        raise ValueError("backend.options: name the rules file at the top level (rules); the "
                         "backend of a family with rules is given it")
    rules = _path(data["rules"], "rules") if data.get("rules") is not None else None
    config = ValidationConfig(
        families=families,
        repeats=_positive(data.get("repeats", DEFAULT_REPEATS), "repeats"),
        slots=_positive(data.get("slots", DEFAULT_SLOTS), "slots"),
        backend={"factory": backend["factory"], "options": dict(options)},
        output_dir=_path(data.get("output_dir", DEFAULT_OUTPUT), "output_dir"),
        tests_root=_path(data.get("tests_root", DEFAULT_TESTS_ROOT), "tests_root"),
        source=path,
        rules=rules,
        reference_fixes_root=_path(data.get("reference_fixes_root", DEFAULT_FIXES_ROOT),
                                   "reference_fixes_root"),
    )
    needing = [family for family in families if family in RULE_FAMILIES]
    if needing and rules is None:
        raise ValueError(f"rules: {', '.join(needing)} change a control by a rule; name the "
                         f"rules file (hoh.rules)")
    if config.ruleset is not None:
        missing = config.ruleset.missing(config.scenarios())
        if missing:
            raise ValueError(f"rules: {rules} has no target for {', '.join(missing)}")
    for family, app, task in config.scenarios():
        for check in PLANS[family](app, task).checks:
            Job(STAGE, family, app, task, check.name, 1)  # every name must make a valid job id
    return config


# --- the backend of one run


BackendFactory = Callable[[Job, int], Backend]
"""Makes the backend of one run (a job) in the slot it holds."""


def _fill(value: Any, slot: int, family: str) -> Any:
    """The stage configuration's rule: "{slot}" alone is the slot number, an integer; inside a
    longer string it and "{family}" are replaced by text."""
    if isinstance(value, str):
        if value == "{slot}":
            return slot
        return value.replace("{slot}", str(slot)).replace("{family}", family)
    if isinstance(value, Mapping):
        return {key: _fill(item, slot, family) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_fill(item, slot, family) for item in value]
    return value


def backend_spec(config: ValidationConfig, slot: int, family: str) -> dict[str, Any]:
    """The configured backend spec with ``{slot}`` and ``{family}`` filled in; the backend of a
    family with rules also gets the rules file (option ``rules``)."""
    options = _fill(dict(config.backend.get("options", {})), slot, family)
    if family in RULE_FAMILIES and config.rules is not None:
        options["rules"] = str(config.rules)
    return {"factory": config.backend["factory"], "options": options}


def default_backend_factory(config: ValidationConfig) -> BackendFactory:
    """One backend per run, made from the configuration for the run's slot."""

    def factory(job: Job, slot: int) -> Backend:
        return load_backend(backend_spec(config, slot, job.family))

    return factory


class _Slots:
    """Slot numbers 1 to ``count``; a run takes the lowest free one and gives it back after."""

    def __init__(self, count: int):
        self._free = list(range(1, count + 1))
        self._changed = threading.Condition()

    @contextmanager
    def take(self) -> Iterator[int]:
        with self._changed:
            while not self._free:
                self._changed.wait()
            slot = self._free.pop(0)
        try:
            yield slot
        finally:
            with self._changed:
                self._free.append(slot)
                self._free.sort()
                self._changed.notify()


# --- one run


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _backend_seconds(out: Path) -> dict[str, float] | None:
    """The phases of the backend's ``backend.json`` (prepare, start, test, oracle, stop)."""
    note = _read_json(out / "backend.json")
    seconds = note.get("seconds") if note else None
    if not isinstance(seconds, dict):
        return None
    return {str(k): v for k, v in seconds.items() if _number(v) is not None}


def _unusable(run: RunRecord, check: Check, out: Path) -> str | None:
    """Why a finished run says nothing about the test, or None when it is a result."""
    result = run.result if isinstance(run.result, dict) else {}
    if result.get("env_ok") is False:
        detail = result.get("env_error") or "no detail given"
        return f"the run's result says the environment was unusable: {detail}"
    if not check.oracle:
        return None
    if not isinstance(run.oracle, dict):
        note = out / "oracle_error.txt"
        detail = note.read_text(encoding="utf-8").strip() if note.is_file() else "no detail given"
        return f"the shadow oracle gave no result: {detail}"
    if run.oracle.get("env_ok") is False:
        detail = run.oracle.get("env_error") or "no detail given"
        return f"the shadow oracle says the environment was unusable: {detail}"
    return None


def _target(task: str) -> str:
    return f"tests/test_{task}.py::test_{task}"


def _failure(result: Any, task: str) -> dict:
    """Where the run failed, from the runner's entry for the target test (or the first test that
    failed when the target is missing); empty when there is none."""
    tests = result.get("tests") if isinstance(result, dict) else None
    entries = [t for t in tests if isinstance(t, dict)] if isinstance(tests, list) else []
    entry = next((t for t in entries if t.get("nodeid") == _target(task)), None)
    if entry is None:
        entry = next((t for t in entries if t.get("outcome") in ("failed", "error")), None)
    if entry is None:
        return {}
    return {key: entry.get(key) for key in ("lineno", "exc_type", "call", "message")}


def _describe(record: dict, task: str) -> str:
    """One line for the queue's record of a run."""
    text = str(record["status"])
    failure = _failure(record["result"], task)
    if failure.get("lineno") is not None:
        text += f" at line {failure['lineno']}: {failure.get('exc_type')} {failure.get('call')}"
    oracle = record.get("oracle")
    if isinstance(oracle, dict):
        text += f"; oracle {oracle.get('status')}"
        if oracle.get("stmt_index") is not None:
            text += f" at statement {oracle['stmt_index']}"
    return text


def _relative(path: Path, root: Path) -> str:
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _rules_of(ruleset: RuleSet | None, variant: Variant) -> list[dict]:
    """The rules a run of the variant applies, as its record keeps them."""
    if ruleset is None or variant.rule is None:
        return []
    return [asdict(spec) for spec in ruleset.specs_for(variant)]


def _test_of(config: ValidationConfig, check: Check, app: str, task: str) -> Path:
    """The file of the test a check runs."""
    if check.test == "reference_fix":
        return fix_path(config.reference_fixes_root, app, task)
    return config.tests_root / app / f"test_{task}.py"


def _write_record(attempt: Path, record: dict) -> None:
    atomic_write_json(attempt / RUN_RECORD, record)


class _Runner:
    """The queue's worker: one job is one run of one check of one scenario."""

    def __init__(
        self,
        config: ValidationConfig,
        checks: Mapping[tuple[str, str, str, str], Check],
        factory: BackendFactory,
        slots: _Slots,
    ):
        self.config = config
        self.checks = checks
        self.factory = factory
        self.slots = slots

    def __call__(self, job: Job, attempt_dir: Path) -> Outcome:
        attempt = Path(attempt_dir)
        check = self.checks[(job.family, job.app, job.task, job.arm)]
        record: dict[str, Any] = {
            "schema": SCHEMA,
            "job": {"job_id": job.job_id, "family": job.family, "app": job.app,
                    "task": job.task, "check": job.arm, "rep": job.rep},
            "variant": check.variant.code,
            "expect": check.expect,
            "oracle_requested": check.oracle,
            "attempt": attempt.name,
            "state": None,
            "error": None,
            "slot": None,
            "started_at": None,
            "wall_s": None,
            "seconds": None,
            "test": None,
            "status": None,
            "result": None,
            "oracle": None,
            "results": None,
        }
        try:
            return self._run(job, check, attempt, record)
        except Exception as exc:  # a fault of the harness: recorded, never retried as a run
            record.update(state="error", error=traceback.format_exc())
            _write_record(attempt, record)
            return Outcome("error", f"{type(exc).__name__}: {exc}")

    def _run(self, job: Job, check: Check, attempt: Path, record: dict) -> Outcome:
        source = _test_of(self.config, check, job.app, job.task)
        data = source.read_bytes()
        tests = attempt / "tests"
        tests.mkdir()
        name = f"test_{job.task}.py"  # the runner's target, whichever test it is
        (tests / name).write_bytes(data)
        record["test"] = {"file": f"tests/{name}", "sha256": hashlib.sha256(data).hexdigest()}
        record["rules"] = _rules_of(self.config.ruleset, check.variant)
        # The runner code the image was built from (checked before anything runs), and what
        # the stacks are assembled from.
        record["runner"] = runner_source_digest()
        record["environment"] = environment_digest()
        record["rules_source"] = rules_source_digest()  # what made the rules, the oracle, the fix
        out = attempt / "out"
        run: RunRecord | None = None
        problem: str | None = None
        with self.slots.take() as slot:
            record["slot"] = slot
            record["started_at"] = _now()
            began = time.monotonic()
            try:
                backend = self.factory(job, slot)
                run = backend.run(tests, check.variant, out, oracle=check.oracle)
            except EnvFailure as failure:
                problem = str(failure) or type(failure).__name__
            finally:
                record["wall_s"] = round(time.monotonic() - began, 3)
        record["seconds"] = _backend_seconds(out)
        if run is not None:
            problem = _unusable(run, check, out)
        if problem is not None or run is None:
            record.update(state="env_failed", error=problem)
            _write_record(attempt, record)
            return Outcome("env_failed", str(problem))
        record.update(
            state="done",
            status=run.status,
            result=run.result,
            oracle=run.oracle if check.oracle else None,
            results=_relative(run.results_dir, attempt),
        )
        _write_record(attempt, record)
        return Outcome("done", _describe(record, job.task))


# --- running a validation


@contextmanager
def _sole_validation(out_dir: Path) -> Iterator[None]:
    """Hold ``out_dir/.validate.lock`` for the queue and the reports; a second validation on the
    same directory is refused before it does anything."""
    with open(out_dir / LOCK_NAME, "a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValidationBusy(
                f"another validation is running on {out_dir} ({LOCK_NAME} is locked)"
            ) from None
        yield


def _chosen(
    scenarios: Sequence[tuple[str, str, str]], only: Sequence[str]
) -> list[tuple[str, str, str]]:
    """The scenarios ``--only`` names (APP/TASK), or all of them."""
    if not only:
        return list(scenarios)
    wanted: set[tuple[str, str]] = set()
    for item in only:
        app, slash, task = str(item).partition("/")
        if not slash or not app or not task or "/" in task:
            raise ValidationRefused(f"--only takes APP/TASK, got {item!r}")
        if not any((a, t) == (app, task) for _, a, t in scenarios):
            raise ValidationRefused(f"--only {item}: the scenario is not in the configuration")
        wanted.add((app, task))
    return [s for s in scenarios if (s[1], s[2]) in wanted]


def prepare(
    config: ValidationConfig, plans: Mapping[tuple[str, str, str], Plan], *, check_backend: bool
) -> list[str]:
    """Make every missing generated test; return what keeps the validation from running."""
    problems: list[str] = []
    for (family, app, task), plan in plans.items():
        key = f"{family}/{app}/{task}"
        try:
            case = load_case(app, task)
        except Exception as exc:  # noqa: BLE001 - any reason is reported, nothing runs
            problems.append(f"{key}: the benchmark task cannot be read: {exc}")
            continue
        if any(c.oracle for c in plan.checks) and not (case.assertion_check or "").strip():
            problems.append(f"{key}: the task has no final assertion for the shadow oracle")
        if not (config.tests_root / app / f"test_{task}.py").is_file():
            try:
                write_tests(app, [task], config.tests_root)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{key}: the test could not be generated: {exc}")
        if any(c.test == "reference_fix" for c in plan.checks):
            try:  # rewritten whenever the test or the rule changed: a build output
                assert config.ruleset is not None  # load_config requires the rules file
                write_fix(config.ruleset, config.tests_root, config.reference_fixes_root,
                          app, task)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{key}: the reference fix could not be made: {exc}")
    if check_backend and plans:
        family = next(iter(plans))[0]
        try:
            load_backend(backend_spec(config, 1, family))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"the backend cannot be made from the configuration: "
                            f"{type(exc).__name__}: {exc}")
        if config.backend["factory"] == DOCKER_FACTORY:
            for problem in (runner_image_problem(), disk_problem(config.output_dir)):
                if problem:
                    problems.append(problem)
    return problems


def run_validation(
    config: ValidationConfig,
    *,
    only: Sequence[str] = (),
    backend_factory: BackendFactory | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """Run the runs that are missing (of the ``only`` scenarios, or of all), then write the
    reports for every scenario of the configuration; return validation.json's content.

    Raises ``ValidationRefused`` before anything runs when the scenarios or the inputs are not
    usable, ``ValidationBusy`` when another validation holds the output directory. A stop
    (KeyboardInterrupt) still writes the reports, from what has finished, and is raised again.
    """
    scenarios = config.scenarios()
    chosen = _chosen(scenarios, only)
    plans = {scenario: PLANS[scenario[0]](scenario[1], scenario[2]) for scenario in scenarios}
    out = config.output_dir
    out.mkdir(parents=True, exist_ok=True)
    with _sole_validation(out):
        problems = prepare(config, plans, check_backend=backend_factory is None)
        if problems:
            raise ValidationRefused("\n".join(problems))
        checks = {
            (family, app, task, check.name): check
            for (family, app, task), plan in plans.items()
            for check in plan.checks
        }
        runner = _Runner(config, checks, backend_factory or default_backend_factory(config),
                         _Slots(config.slots))
        jobs = order([
            Job(STAGE, family, app, task, check.name, rep)
            for family, app, task in chosen
            for rep in range(1, config.repeats + 1)
            for check in plans[(family, app, task)].checks
        ])
        try:
            run_queue(jobs, runner, out, slots=config.slots, max_env_retries=MAX_ENV_RETRIES,
                      on_summary=on_progress)
        finally:
            report = write_reports(config, plans, out)
    return report


# --- where a failing line is in the test


def _step_spans(test_text: str, case: BenchCase) -> list[tuple[int, int, str]] | None:
    """(first line, last line, step) for each snippet of the generated test, or None when the
    text is not what ``hoh.generator`` makes of the case.

    The generator writes, after the ``def`` line, each step's action, then its check, and the
    final assertion last (``generator._snippets``); the parts are rendered the same way here and
    must give the function's body exactly.
    """
    lines = test_text.split("\n")
    heads = [i for i, line in enumerate(lines) if line.startswith(f"def test_{case.task}(")]
    if len(heads) != 1:
        return None
    parts: list[tuple[str, str]] = []
    for number, step in enumerate(case.steps, start=1):
        parts.append((f"step {number}", step.solution))
        if step.action_check:
            parts.append((f"step {number} check", step.action_check))
    if case.assertion_check:
        parts.append(("final assertion", case.assertion_check))
    spans: list[tuple[int, int, str]] = []
    rendered: list[str] = []
    first = heads[0] + 2  # 1-based number of the line after the def line
    for label, snippet in parts:
        try:
            text = _indent(strip_comments(textwrap.dedent(snippet)))
        except Exception:  # noqa: BLE001 - a snippet the generator would refuse: no steps
            return None
        count = text.count("\n")
        if count:
            spans.append((first, first + count - 1, label))
        first += count
        rendered.append(text)
    if "".join(rendered) != "\n".join(lines[heads[0] + 1:]):
        return None
    return spans


def step_of(test_text: str, case: BenchCase, lineno: int | None) -> str | None:
    """The part of the generated test that line ``lineno`` belongs to: "step <n>" (its action),
    "step <n> check" or "final assertion"; None for any other line or an unknown layout."""
    if isinstance(lineno, bool) or not isinstance(lineno, int):
        return None
    for first, last, label in _step_spans(test_text, case) or ():
        if first <= lineno <= last:
            return label
    return None


# --- reading the runs back


def _attempt_number(path: Path) -> int:
    match = _ATTEMPT.fullmatch(path.name)
    return int(match.group(1)) if match else 0


def _first_line(text: Any) -> str:
    lines = str(text or "").strip().splitlines()
    return lines[0] if lines else ""


def _rule_words(rules: Sequence[dict]) -> str:
    if not rules:
        return "none"
    return "; ".join(f"{r.get('op')} {r.get('role')} {r.get('name')!r}"
                     + (f" to {r['new_name']!r}" if r.get("new_name") else "") for r in rules)


def _run_entry(
    out: Path, job: Job, sha: str | None, step: Callable[[Any], str | None],
    rules: Sequence[dict] = (),
) -> tuple[dict, dict | None, Path | None]:
    """The run of one job as validation.json lists it, with its record and attempt directory
    when it is a current result (state done): one of the test ``sha``, with ``rules``."""
    job_dir = out / job.path
    attempts = sorted(
        (p for p in job_dir.glob("attempt-*") if _ATTEMPT.fullmatch(p.name)),
        key=_attempt_number,
    ) if job_dir.is_dir() else []
    states = [(_read_json(a / RUN_RECORD) or {}).get("state") for a in attempts]
    entry: dict[str, Any] = {
        "rep": job.rep, "state": "pending", "detail": "not run yet",
        "status": None, "reason": None, "lineno": None, "exc_type": None, "call": None,
        "step": None, "oracle": None, "start_s": None, "test_s": None, "oracle_s": None,
        "wall_s": None, "slot": None, "started_at": None,
        "attempts": len(attempts), "env_failures": states.count("env_failed"), "dir": None,
    }
    queued = _read_json(job_dir / "job.json")
    state = queued.get("state") if queued else None
    if state in ("env_failed", "error"):
        entry.update(state=state, detail=_first_line(queued.get("detail")))
        return entry, None, None
    if state != "done":
        return entry, None, None
    name = str(queued.get("attempt_dir"))
    attempt = job_dir / name
    record = _read_json(attempt / RUN_RECORD) if _ATTEMPT.fullmatch(name) else None
    entry["dir"] = _relative(attempt, out)
    if record is None or record.get("state") != "done":
        entry.update(state="error", detail=f"the record of {name} is missing or not done")
        return entry, None, None
    result = record.get("result")
    failure = _failure(result, job.task)
    seconds = record.get("seconds") if isinstance(record.get("seconds"), dict) else {}
    oracle = record.get("oracle")
    entry.update(
        state="done",
        detail=None,
        status=record.get("status"),
        reason=result.get("reason") if isinstance(result, dict) else None,
        page_errors=_page_errors(result),
        lineno=failure.get("lineno"),
        exc_type=failure.get("exc_type"),
        call=failure.get("call"),
        step=step(failure.get("lineno")),
        oracle={k: oracle.get(k) for k in _ORACLE_FIELDS} if isinstance(oracle, dict) else None,
        start_s=_number(seconds.get("start")),
        test_s=_number(seconds.get("test")),
        oracle_s=_number(seconds.get("oracle")),
        wall_s=_number(record.get("wall_s")),
        slot=record.get("slot"),
        started_at=record.get("started_at"),
    )
    ran = (record.get("test") or {}).get("sha256") if isinstance(record.get("test"), dict) else None
    if sha is None or ran != sha:
        entry.update(state="stale", detail=f"ran another version of the test "
                                           f"({str(ran)[:12]}, now {str(sha)[:12]})")
        return entry, None, None
    runner, now = record.get("runner"), runner_source_digest()
    if runner != now:
        entry.update(state="stale", detail=f"ran with another runner "
                                           f"({str(runner)[:12]}, now {now[:12]})")
        return entry, None, None
    environment, now = record.get("environment"), environment_digest()
    if environment != now:
        entry.update(state="stale", detail=f"ran in another environment "
                                           f"({str(environment)[:12]}, now {now[:12]})")
        return entry, None, None
    ran_rules = record.get("rules", [])
    if ran_rules != list(rules):
        entry.update(state="stale", detail=f"ran with another rule ({_rule_words(ran_rules)}; "
                                           f"now {_rule_words(rules)})")
        return entry, None, None
    if ran_rules:  # a run without a rule does not use the code that makes them
        code, now = record.get("rules_source"), rules_source_digest()
        if code != now:
            entry.update(state="stale", detail=f"ran with another {RULES_SOURCE} "
                                               f"({str(code)[:12]}, now {now[:12]})")
            return entry, None, None
    return entry, record, attempt


# --- the verdict


def _runs_words(reps: Sequence[int]) -> str:
    numbers = [str(r) for r in reps]
    if len(numbers) == 1:
        return f"run {numbers[0]}"
    return f"runs {', '.join(numbers[:-1])} and {numbers[-1]}"


def _place(lineno: Any, step: Any, exc_type: Any, call: Any) -> str:
    where = f"line {lineno}" + (f" ({step})" if step else "")
    what = " ".join(str(part) for part in (exc_type, call) if part)
    return f"{where}: {what}" if what else where


def _outcome_words(run: dict) -> str:
    status = str(run["status"])
    if run["lineno"] is not None:
        return f"{status} at {_place(run['lineno'], run['step'], run['exc_type'], run['call'])}"
    return f"{status} ({run['reason']})" if run["reason"] else status


def _url_path(url: Any) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    try:
        return urlsplit(url).path
    except ValueError:
        return None


def _oracle_place(oracle: dict) -> str:
    what = " ".join(str(p) for p in (oracle.get("exc_type"), oracle.get("call")) if p)
    return (f"{oracle.get('status')} at statement {oracle.get('stmt_index')} on "
            f"{_url_path(oracle.get('url'))}: {what}")


def _missing_words(check: Check, run: dict, key: str) -> str:
    """Why a run of ``check`` has no result, and what to do about it."""
    code, rep, state, detail = check.label, run["rep"], run["state"], run["detail"]
    if state == "env_failed":
        return (f"{code} run {rep} has no result: the environment failed in all "
                f"{run['attempts']} attempts (last: {detail}); run the same command again")
    if state == "error":
        return (f"{code} run {rep} has no result: {detail}; remove "
                f"{STAGE}/{key}/{check.name}/{rep} to run it again")
    if state == "stale":
        return f"{code} run {rep} {detail}; remove {STAGE}/{key} to validate the current test"
    return f"{code} run {rep} has not run yet"


def _assess(
    plan: Plan, runs: Mapping[str, Sequence[dict]], key: str
) -> tuple[str, list[str], dict[str, dict]]:
    """The verdict, the reasons, and per check what its runs share."""
    broken: list[str] = []
    missing: list[str] = []
    shared: dict[str, dict] = {}
    labels = {c.name: c.label for c in plan.checks}
    for check in plan.checks:
        code = check.label
        done = [run for run in runs[check.name] if run["state"] == "done"]
        missing += [_missing_words(check, run, key) for run in runs[check.name]
                    if run["state"] != "done"]
        if check.expect == "pass":
            broken += [f"{code} run {run['rep']} was not green: {_outcome_words(run)}"
                       for run in done if run["status"] != "pass"]
            as_expected = sum(run["status"] == "pass" for run in done)
            signature = None
        else:
            signature, more = _red_signature(code, done)
            broken += more
            as_expected = sum(run["status"] == "fail" for run in done)
        oracle_signature = None
        against: dict[str, Any] = {}
        if check.oracle and check.shadow_of is None:
            oracle_signature, more = _oracle_signature(code, done)
            broken += more
        elif check.oracle:
            shadow, more = _shadow_agreement(code, done, runs[check.shadow_of],
                                             labels[check.shadow_of])
            broken += more
            against = {"shadow": shadow, "shadow_of": labels[check.shadow_of]}
        if check.variant.rule is not None:
            # A control renamed or removed under a framework that still holds it can throw
            # and blank the app: then the variant is not "the control is renamed / missing".
            broken += [f"{code} run {run['rep']}: the page threw {run['page_errors']} error(s) "
                       f"after the control was changed" for run in done
                       if (run.get("page_errors") or 0) > 0]
        shared[check.name] = {"as_expected": as_expected, "signature": signature,
                              "oracle_signature": oracle_signature, **against}
    verdict = "excluded" if broken else "incomplete" if missing else "admitted"
    return verdict, broken + missing, shared


def _page_errors(result: Any) -> int:
    """How many errors the page's own script threw during the run (0 when not reported)."""
    errors = result.get("page_errors") if isinstance(result, dict) else None
    count = errors.get("count") if isinstance(errors, dict) else None
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def _red_signature(code: str, done: Sequence[dict]) -> tuple[dict | None, list[str]]:
    reasons: list[str] = []
    places: dict[tuple, list[int]] = {}
    steps: dict[tuple, Any] = {}
    for run in done:
        if run["status"] != "fail":
            reasons.append(f"{code} run {run['rep']} was not red: {_outcome_words(run)}")
        elif isinstance(run["lineno"], bool) or not isinstance(run["lineno"], int) or not (
            isinstance(run["exc_type"], str) and run["exc_type"]
        ):
            reasons.append(f"{code} run {run['rep']} failed without a recorded line and "
                           f"exception type")
        else:
            place = (run["lineno"], run["exc_type"], run["call"])
            places.setdefault(place, []).append(run["rep"])
            steps[place] = run["step"]
    if len(places) > 1:
        reasons.append(f"{code} did not fail at the same place every time: " + "; ".join(
            f"{_runs_words(reps)} at {_place(place[0], steps[place], place[1], place[2])}"
            for place, reps in places.items()))
    if len(places) != 1:
        return None, reasons
    [(lineno, exc_type, call)] = list(places)
    step = steps[(lineno, exc_type, call)]
    return {"step": step, "lineno": lineno, "exc_type": exc_type, "call": call}, reasons


def _oracle_signature(code: str, done: Sequence[dict]) -> tuple[dict | None, list[str]]:
    reasons: list[str] = []
    failures: list[tuple[int, dict]] = []
    for run in done:
        oracle = run["oracle"]
        if not isinstance(oracle, dict):
            reasons.append(f"the shadow oracle gave no result after {code} run {run['rep']}")
            continue
        if oracle.get("status") == "pass":
            reasons.append(f"the shadow oracle passed after {code} run {run['rep']}: the final "
                           f"assertion held")
            continue
        try:
            classify_shadow(oracle, oracle)  # refuses a result that cannot serve as a baseline
        except (ValueError, EnvFailure) as exc:
            reasons.append(f"the shadow oracle after {code} run {run['rep']} is not a usable "
                           f"failure: {exc}")
            continue
        failures.append((run["rep"], oracle))
    if not failures:
        return None, reasons
    first = failures[0][1]
    if not all(classify_shadow(oracle, first) == "bug_seen" for _, oracle in failures):
        groups: dict[str, list[int]] = {}
        for rep, oracle in failures:
            groups.setdefault(_oracle_place(oracle), []).append(rep)
        reasons.append("the shadow oracle did not fail the same way every time: " + "; ".join(
            f"{_runs_words(reps)}: {place}" for place, reps in groups.items()))
        return None, reasons
    return {"status": first["status"], "stmt_index": first["stmt_index"],
            "exc_type": first["exc_type"], "call": first["call"],
            "url_path": _url_path(first["url"])}, reasons


def _shadow_agreement(
    code: str, done: Sequence[dict], reference_runs: Sequence[dict], reference_label: str
) -> tuple[str | None, list[str]]:
    """The shadow every run of a check shares, classified against the first oracle result of
    another check (``Check.shadow_of``), or None and why not. Without a usable first result of
    that check there is nothing to classify against yet; its own rule says why."""
    first = reference_runs[0] if reference_runs else None
    if first is None or first["state"] != "done" or not isinstance(first["oracle"], dict):
        return None, []
    reference = first["oracle"]
    try:
        classify_shadow(reference, reference)
    except (ValueError, EnvFailure):
        return None, []
    reasons: list[str] = []
    found: dict[str, list[int]] = {}
    for run in done:
        if not isinstance(run["oracle"], dict):
            reasons.append(f"the shadow oracle gave no result after {code} run {run['rep']}")
            continue
        try:
            shadow = classify_shadow(run["oracle"], reference)
        except (ValueError, EnvFailure) as exc:
            reasons.append(f"the shadow oracle after {code} run {run['rep']} is not usable: {exc}")
            continue
        found.setdefault(str(shadow), []).append(run["rep"])
    if len(found) > 1:
        reasons.append(
            f"the shadow oracle after {code} did not come out the same every time against "
            f"{reference_label} run 1: "
            + "; ".join(f"{_runs_words(reps)}: {shadow}" for shadow, reps in found.items()))
    if len(found) != 1 or reasons:
        return None, reasons
    return next(iter(found)), reasons


# --- the reports


def _display(path: Path | None) -> str | None:
    """A path as reports show it: relative to the repository when inside it, else its name."""
    if path is None:
        return None
    try:
        return Path(path).resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return Path(path).name


def _scenario_report(
    config: ValidationConfig, out: Path, scenario: tuple[str, str, str], plan: Plan
) -> tuple[dict, tuple[dict, Path] | None]:
    family, app, task = scenario
    key = f"{family}/{app}/{task}"
    test = config.tests_root / app / f"test_{task}.py"
    data = test.read_bytes() if test.is_file() else None
    sha = hashlib.sha256(data).hexdigest() if data is not None else None
    text = data.decode("utf-8", errors="replace") if data is not None else None
    try:
        case: BenchCase | None = load_case(app, task)
    except Exception:  # noqa: BLE001 - without the benchmark no step is named
        case = None

    def step(lineno: Any) -> str | None:
        # A reference fix renames strings only: its lines are the generated test's.
        return step_of(text, case, lineno) if text is not None and case is not None else None

    fix = None
    if any(c.test == "reference_fix" for c in plan.checks):
        fix = fix_path(config.reference_fixes_root, app, task)
    fix_data = fix.read_bytes() if fix is not None and fix.is_file() else None
    fix_sha = hashlib.sha256(fix_data).hexdigest() if fix_data is not None else None
    target = config.ruleset.target(app, task) if config.ruleset is not None else None

    runs: dict[str, list[dict]] = {}
    records: dict[str, list[dict | None]] = {}
    attempts: dict[str, list[Path | None]] = {}
    for check in plan.checks:
        expected = fix_sha if check.test == "reference_fix" else sha
        rules = _rules_of(config.ruleset, check.variant)
        for rep in range(1, config.repeats + 1):
            entry, record, attempt = _run_entry(out, Job(STAGE, family, app, task, check.name,
                                                          rep), expected, step, rules)
            runs.setdefault(check.name, []).append(entry)
            records.setdefault(check.name, []).append(record)
            attempts.setdefault(check.name, []).append(attempt)
    verdict, reasons, shared = _assess(plan, runs, key)
    variants = {}
    for check in plan.checks:
        variants[check.label] = {
            "check": check.name, "expect": check.expect, "oracle": check.oracle,
            **shared[check.name], "runs": runs[check.name],
        }
    a_not_green = sum(
        run["status"] != "pass"
        for check in plan.checks
        if check.variant.code == "A" and check.expect == "pass" and check.test == "generated"
        for run in runs[check.name] if run["state"] == "done"
    )
    entry = {
        "family": family, "app": app, "task": task,
        "test": {"path": _relative(test, REPO_ROOT), "sha256": sha},
        **({"reference_fix": {"path": _relative(fix, REPO_ROOT), "sha256": fix_sha}}
           if fix is not None else {}),
        **({"target": asdict(target)} if family in RULE_FAMILIES and target is not None
           else {}),
        **({"rules_source": {"path": RULES_SOURCE, "sha256": rules_source_digest()}}
           if family in RULE_FAMILIES else {}),
        "verdict": verdict,
        "reasons": reasons,
        "a_not_green": a_not_green,
        "env_failures": sum(run["env_failures"] for check_runs in runs.values()
                            for run in check_runs),
        "variants": variants,
    }
    admitted = None
    if verdict == "admitted":
        first_record = records[plan.initial][0]
        first_attempt = attempts[plan.initial][0]
        assert first_record is not None and first_attempt is not None  # admitted: every run done
        admitted = (plan.baseline(records), first_attempt / first_record["results"])
    return entry, admitted


def build_report(
    config: ValidationConfig, plans: Mapping[tuple[str, str, str], Plan], out: Path
) -> tuple[dict, dict[str, tuple[dict, Path]]]:
    """validation.json's content, and per admitted scenario its baseline entry and the
    test-results directory sessions start from."""
    scenarios: dict[str, dict] = {}
    admitted: dict[str, tuple[dict, Path]] = {}
    for scenario, plan in plans.items():
        key = "/".join(scenario)
        scenarios[key], payload = _scenario_report(config, out, scenario, plan)
        if payload is not None:
            admitted[key] = payload
    counts = {"scenarios": len(scenarios)}
    counts.update({v: sum(s["verdict"] == v for s in scenarios.values()) for v in VERDICTS})
    a_runs = sum(
        1 for s in scenarios.values() for code, v in s["variants"].items()
        if code == "A" and v["expect"] == "pass"
        for run in v["runs"] if run["state"] == "done"
    )
    not_green = sum(s["a_not_green"] for s in scenarios.values())
    report = {
        "schema": SCHEMA,
        "written_at": _now(),
        "config": _display(config.source),
        "repeats": config.repeats,
        "slots": config.slots,
        "backend": config.backend["factory"],
        "rule": list(RULE),
        "counts": counts,
        "baseline_flake_rate": {
            "a_runs": a_runs,
            "not_green": not_green,
            "rate": not_green / a_runs if a_runs else None,
            "scenarios_affected": sum(s["a_not_green"] > 0 for s in scenarios.values()),
        },
        "env_failures": sum(s["env_failures"] for s in scenarios.values()),
        "scenarios": scenarios,
    }
    return report, admitted


def _regular_files(root: Path) -> dict[str, bytes]:
    """Regular files under ``root`` by relative path; links are never followed or copied."""
    found: dict[str, bytes] = {}
    if root.is_symlink() or not root.is_dir():
        return found
    for directory, _dirs, names in os.walk(root):  # does not descend into linked directories
        for name in names:
            path = Path(directory) / name
            if path.is_file() and not path.is_symlink():
                found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


def _put_initial(root: Path, wanted: Mapping[str, Path]) -> None:
    """Make ``root/<family>/<app>/<task>/`` hold the files of ``wanted[key]`` for every admitted
    scenario. A directory that already holds the same files is left alone, so a stage reading it
    meanwhile never sees it change; one that must change is replaced whole."""
    root.mkdir(parents=True, exist_ok=True)
    for key, source in wanted.items():
        target = root / key
        files = _regular_files(source)
        if target.is_dir() and not target.is_symlink() and _regular_files(target) == files:
            continue
        staging = target.with_name(f".{target.name}.new")
        shutil.rmtree(staging, ignore_errors=True)
        for name, data in files.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        staging.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            old = target.with_name(f".{target.name}.old")
            shutil.rmtree(old, ignore_errors=True)
            os.replace(target, old)
            os.replace(staging, target)
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.replace(staging, target)


def _prune_initial(root: Path, wanted: Mapping[str, Path]) -> None:
    """Remove every scenario directory under ``root`` that ``wanted`` does not name, and the
    directories left empty."""
    if not root.is_dir():
        return
    for directory in sorted(root.glob("*/*/*"), reverse=True):
        key = directory.relative_to(root).as_posix()
        if directory.is_dir() and not directory.is_symlink() and key not in wanted:
            shutil.rmtree(directory)
    for level in ("*/*", "*"):
        for directory in root.glob(level):
            if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
                directory.rmdir()


def _write_text(path: Path, text: str) -> None:
    partial = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)


def _bound(scenario: dict) -> dict:
    """What a stage checks a baseline entry against: the test, and for the families with rules
    the reference fix (C-rename, C-assert, Mixed), the target and the src/hoh/rules.py the runs
    were made
    with."""
    bound = {"test_sha256": scenario["test"]["sha256"]}
    if "reference_fix" in scenario:
        bound["reference_fix_sha256"] = scenario["reference_fix"]["sha256"]
    if "target" in scenario:
        bound["target"] = scenario["target"]
    if "rules_source" in scenario:
        bound["rules_source_sha256"] = scenario["rules_source"]["sha256"]
    return bound


def write_reports(
    config: ValidationConfig, plans: Mapping[tuple[str, str, str], Plan], out: Path
) -> dict:
    """Write validation.json, baseline.json, initial/ and summary.md from what is on disk."""
    report, admitted = build_report(config, plans, out)
    wanted = {key: results for key, (_, results) in admitted.items()}
    # A scenario's initial test-results exist before baseline.json names it, and until after
    # it no longer does: a stage that reads both never finds one without the other.
    _put_initial(out / "initial", wanted)
    baseline = {
        "schema": BASELINE_SCHEMA,
        "about": (
            f"Written by hoh.validate at {report['written_at']}: the admitted scenarios of "
            f"validation.json ({config.repeats} runs per variant), in the format hoh.worker "
            f"reads. Keys are <family>/<app>/<task>."
        ),
        # The runner that measured these runs, and the test each scenario ran: a stage refuses
        # a baseline that another runner or another version of a test would not reproduce.
        "runner": runner_source_digest(),
        "environment": environment_digest(),
        "scenarios": {
            key: {**entry, **_bound(report["scenarios"][key])}
            for key, (entry, _) in admitted.items()
        },
    }
    atomic_write_json(out / "baseline.json", baseline)
    _prune_initial(out / "initial", wanted)
    atomic_write_json(out / "validation.json", report)
    _write_text(out / "summary.md", render_summary(report, plans))
    return report


# --- summary.md


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _median(values: Sequence[float | None]) -> str:
    numbers = [v for v in values if v is not None]
    return f"{statistics.median(numbers):.1f}" if numbers else "–"


def _count_cell(variant: dict, repeats: int) -> str:
    missing = sum(run["state"] != "done" for run in variant["runs"])
    text = f"{variant['as_expected']}/{repeats}"
    return f"{text} ({missing} missing)" if missing else text


def _signature_cell(variant: dict) -> str:
    signature = variant["signature"]
    if signature:
        where = f"{signature['step']}, " if signature["step"] else ""
        call = f" `{signature['call']}`" if signature["call"] else ""
        return f"{where}line {signature['lineno']}: {signature['exc_type']}{call}"
    red = [run for run in variant["runs"] if run["state"] == "done" and run["status"] == "fail"]
    return "differs" if red else "–"


def _oracle_cell(variant: dict) -> str:
    if "shadow_of" in variant:
        seen = [run for run in variant["runs"] if run["state"] == "done" and run["oracle"]]
        if variant["shadow"]:
            return f"{variant['shadow']} against {variant['shadow_of']}"
        return "differs" if seen else "–"
    signature = variant["oracle_signature"]
    if signature:
        call = f" `{signature['call']}`" if signature["call"] else ""
        return (f"statement {signature['stmt_index']} on `{signature['url_path']}`: "
                f"{signature['exc_type']}{call}")
    seen = [run for run in variant["runs"] if run["state"] == "done" and run["oracle"]]
    passed = [run["rep"] for run in seen if run["oracle"].get("status") == "pass"]
    if passed:
        return f"passed after {_runs_words(passed)}"
    return "differs" if seen else "–"


def render_summary(report: dict, plans: Mapping[tuple[str, str, str], Plan]) -> str:
    """The Markdown summary: what was run, the totals, a table per family, the reasons."""
    counts, flakes = report["counts"], report["baseline_flake_rate"]
    source = f" from `{report['config']}`" if report["config"] else ""
    lines = [
        "# Validation run",
        "",
        f"Written by `python -m hoh.validate` on "
        f"{report['written_at'].replace('T', ' ').replace('Z', ' UTC')}{source}. Each test "
        f"ran {report['repeats']} times on every variant, in up to {report['slots']} parallel "
        f"slots, on `{report['backend']}`. The admission rule is design doc 5.10 as "
        f"`hoh.validate` states it.",
        "",
        f"- Admitted: {counts['admitted']} of {counts['scenarios']}. Excluded: "
        f"{counts['excluded']}. Incomplete: {counts['incomplete']}.",
    ]
    if flakes["a_runs"]:
        lines.append(
            f"- Baseline flake rate of T on A: {flakes['not_green']} of {flakes['a_runs']} runs "
            f"not green ({100 * flakes['rate']:.1f}%), in {flakes['scenarios_affected']} "
            f"scenario(s)."
        )
    else:
        lines.append("- Baseline flake rate of T on A: no run on A yet.")
    lines.append(f"- Environment failures: {report['env_failures']} attempt(s). None is counted "
                 f"as a run; the run is tried again, up to {MAX_ENV_RETRIES + 1} attempts per "
                 f"command.")
    by_family: dict[str, list[tuple[str, dict, Plan]]] = {}
    for scenario, plan in plans.items():
        key = "/".join(scenario)
        by_family.setdefault(scenario[0], []).append((key, report["scenarios"][key], plan))
    for family, entries in by_family.items():
        checks = entries[0][2].checks
        fails = [c for c in checks if c.expect == "fail"]
        oracles = [c for c in checks if c.oracle]
        header = ["Scenario"]
        header += [f"{c.label} {'green' if c.expect == 'pass' else 'red'}" for c in checks]
        header += ["Red signature" if len(fails) == 1 else f"{c.label} red signature"
                   for c in fails]
        header += ["Shadow oracle" if len(oracles) == 1 else f"{c.label} shadow oracle"
                   for c in oracles]
        header += ["Verdict", "Start s", f"Test s ({' / '.join(c.label for c in checks)})"]
        lines += ["", f"## {family}", "", "| " + " | ".join(header) + " |",
                  "|" + "---|" * len(header)]
        for _key, entry, plan in entries:
            variants = [entry["variants"][c.label] for c in plan.checks]
            done = [run for v in variants for run in v["runs"] if run["state"] == "done"]
            row = [f"{entry['app']}/{entry['task']}"]
            row += [_count_cell(v, report["repeats"]) for v in variants]
            row += [_signature_cell(entry["variants"][c.label]) for c in fails]
            row += [_oracle_cell(entry["variants"][c.label]) for c in oracles]
            row += [entry["verdict"], _median([run["start_s"] for run in done]),
                    " / ".join(_median([run["test_s"] for run in v["runs"]
                                        if run["state"] == "done"]) for v in variants)]
            lines.append("| " + " | ".join(_cell(c) for c in row) + " |")
        others = [(key, entry) for key, entry, _ in entries if entry["verdict"] != "admitted"]
        if others:
            lines += ["", "### Not admitted", ""]
            for _key, entry in others:
                lines.append(f"- {entry['app']}/{entry['task']} ({entry['verdict']}): "
                             + "; ".join(entry["reasons"]) + ".")
    return "\n".join(lines) + "\n"


# --- the command line


def _progress(summary: dict) -> None:
    counts = summary["counts"]
    finished = counts["done"] + counts["error"] + counts["env_failed"]
    print(f"validate: {finished} of {summary['total']} runs finished (without a result: "
          f"{counts['error'] + counts['env_failed']}; environment retries: "
          f"{summary['env_retries']})", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hoh.validate", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, type=Path, help="the validation's YAML file")
    parser.add_argument("--only", nargs="+", default=[], metavar="APP/TASK",
                        help="run only these scenarios (the reports still cover all)")
    parser.add_argument("--repeats", type=int, help="runs per variant (overrides the file)")
    parser.add_argument("--slots", type=int, help="runs at once (overrides the file)")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.repeats is not None:
            config = replace(config, repeats=_positive(args.repeats, "--repeats"))
        if args.slots is not None:
            config = replace(config, slots=_positive(args.slots, "--slots"))
    except (OSError, ValueError) as exc:
        print(f"validate: {args.config}: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        report = run_validation(config, only=args.only, on_progress=_progress)
    except (ValidationRefused, ValidationBusy) as exc:
        for line in str(exc).splitlines():
            print(f"validate: {line}", file=sys.stderr)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        print("validate: stopped. The reports show what has finished; run the same command "
              "again to continue.", file=sys.stderr)
        return EXIT_STOPPED
    counts = report["counts"]
    chosen = {"/".join(s) for s in _chosen(config.scenarios(), args.only)}
    lacking = sum(
        run["state"] != "done"
        for key in chosen
        for variant in report["scenarios"][key]["variants"].values()
        for run in variant["runs"]
    )
    print(f"validate: admitted {counts['admitted']}, excluded {counts['excluded']}, "
          f"incomplete {counts['incomplete']} (of {counts['scenarios']} scenarios); runs of this "
          f"command without a result: {lacking}")
    print(f"validate: see validation.json, summary.md, baseline.json and initial/ in "
          f"{config.output_dir}")
    return EXIT_MISSING if lacking else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
