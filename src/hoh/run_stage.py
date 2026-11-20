"""The stage runner: read a stage configuration, run its jobs through the queue, summarize.

    python -m hoh.run_stage --stage 1a --config stages/1a.yaml [--dry-run] [--rejudge]
                            [--retry-errors]

``--stage`` must name the configuration's stage. The configuration format is described in
``hoh.worker`` and shown in full in ``stages/dry-run.yaml``.

A run checks the stage's inputs first (``preflight``): every scenario's test, initial
test-results and baseline entry, every reference fix, and, for a real run, the CLI binary, a slot
root outside every repository and an importable backend. Then it runs the jobs in the queue's
order with the session worker (``hoh.worker``). The queue skips jobs that are already ``done``
or ``error``, so the same command resumes a stage. Every attempt leaves a ``job_record.json``;
``stage_summary.json`` in the output directory indexes them.

``--retry-errors`` runs again the jobs that ended in an error (a fault of the harness), after the
cause is fixed; an error is not retried on its own, since it may repeat and each try can cost a
session. A session that a model switch or refusal ended stays as it is: the analysis plan
decides about those.

``--rejudge`` makes no session. It judges again, from the saved workspace, every job whose
record says ``judge_failed``, while no queue runs on the same directory.

``--dry-run`` runs the same pipeline without Docker or a credential: the agent CLI is the
stand-in ``tests/support/fake_claude.py`` (arms with an appended system prompt add a wait to the
test and run it again; the others leave it as it is), every backend is a
``hoh.fake_backend.FakeBackend`` with scripted results, the credential is made up and the slots
live in a temporary directory. It writes under ``<output_root>/dry-run/``, never among the
results of a real run. A recorded stage must still name a readable reference init; the dry run
checks that and then runs without it, since the stand-in's init is not the calibration init.

Exit codes: 0 the queue ran to its end; 1 ``--rejudge`` left jobs unjudged; 2 the configuration
or the inputs are not usable (nothing ran); 3 the stage stopped because the credential was
refused (fix it and run the same command again).

``stage_summary.json``::

    schema, stage, dry_run, written_at
    jobs         {total, done, error, env_failed, pending}: the queue's record of each job
    attempts     {record state: count} over every attempt of every job
    labels       {total, by_arm, by_app}: label counts of the judged jobs (the counting attempt)
    judge_failed {total, by_arm, by_app}: jobs whose judging still has to be made
    discards     {total, by_class, by_arm}: discarded attempts (sessions that were not data)
    endings      {total, by_arm}: how the sessions of done jobs ended (completed, censored)
    spend        {usd, sessions, by_credential}: what every session cost, data or not (the
                 CLI's list-price estimate; under an API key, what the credit pays)
    cost         {by_arm: {arm: {sessions, usd, mean_usd, mean_turns}}}: the counting session
                 of each done job
    index        one entry per job: job_id, family, app, task, arm, rep, queue_state, attempt,
                 record and judgment (paths relative to the output directory), state, label
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from hoh.backend import STATUSES
from hoh.benchmark import REPO_ROOT
from hoh.docker_backend import runner_image_problem, runner_source_digest
from hoh.envs import environment_digest
from hoh.fake_backend import FakeBackend
from hoh.jobs import FORBIDDEN_WORDS, Job
from hoh.judge import cells, classify_shadow
from hoh.mechanism import oracle_edited
from hoh.queue import LOCK_NAME, atomic_write_json, disk_problem, run_queue
from hoh.rules import RULE_FAMILIES, RULES_SOURCE, reference_fix, rules_source_digest
from hoh.session import (
    API_KEY_SOURCE,
    BUILTIN_TOOLS,
    PROJECT_MARKERS,
    RUN_TEST_TOOL,
    SERVER_KEY,
    Credential,
    CredentialError,
    end_live_sessions,
    load_credential,
)
from hoh.worker import (
    RECORD_NAME,
    BackendFactory,
    SlotPool,
    SlotsBusy,
    StageConfig,
    Worker,
    default_backend_factory,
    evaluate,
    load_stage_config,
    make_worker,
    read_records,
)

SUMMARY_NAME = "stage_summary.json"
DOCKER_FACTORY = "hoh.docker_backend:DockerBackend"
DRY_RUN_DIR = "dry-run"
STAND_IN = REPO_ROOT / "tests" / "support" / "fake_claude.py"
EXIT_OK, EXIT_UNJUDGED, EXIT_CONFIG, EXIT_STOPPED = 0, 1, 2, 3
EXIT_INTERRUPTED = 130
SUMMARY_EVERY_S = 60.0


NO_REFERENCE_INIT = (
    "a recorded stage needs reference_init, the init event of a calibration session checked by "
    "eye (docs/runbooks/gate0-session.md)"
)


class StageStopped(RuntimeError):
    """The worker refuses to start further sessions; the queue is ended instead of paused."""


def _on_interrupt(signum: int, frame: Any) -> None:
    """Ctrl-C: end the agent sessions first (each runs in a process group of its own, which the
    terminal's signal does not reach), then stop as Python would."""
    ended = end_live_sessions()
    print(f"run_stage: Ctrl-C: ended {ended} running session(s)", file=sys.stderr)
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hoh.run_stage", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", required=True, help="the stage name in the configuration")
    parser.add_argument("--config", required=True, type=Path, help="the stage's YAML file")
    parser.add_argument("--dry-run", action="store_true",
                        help="stand-in CLI, scripted backend, made-up credential")
    parser.add_argument("--rejudge", action="store_true",
                        help="judge every judge_failed job again; run no session")
    parser.add_argument("--retry-errors", action="store_true",
                        help="run again the jobs that ended in an error (not a model switch)")
    args = parser.parse_args(argv)
    try:
        config = load_stage_config(args.config)
    except (OSError, ValueError) as exc:
        return _refuse([f"{args.config}: {exc}"])
    if config.stage != args.stage:
        return _refuse([f"--stage {args.stage} does not match the configuration's stage "
                        f"{config.stage!r}"])
    jobs = config.jobs()
    if args.dry_run:
        problem = _reference_init_problem(config)
        if problem:
            return _refuse([problem])
        with dry_run(config) as dry:
            return _start(dry.config, jobs, dry.backend_factory, dry.credential,
                          config.output_root / DRY_RUN_DIR, dry_run=True, rejudging=args.rejudge,
                          retry_errors=args.retry_errors)
    return _start(config, jobs, default_backend_factory(config), None, config.output_root,
                  dry_run=False, rejudging=args.rejudge, retry_errors=args.retry_errors)


def _reference_init_problem(config: StageConfig) -> str | None:
    """Why a recorded stage's reference init cannot be used, or None.

    Checked before a dry run, which then runs without it: the stand-in's init is not the
    calibration init, so the two cannot be compared there.
    """
    if not config.recorded:
        return None
    if config.reference_init is None:
        return NO_REFERENCE_INIT
    try:
        init = json.loads(config.reference_init.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"the reference init {config.reference_init} cannot be read: {exc}"
    if not isinstance(init, dict):
        return f"the reference init {config.reference_init} must hold one init event"
    return None


def _refuse(problems: Sequence[str]) -> int:
    for problem in problems:
        print(f"run_stage: {problem}", file=sys.stderr)
    return EXIT_CONFIG


def _start(
    config: StageConfig,
    jobs: list[Job],
    backend_factory: BackendFactory,
    credential: Credential | None,
    out_dir: Path,
    *,
    dry_run: bool,
    rejudging: bool,
    retry_errors: bool = False,
) -> int:
    problems = preflight(config, jobs, dry_run=dry_run)
    if problems:
        return _refuse(problems)
    pool = SlotPool(config.slot_root, config.slots)
    try:
        pool.claim()
    except SlotsBusy as exc:
        return _refuse([str(exc)])
    try:
        if rejudging:
            results = rejudge(config, out_dir, backend_factory, pool, jobs)
            write_stage_summary(config, jobs, out_dir, dry_run=dry_run)
            left = sorted(job_id for job_id, state in results.items() if state != "judged")
            print(f"run_stage: judged again: {len(results) - len(left)}; still judge_failed: "
                  f"{len(left)}")
            return EXIT_UNJUDGED if left else EXIT_OK
        if credential is None:
            try:
                credential = load_credential(config.credential_kind, config.env_file)
            except CredentialError as exc:
                return _refuse([str(exc)])
        worker = make_worker(config, backend_factory, pool, credential=credential,
                             dry_run=dry_run)
        return run_jobs(config, jobs, worker, out_dir, dry_run=dry_run,
                        retry_errors=retry_errors)
    finally:
        pool.free()


def errors_to_retry(jobs: Sequence[Job], out_dir: Path) -> tuple[list[Job], list[Job]]:
    """The jobs whose record says error that may run again, and those that may not: a session
    the model switch or a refusal ended (``model_changed``) is the analysis plan's to decide."""
    again, kept = [], []
    for job in jobs:
        queued = _read_json(Path(out_dir) / job.path / "job.json")
        if not queued or queued.get("state") != "error":
            continue
        attempt = queued.get("attempt_dir")
        record = (_read_json(Path(out_dir) / job.path / attempt / RECORD_NAME)
                  if isinstance(attempt, str) else None) or {}
        discard = record.get("discard") or {}
        (kept if discard.get("class") == "model_changed" else again).append(job)
    return again, kept


def run_jobs(
    config: StageConfig,
    jobs: list[Job],
    worker: Worker,
    out_dir: Path,
    *,
    dry_run: bool = False,
    retry_errors: bool = False,
) -> int:
    """Run the jobs through the queue with ``worker``; write the stage summary as they finish.

    When the worker refuses to start sessions (``Worker.stop_reason``), the queue pauses; the
    pause ends it here instead of waiting. The jobs it held stay unrecorded, so running the same
    command again (after fixing the cause) runs them. With ``retry_errors`` the jobs that ended
    in an error run again (``errors_to_retry``).
    """
    out_dir = Path(out_dir)
    retry: list[Job] = []
    if retry_errors:
        retry, kept = errors_to_retry(jobs, out_dir)
        print(f"run_stage: running again {len(retry)} job(s) that ended in an error; leaving "
              f"{len(kept)} that a model switch or refusal ended", file=sys.stderr)
    last_written = [float("-inf")]

    def on_summary(_queue_summary: dict) -> None:
        if time.monotonic() - last_written[0] >= SUMMARY_EVERY_S:
            last_written[0] = time.monotonic()
            write_stage_summary(config, jobs, out_dir, dry_run=dry_run)

    def sleep(seconds: float) -> None:
        reason = worker.stop_reason
        if reason is not None:
            raise StageStopped(reason)
        time.sleep(seconds)

    written: dict[str, Any] = {}
    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGINT, _on_interrupt)
    try:
        summary = run_queue(jobs, worker, out_dir, slots=config.slots, sleep=sleep,
                            on_summary=on_summary, retry=retry)
    except StageStopped as stop:
        print(f"run_stage: stopped: {stop}. Fix the cause and run the same command again; "
              f"finished jobs are skipped.", file=sys.stderr)
        return EXIT_STOPPED
    except KeyboardInterrupt:
        print("run_stage: interrupted. The jobs that were running stay unrecorded: run the same "
              "command again to go on.", file=sys.stderr)
        return EXIT_INTERRUPTED
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        written = write_stage_summary(config, jobs, out_dir, dry_run=dry_run)
        spend = written["spend"]
        print(f"run_stage: {config.stage}: spend ${spend['usd']:.2f} over {spend['sessions']} "
              f"session(s) so far, {spend['by_credential']}", file=sys.stderr)
    counts = summary["counts"]
    print(f"run_stage: {config.stage}: done {counts['done']}, error {counts['error']}, "
          f"env_failed {counts['env_failed']}, pending {counts['pending']}; see "
          f"{out_dir / SUMMARY_NAME}")
    if worker.stop_reason is not None:
        print(f"run_stage: stopped: {worker.stop_reason}", file=sys.stderr)
        return EXIT_STOPPED
    return EXIT_OK


# --- judging again


@contextmanager
def _sole_user(out_dir: Path) -> Iterator[None]:
    """Hold the queue's lock on ``out_dir``: no queue may run while jobs are judged again."""
    with open(out_dir / LOCK_NAME, "a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                f"a queue is running on {out_dir}; judge again when it ends"
            ) from None
        yield


def _counting_attempt(out_dir: Path, job: Job) -> Path | None:
    """The attempt directory the queue's record of a done job names, or None."""
    try:
        queued = json.loads((out_dir / job.path / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(queued, dict) or queued.get("state") != "done":
        return None
    attempt = queued.get("attempt_dir")
    return out_dir / job.path / attempt if isinstance(attempt, str) else None


def rejudge(
    config: StageConfig,
    out_dir: Path,
    backend_factory: BackendFactory,
    pool: SlotPool,
    jobs: Sequence[Job],
) -> dict[str, str]:
    """Judge again every job whose counting attempt is ``judge_failed``; never run a session.

    Returns ``{job_id: new record state}`` for the jobs it judged again.
    """
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return {}
    results: dict[str, str] = {}
    with _sole_user(out_dir):
        for job in jobs:
            attempt = _counting_attempt(out_dir, job)
            if attempt is None:
                continue
            path = attempt / RECORD_NAME
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("state") != "judge_failed":
                continue
            scenario = config.scenario(job.family, job.app, job.task)
            with pool.take() as slot:
                evaluate(job, scenario, attempt, record, backend_factory, slot)
            atomic_write_json(path, record)
            results[job.job_id] = record["state"]
    return results


# --- before anything runs


def preflight(config: StageConfig, jobs: Sequence[Job], *, dry_run: bool) -> list[str]:
    """What keeps the stage from running, as messages; empty when it may start."""
    problems: list[str] = []
    ruled = []  # the scenarios of the families with rules
    for family, app, task in dict.fromkeys((j.family, j.app, j.task) for j in jobs):
        key = f"{family}/{app}/{task}"
        try:
            scenario = config.scenario(family, app, task)
        except (OSError, ValueError) as exc:
            problems.append(f"{key}: {exc}")
            continue
        if family in RULE_FAMILIES:
            ruled.append(scenario)
        if not scenario.test_file.is_file():
            problems.append(f"{key}: the test {scenario.test_file} does not exist")
        else:
            problem = _test_drift(config, scenario) or _fix_drift(config, scenario)
            if problem:
                problems.append(f"{key}: {problem}")
        if not scenario.initial_results.is_dir():
            problems.append(f"{key}: the initial test-results {scenario.initial_results} "
                            f"do not exist")
        else:
            problems += [f"{key}: the initial test-results hold {name!r}, a name the agent "
                         f"must not see" for name in _leaky_names(scenario.initial_results)]
        problems += [f"{key}: {p}" for p in _baseline_problems(scenario.baseline, family, app,
                                                                task)]
        if scenario.reference_fix is not None:
            problem = (_reference_problem(scenario.test_file, scenario.reference_fix)
                       or _named_fix_drift(scenario))
            if problem:
                problems.append(f"{key}: {problem}")
    root = config.slot_root.resolve()
    if any(word in str(root).lower() for word in FORBIDDEN_WORDS):
        problems.append(f"the slot root {root} names a word the agent must not see "
                        f"({', '.join(FORBIDDEN_WORDS)}); the agent is shown its workspace path")
    if config.recorded and config.reference_init is None:
        problems.append(NO_REFERENCE_INIT)
    if dry_run:
        if not STAND_IN.is_file():
            problems.append(f"the stand-in CLI {STAND_IN} is missing")
        return problems
    problem = disk_problem(config.output_root)
    if problem:
        problems.append(problem)
    binary = config.launch.claude_bin
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        problems.append(f"the CLI {binary} does not exist or cannot be run")
    for directory in (root, *root.parents):
        for marker in PROJECT_MARKERS:
            if os.path.lexists(directory / marker):
                problems.append(f"the slot root {config.slot_root} lies under {directory / marker};"
                                f" put it outside every repository and project")
    module, _, attribute = str(config.backend["factory"]).partition(":")
    try:
        getattr(importlib.import_module(module), attribute)
    except (ImportError, AttributeError) as exc:
        problems.append(f"the backend factory {config.backend['factory']} cannot be loaded: {exc}")
    if config.backend["factory"] == DOCKER_FACTORY:
        # The judge compares with what the validation measured: with the same runner code, in
        # an image built from it.
        try:
            measured = config.baseline_runner
        except (OSError, ValueError):
            measured = None  # reported with the scenarios above
        now = runner_source_digest()
        if measured != now:
            problems.append(f"the baseline {config.baseline} was measured with another runner "
                            f"({str(measured)[:12]}, now {now[:12]}); validate again")
        problem = runner_image_problem()
        if problem:
            problems.append(problem)
        try:
            measured_in = config.baseline_environment
        except (OSError, ValueError):
            measured_in = None
        now = environment_digest()
        if measured_in != now:
            problems.append(f"the baseline {config.baseline} was measured in another environment "
                            f"({str(measured_in)[:12]}, now {now[:12]}); validate again")
        # And, for a family with rules, with the code that made its page rules, its oracle
        # check and its reference fix.
        now = rules_source_digest()
        for scenario in ruled:
            if scenario.rules_source_sha256 != now:
                problems.append(f"{scenario.key}: the baseline was measured with another "
                                f"{RULES_SOURCE} ({str(scenario.rules_source_sha256)[:12]}, now "
                                f"{now[:12]}); validate again")
    return problems


def _test_drift(config: StageConfig, scenario: Any) -> str | None:
    """Why the test handed out is not the one the validation ran, or None when it is."""
    measured = scenario.test_sha256
    if measured is None:  # a baseline written before it named its tests: ask validation.json
        report = _read_json(config.baseline.with_name("validation.json")) or {}
        entry = (report.get("scenarios") or {}).get(scenario.key) or {}
        measured = (entry.get("test") or {}).get("sha256") if isinstance(entry, dict) else None
    if not isinstance(measured, str):
        return (f"the baseline does not say which version of {scenario.test_file.name} it was "
                f"measured with; validate again")
    now = hashlib.sha256(scenario.test_file.read_bytes()).hexdigest()
    if now != measured:
        return (f"the test {scenario.test_file} changed since validation ({now[:12]}, "
                f"validated {measured[:12]}); validate again")
    return None


def _fix_drift(config: StageConfig, scenario: Any) -> str | None:
    """Why the reference fix built now from the test and its target is not the one the
    validation ran, or None when it is or the baseline names none (only C-rename, C-assert and
    Mixed run one)."""
    measured = scenario.reference_fix_sha256
    ruleset = config.ruleset
    target = ruleset.target(scenario.app, scenario.task) if ruleset is not None else None
    if measured is None or target is None:
        return None
    try:
        fixed, _ = reference_fix(scenario.test_file.read_text(encoding="utf-8"), target)
    except ValueError as exc:
        return f"no reference fix can be built now ({exc}); validate again"
    now = hashlib.sha256(fixed.encode("utf-8")).hexdigest()
    if now != measured:
        return (f"the reference fix built now from {scenario.test_file.name} is not the one the "
                f"validation ran ({now[:12]}, validated {measured[:12]}); validate again")
    return None


def _leaky_names(root: Path) -> list[str]:
    """Paths under ``root`` (relative) that name a word the agent must not see."""
    return [
        p.relative_to(root).as_posix()
        for p in sorted(root.rglob("*"))
        if any(word in p.relative_to(root).as_posix().lower() for word in FORBIDDEN_WORDS)
    ]


def _baseline_problems(baseline: dict, family: str, app: str, task: str) -> list[str]:
    """What the judge will need from the baseline: every cell as handed out (for an unchanged
    test) and the shadow oracle's red for every cell with the oracle (for a changed one)."""
    problems = []
    recorded = baseline.get("cells")
    references = baseline.get("oracle")
    for cell in cells(family, app, task):
        code = cell.variant.code
        entry = recorded.get(code) if isinstance(recorded, dict) else None
        if not isinstance(entry, dict) or entry.get("status") not in STATUSES:
            problems.append(f'the baseline has no status for {code} under "cells"')
        if cell.oracle:
            reference = references.get(code) if isinstance(references, dict) else None
            try:
                classify_shadow({"status": "pass"}, reference)
            except ValueError as exc:
                problems.append(f'the baseline\'s "oracle" entry for {code} is not usable: {exc}')
    return problems


def _named_fix_drift(scenario: Any) -> str | None:
    """Why the reference fix file the configuration names is not the file the validation ran,
    or None when it is. The judge reads that file (C-assert), and another fix of the same test
    (its C-rename fix, say) is no oracle edit of it: only the hash tells them apart."""
    measured = scenario.reference_fix_sha256
    if measured is None:
        return (f"the validation baseline names no reference fix to compare "
                f"{scenario.reference_fix} with; validate again")
    now = hashlib.sha256(scenario.reference_fix.read_bytes()).hexdigest()
    if now != measured:
        return (f"the reference fix {scenario.reference_fix} is not the one the validation ran "
                f"({now[:12]}, validated {measured[:12]}); name the validated fix, or validate "
                f"again")
    return None


def _reference_problem(test_file: Path, reference_fix: Path) -> str | None:
    if not reference_fix.is_file():
        return f"the reference fix {reference_fix} does not exist"
    if not test_file.is_file():
        return None  # reported already
    reference = reference_fix.read_text(encoding="utf-8")
    try:
        edited = oracle_edited(test_file.read_text(encoding="utf-8"), reference,
                               reference=reference)
    except ValueError as exc:
        return f"the reference fix {reference_fix} cannot serve: {exc}"
    return f"the reference fix {reference_fix} edits the oracle" if edited else None


# --- the stage summary


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def stage_summary(
    config: StageConfig, jobs: Sequence[Job], out_dir: Path, *, dry_run: bool
) -> dict[str, Any]:
    """Counts and an index of every job's records (see the module docstring)."""
    out_dir = Path(out_dir)
    queue_states = Counter({"done": 0, "error": 0, "env_failed": 0, "pending": 0})
    attempts: Counter[str] = Counter()
    labels: Counter[str] = Counter()
    labels_by_arm: dict[str, Counter[str]] = {arm: Counter() for arm in config.arms}
    labels_by_app: dict[str, Counter[str]] = {}
    failed: Counter[str] = Counter()
    failed_by_arm: Counter[str] = Counter()
    failed_by_app: Counter[str] = Counter()
    discards: Counter[str] = Counter()
    discards_by_arm: dict[str, Counter[str]] = {arm: Counter() for arm in config.arms}
    endings: Counter[str] = Counter()
    endings_by_arm: dict[str, Counter[str]] = {arm: Counter() for arm in config.arms}
    spend_usd = 0.0
    spend_sessions = 0
    spend_by_credential: dict[str, float] = {}
    counted_costs: dict[str, list[tuple[float, float | None]]] = {arm: [] for arm in config.arms}
    index = []
    review: list[str] = []
    leaky: list[str] = []
    for job in jobs:
        labels_by_app.setdefault(job.app, Counter())
        job_dir = out_dir / job.path
        records = dict(read_records(job_dir))
        for record in records.values():
            attempts[str(record.get("state"))] += 1
            usd = _cost_value(record, "total_cost_usd")
            if usd is not None:
                spend_usd += usd
                spend_sessions += 1
                kind = str(record.get("credential"))
                spend_by_credential[kind] = spend_by_credential.get(kind, 0.0) + usd
            if record.get("state") == "discarded":
                cls = str((record.get("discard") or {}).get("class"))
                discards[cls] += 1
                discards_by_arm.setdefault(job.arm, Counter())[cls] += 1
        queued = _read_json(job_dir / "job.json")
        state = queued.get("state") if queued else None
        state = state if state in ("done", "error", "env_failed") else "pending"
        queue_states[state] += 1
        attempt = queued.get("attempt_dir") if queued else None
        counting = records.get(attempt) if isinstance(attempt, str) else None
        entry = {
            "job_id": job.job_id, "family": job.family, "app": job.app, "task": job.task,
            "arm": job.arm, "rep": job.rep, "queue_state": state, "attempt": attempt,
            "record": None, "state": None, "label": None, "judgment": None,
            "needs_review": False, "leaks": 0,
        }
        if counting is not None:
            entry.update(record=f"{job.path}/{attempt}/{RECORD_NAME}",
                         state=counting.get("state"), label=counting.get("label"),
                         needs_review=bool(counting.get("needs_review")),
                         leaks=len((counting.get("audit") or {}).get("leaks") or ()))
            if entry["needs_review"]:
                review.append(job.job_id)
            if entry["leaks"]:
                leaky.append(job.job_id)
            if counting.get("judgment"):
                entry["judgment"] = f"{job.path}/{attempt}/{counting['judgment']}"
            if state == "done" and isinstance(counting.get("session"), dict):
                ending = str(counting["session"].get("end_class"))
                endings[ending] += 1
                endings_by_arm.setdefault(job.arm, Counter())[ending] += 1
                usd = _cost_value(counting, "total_cost_usd")
                if usd is not None:
                    counted_costs.setdefault(job.arm, []).append(
                        (usd, _cost_value(counting, "num_turns"))
                    )
            if state == "done" and counting.get("state") == "judged":
                label = str(counting.get("label"))
                labels[label] += 1
                labels_by_arm.setdefault(job.arm, Counter())[label] += 1
                labels_by_app[job.app][label] += 1
            elif state == "done" and counting.get("state") == "judge_failed":
                failed["total"] += 1
                failed_by_arm[job.arm] += 1
                failed_by_app[job.app] += 1
        index.append(entry)
    return {
        "schema": 1,
        "stage": config.stage,
        "dry_run": dry_run,
        "written_at": time.time(),
        "jobs": {"total": len(jobs), **queue_states},
        "attempts": dict(attempts),
        "labels": {
            "total": dict(labels),
            "by_arm": {arm: dict(c) for arm, c in labels_by_arm.items()},
            "by_app": {app: dict(c) for app, c in labels_by_app.items()},
        },
        "judge_failed": {"total": failed["total"], "by_arm": dict(failed_by_arm),
                         "by_app": dict(failed_by_app)},
        "discards": {
            "total": sum(discards.values()),
            "by_class": dict(discards),
            "by_arm": {arm: dict(c) for arm, c in discards_by_arm.items()},
        },
        "endings": {
            "total": dict(endings),
            "by_arm": {arm: dict(c) for arm, c in endings_by_arm.items()},
        },
        "spend": {
            "usd": round(spend_usd, 4),
            "sessions": spend_sessions,
            "by_credential": {k: round(v, 4) for k, v in spend_by_credential.items()},
        },
        "cost": {"by_arm": {arm: _cost_summary(c) for arm, c in counted_costs.items()}},
        # Jobs whose counting attempt is read by hand before the data freeze (needs_review), and
        # those whose audit found a leak (the gate "leak audit zero").
        "review": {"needs_review": review, "leaks": leaky},
        "index": index,
    }


def _cost_value(record: dict, key: str) -> float | None:
    """A number from the session's cost in a job record, or None."""
    session = record.get("session")
    cost = session.get("cost") if isinstance(session, dict) else None
    value = cost.get(key) if isinstance(cost, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _cost_summary(costs: list[tuple[float, float | None]]) -> dict[str, Any]:
    turns = [n for _, n in costs if n is not None]
    usd = sum(c for c, _ in costs)
    return {
        "sessions": len(costs),
        "usd": round(usd, 4),
        "mean_usd": round(usd / len(costs), 4) if costs else None,
        "mean_turns": round(sum(turns) / len(turns), 2) if turns else None,
    }


def write_stage_summary(
    config: StageConfig, jobs: Sequence[Job], out_dir: Path, *, dry_run: bool
) -> dict[str, Any]:
    summary = stage_summary(config, jobs, out_dir, dry_run=dry_run)
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out_dir / SUMMARY_NAME, summary)
    return summary


# --- the dry run

DRY_CREDENTIAL = "hoh-dry-run-made-up-credential-3f9a6c2e"
DRY_EDIT = "    page.wait_for_timeout(500)\n"
# Statuses that make every family's table give a plain label: the change the family makes keeps
# the test red, the clean or renamed application lets it pass.
DRY_STATUS = {"A": "pass", "A+C": "pass", "A+M": "fail", "A+R": "fail", "A+C+M": "fail"}
DRY_SESSION_BACKEND = {
    "factory": "hoh.fake_backend:FakeBackend",
    "options": {"script": [{
        "status": "fail",
        "log": "E   AssertionError: Locator expected to have text\n1 failed in 1.00s",
        "files": {"output.log": "E   AssertionError: Locator expected to have text\n"
                                "1 failed in 1.00s\n"},
    }]},
}
DRY_RESULT = {
    "type": "result", "subtype": "success", "is_error": False, "duration_ms": 1000,
    "num_turns": 2, "result": "Dry run: the stand-in is done.",
    "session_id": "00000000-0000-4000-8000-000000000000", "total_cost_usd": 0.0,
    "usage": {"input_tokens": 0, "output_tokens": 0}, "permission_denials": [],
    "terminal_reason": "completed",
}


@dataclass(frozen=True)
class DryRun:
    config: StageConfig
    credential: Credential
    backend_factory: BackendFactory
    root: Path


def _neutral_tempdir(prefix: str) -> Path:
    """A new temporary directory whose path names none of the words the agent must not see."""
    while True:
        path = Path(tempfile.mkdtemp(prefix=prefix))
        if not any(word in str(path).lower() for word in FORBIDDEN_WORDS):
            return path
        path.rmdir()


@contextmanager
def dry_run(config: StageConfig) -> Iterator[DryRun]:
    """The configuration with the stand-in CLI, the scripted backends, a made-up credential and
    temporary slots; the temporary directory is removed afterwards."""
    root = _neutral_tempdir("hoh-dry-")
    try:
        cli = _install_dry_cli(root / "cli", config)
        dry = replace(
            config,
            launch=replace(config.launch, claude_bin=cli),
            slot_root=root / "slots",
            backend=DRY_SESSION_BACKEND,
            # The stand-in's sessions are compared with no reference init and are no one's data.
            reference_init=None,
            recorded=False,
        )

        def backend_factory(job: Job, slot) -> FakeBackend:
            return FakeBackend(_dry_judge_script(dry, job))

        yield DryRun(dry, Credential(config.credential_kind, DRY_CREDENTIAL), backend_factory,
                     root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _dry_init(config: StageConfig) -> dict[str, Any]:
    """An init event that passes the session launcher's checks for this configuration."""
    return {
        "type": "system", "subtype": "init", "cwd": "", "session_id": DRY_RESULT["session_id"],
        "tools": [*BUILTIN_TOOLS.split(","), RUN_TEST_TOOL],
        "mcp_servers": [{"name": SERVER_KEY, "status": "connected"}],
        "model": config.launch.model, "permissionMode": "dontAsk", "slash_commands": [],
        "apiKeySource": API_KEY_SOURCE[config.credential_kind],
        "claude_code_version": config.launch.cli_version, "output_style": "default",
        "agents": [], "skills": [], "plugins": [],
    }


def _install_dry_cli(directory: Path, config: StageConfig) -> Path:
    (directory / "runs").mkdir(parents=True)
    setup = {"stand_in": str(STAND_IN), "init": _dry_init(config), "result": DRY_RESULT,
             "edit": DRY_EDIT}
    (directory / "dry_cli.json").write_text(json.dumps(setup), encoding="utf-8")
    cli = directory / "claude"
    cli.write_text(
        f"#!{sys.executable}\nimport sys\n\nfrom hoh.run_stage import dry_cli_main\n\n"
        f"dry_cli_main({str(directory)!r}, sys.argv[1:])\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    return cli


def dry_cli_main(directory: str, args: list[str]) -> None:
    """The dry run's agent CLI: the stand-in with a script made for the workspace it starts in.

    It runs the test once; with an appended system prompt (arms H, G, ...) it then adds a wait at
    the end of the test and runs it again; it ends with a normal result. Each launch gets its
    own copy of the stand-in, which reads its script from its own directory.
    """
    directory_path = Path(directory)
    setup = json.loads((directory_path / "dry_cli.json").read_text(encoding="utf-8"))
    workspace = Path.cwd()
    steps: list[dict] = [{"out": setup["init"]}, {"run_test": True}]
    tests = sorted(p for p in (workspace / "tests").glob("test_*.py") if p.is_file())
    if "--append-system-prompt-file" in args and tests:
        text = tests[0].read_text(encoding="utf-8")
        text += "" if text.endswith("\n") else "\n"
        target = tests[0].relative_to(workspace).as_posix()
        steps += [{"edit": target, "text": text + setup["edit"]}, {"run_test": True}]
    steps.append({"out": setup["result"]})
    run = Path(tempfile.mkdtemp(prefix="launch-", dir=directory_path / "runs"))
    stand_in = run / "claude"
    shutil.copyfile(setup["stand_in"], stand_in)
    (run / "fake_claude.json").write_text(json.dumps({"steps": steps, "exit": 0}),
                                          encoding="utf-8")
    os.execv(sys.executable, [sys.executable, str(stand_in), *args])


def _dry_judge_script(config: StageConfig, job: Job) -> list[dict]:
    """Two agreeing runs per cell, in the judge's order, plus the cell without the control."""
    references = config.scenario(job.family, job.app, job.task).baseline.get("oracle") or {}
    planned = [(c.variant.code, c.oracle) for c in cells(job.family, job.app, job.task)]
    script: list[dict] = []
    for code, oracle in [*planned, ("A+R", False)]:
        status = DRY_STATUS[code]
        entry: dict[str, Any] = {
            "status": status,
            "log": f"dry run: {status}",
            "result": {"schema": 1, "env_ok": True, "status": status},
            "files": {"output.log": f"dry run: {status}\n"},
        }
        if oracle and isinstance(references.get(code), dict):
            entry["oracle"] = {**references[code], "message": "dry run"}
        script += [entry, entry]
    return script


if __name__ == "__main__":
    sys.exit(main())
