"""The paper's numbers, from the job records of the recorded stages (docs/analysis-plan.md).

    python -m hoh.analysis --results RESULTS --stages 1a 1b explore [--partial] \
        [--out build/analysis]

The analysis plan is frozen: every number here is one of its three comparisons or a descriptive
figure it lists, and anything else carries ``"exploratory": true``. The code reads the job
records and never writes under the results root.

Loading (``load_stage``). A stage's jobs are the ``index`` of its ``stage_summary.json``: one
entry per job of the stage's configuration, started or not. For each entry the loader reads the
job's ``job.json`` (the queue's record, written for done, error and env_failed), the
``job_record.json`` of the attempt it names and, for section 8's retries, the record of each
other attempt the queue lists. The run is refused when the summary or a record is
from a dry run; when the summary's job counts, an entry's queue state, the label counts or what
section 8 counts (attempts, discards, endings, failed judgings: ``_check_counts``) differ from
the files; when a ``job.json`` on disk is not in the index; and when a job is unfinished
(pending, env_failed or judge_failed: states the harness runs or judges again) unless
``partial`` is given, which marks every output partial.

An *observation* is a job the queue finished (``done``) whose record is ``judged`` with a label.
Every other job is a non-observation with one reason: ``pending``, ``env_failed``,
``judge_failed`` (unfinished), or ``model_changed``, ``refused``, ``error`` (final).

The values every recorded session uses (analysis plan section 5) are checked per observation
(``pinned_differences``). A difference refuses the run (``check_pins``): the frozen plan has no
rule that would drop such a session, so the author decides. The values the plan only observed
in the pilot are read and reported (``pilot_values``).

The defect codes (analysis plan sections 1 and 9) of a reviewed stage are read from the
committed ``docs/coding/<stage>/`` once its gate says ready (``defect_codes``); until then their
figures are pending. A ready stage's folder must then hold together, or the run is refused: its
key names exactly the stage's observations (``load_codes``); ``key.json``, ``agreement.json``,
``adjudication.json``, ``adjudicated.csv`` and the two coders' files, which step 7 of
``docs/coding/README.md`` commits, are there and pass the checks ``_coding`` lists; and the
coders' agreement and the adjudication's counts, computed again from them, are what
``agreement.json`` and ``adjudication.json`` give (``coding_agreement``). Ready stages whose
coders are not the same two models in the same order refuse the run too
(``coding_agreement_all``).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import statistics
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy

from hoh import stats
from hoh.coding_export import (
    CODE_COLUMNS,
    COLUMN_FILE,
    COUNTS,
    _read_codes,
    adjudication_counts,
    cohen_kappa,
    column_agreement,
    session_id_of,
)
from hoh.labels import LABELS, pair_success
from hoh.rules import RuleSet, load_rules

# --- analysis plan section 5: the values every recorded session uses

PROMPTS: dict[str, tuple[str, str | None]] = {
    "N": ("a96eff8d399c89f103b74be4970529ed883188e6fbead206221540b4d47b6e7f", None),
    "H": ("b76de798a8b95f085d7daba27170828c5f4a7b6cbb26ddc3dd5b5df88b4aa699",
          "cd1cb00ed3d20e3c376d572569f2a5e6a0090c3f60f0d41c6372df4f085de989"),
    "G": ("b76de798a8b95f085d7daba27170828c5f4a7b6cbb26ddc3dd5b5df88b4aa699",
          "9c8aa54a777ae45d26ffbc8b58ecf96f8c86e741454314a29ab7e2aa923417ac"),
}
"""SHA-256 of each recorded arm's user message (``prompt.md``) and appended system prompt
(``append.md``; arm N has none). An arm not listed (the exploratory P and H_noskip) has its own
prompts: they are listed (``exploratory_prompts``), not checked."""
SETTINGS_SHA256 = "5e1345543b7856bf885a5f86b2cbc41470cac1fd36312ad105ed44d806432c41"
CLI_SHA256 = "84590fefe6338be9087f5814b61f9a54c5f21524163a4c127cb5a808e31009f2"
LAUNCH: dict[str, Any] = {
    "cli_version": "2.1.325", "model": "claude-sonnet-5-5", "effort": "medium",
    "cache_ttl": "1h", "max_turns": 80, "max_budget_usd": 1.5, "wall_clock_s": 1200,
    "hard_wall_clock_s": 3600,
}
"""The launch table of section 5, as ``out/launch.json`` records it under ``config``."""
MODEL = "claude-sonnet-5-5"
MAX_RUNS = 4
FALLBACK = "subscription"
CREDENTIALS = ("api_key", FALLBACK)
"""The API key, and the fallback of section 7 (sessions under it are flagged)."""
PILOT: dict[str, Any] = {
    "plugins": ("cc-plugin-agents-md@builtin", "cc-plugin-plugin-authoring@builtin",
                "cc-plugin-privacy@builtin"),
    "policy-limits.json": "5bc7f2578d4305791350dacb0593adbbcc91b6cfeeda28582702f3ceed5b0413",
    "remote-settings.json": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
}
"""Observed in the pilot: a different value is reported per arm and value, not refused."""

UNFINISHED = ("pending", "env_failed", "judge_failed")
FINAL = ("model_changed", "refused", "error")
QUEUE_STATES = ("done", "error", "env_failed", "pending")
SUMMARY = "stage_summary.json"
RECORD = "job_record.json"


class AnalysisRefused(ValueError):
    """The inputs keep the analysis from running: it writes nothing."""


# --- loading


@dataclass(frozen=True)
class Session:
    """One job of a stage, with what its files say."""

    stage: str
    job_id: str
    family: str
    app: str
    task: str
    arm: str
    rep: int
    queue_state: str
    attempt_dir: Path | None
    record: Mapping[str, Any] | None = field(repr=False)
    retried: tuple[str, ...] = ()
    """How each attempt before the counting one ended (``_retried``)."""

    @property
    def scenario(self) -> tuple[str, str, str]:
        return (self.family, self.app, self.task)

    @property
    def fallback(self) -> bool:
        """An observation run under the fallback credential (analysis plan section 7)."""
        return self.observation and (self.record or {}).get("credential") == FALLBACK

    @property
    def record_state(self) -> str | None:
        return self.record.get("state") if self.record is not None else None

    @property
    def observation(self) -> bool:
        return self.reason is None

    @property
    def label(self) -> str | None:
        return self.record.get("label") if self.observation else None

    @property
    def reason(self) -> str | None:
        """None for an observation, else why the job is not one."""
        if self.queue_state in ("pending", "env_failed"):
            return self.queue_state
        if self.queue_state == "done":
            if self.record_state == "judge_failed":
                return "judge_failed"
            return None
        # the queue state error: the record says which final reason
        discard = (self.record or {}).get("discard") or {}
        if self.record_state == "discarded" and discard.get("class") == "model_changed":
            return "model_changed"
        if self.record_state == "refused":
            return "refused"
        return "error"


@dataclass(frozen=True)
class Stage:
    name: str
    sessions: tuple[Session, ...]
    summary: Mapping[str, Any] = field(repr=False)
    partial: bool


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise AnalysisRefused(f"{path}: cannot be read ({exc})") from exc


def load_stage(results: Path, stage: str, *, partial: bool = False) -> Stage:
    """The jobs of ``results/<stage>/`` (see the module docstring), or AnalysisRefused."""
    root = Path(results) / stage
    summary = _read_json(root / SUMMARY)
    if not isinstance(summary, dict) or not isinstance(summary.get("index"), list):
        raise AnalysisRefused(f"{root / SUMMARY}: missing or without an index")
    if summary.get("dry_run") is not False:
        raise AnalysisRefused(f"{root / SUMMARY}: written by a dry run")
    index = summary["index"]
    counts = Counter(entry.get("queue_state") for entry in index)
    jobs = summary.get("jobs") or {}
    wanted = {"total": len(index), **{state: counts.get(state, 0) for state in QUEUE_STATES}}
    unknown = set(counts) - set(QUEUE_STATES)
    if unknown or any(jobs.get(key) != value for key, value in wanted.items()):
        raise AnalysisRefused(f"{root / SUMMARY}: its jobs {jobs} differ from its index {wanted}")
    sessions = []
    attempts: dict[str, list[tuple[str, dict | None]]] = {}
    for entry in index:
        job_dir = root / entry["job_id"]
        queued = _read_json(job_dir / "job.json")
        state = queued.get("state") if isinstance(queued, dict) else None
        state = state if state in ("done", "error", "env_failed") else "pending"
        if state != entry.get("queue_state"):
            raise AnalysisRefused(f"{entry['job_id']}: the index says {entry.get('queue_state')}, "
                                  f"its job.json says {state}")
        attempt = queued.get("attempt_dir") if isinstance(queued, dict) else None
        attempt_dir = job_dir / attempt if isinstance(attempt, str) else None
        record = _read_json(attempt_dir / RECORD) if attempt_dir is not None else None
        if record is not None and not isinstance(record, dict):
            raise AnalysisRefused(f"{attempt_dir / RECORD}: not a record")
        if record is not None and record.get("dry_run") is not False:
            raise AnalysisRefused(f"{attempt_dir / RECORD}: a dry run's record")
        if record is not None and (record.get("job") or {}).get("job_id") != entry["job_id"]:
            raise AnalysisRefused(f"{attempt_dir / RECORD}: the record of another job")
        listed = _listed(job_dir, queued)
        attempts[entry["job_id"]] = listed
        session = Session(stage=stage, job_id=entry["job_id"], family=entry["family"],
                          app=entry["app"], task=entry["task"], arm=entry["arm"],
                          rep=int(entry["rep"]), queue_state=state, attempt_dir=attempt_dir,
                          record=record, retried=_retried(listed, attempt))
        if state == "done" and session.record_state not in ("judged", "judge_failed"):
            raise AnalysisRefused(f"{entry['job_id']}: done, but its record is "
                                  f"{session.record_state}")
        if session.observation and not isinstance(session.label, str):
            raise AnalysisRefused(f"{entry['job_id']}: judged without a label")
        sessions.append(session)
    known = {s.job_id for s in sessions}
    for path in sorted((root / stage).rglob("job.json")) if (root / stage).is_dir() else ():
        job_id = path.parent.relative_to(root).as_posix()
        if job_id not in known:
            raise AnalysisRefused(f"{path}: a job.json not in the index of {root / SUMMARY}")
    _check_labels(root / SUMMARY, summary, sessions)
    _check_counts(root / SUMMARY, summary, sessions, attempts)
    unfinished = [s for s in sessions if s.reason in UNFINISHED]
    if unfinished and not partial:
        reasons = Counter(s.reason for s in unfinished)
        raise AnalysisRefused(f"stage {stage}: {len(unfinished)} unfinished jobs "
                              f"({dict(reasons)}); finish the stage, or pass --partial")
    return Stage(name=stage, sessions=tuple(sessions), summary=summary,
                 partial=bool(unfinished))


def _listed(job_dir: Path, queued: Any) -> list[tuple[str, dict | None]]:
    """Each attempt the queue lists in ``job.json`` (``attempts``, every attempt directory of
    the job) as its directory and its record, None when it has none that can be read."""
    if not isinstance(queued, dict):
        return []
    found = []
    for entry in queued.get("attempts") or ():
        name = entry.get("dir") if isinstance(entry, dict) else None
        if not isinstance(name, str):
            continue
        try:
            record = _read_json(job_dir / name / RECORD)
        except AnalysisRefused:
            record = None
        found.append((name, record if isinstance(record, dict) else None))
    return found


def _retried(listed: Sequence[tuple[str, dict | None]], counting: str | None) -> tuple[str, ...]:
    """How each listed attempt other than the counting one ended: the discard class of a
    discarded session (the harness queued it again), else its record's state, or
    ``no record``."""
    found = []
    for name, record in listed:
        if name == counting:
            continue
        if record is None:
            found.append("no record")
        elif record.get("state") == "discarded":
            found.append(str((record.get("discard") or {}).get("class")))
        else:
            found.append(str(record.get("state")))
    return tuple(found)


def _check_counts(path: Path, summary: Mapping[str, Any], sessions: Sequence[Session],
                  attempts: Mapping[str, Sequence[tuple[str, dict | None]]]) -> None:
    """Refuse when what section 8 counts differs from the summary, which ``hoh.run_stage``
    counts from the records on disk: the endings and the failed judgings of the done jobs, and,
    when no job is pending, the attempts by their record's state and the discarded ones by arm
    and class. A pending job has no ``job.json`` to list its attempts, so a stage with one
    leaves those two out."""

    def by_arm(table: Mapping[str, Any] | None) -> dict[str, dict[str, int]]:
        return {k: dict(v) for k, v in (table or {}).items() if v}

    endings: dict[str, Counter[str]] = {}
    failed: Counter[str] = Counter()
    for s in sessions:
        if s.queue_state != "done" or s.record is None:
            continue
        if isinstance(s.record.get("session"), dict):
            endings.setdefault(s.arm, Counter())[str(s.record["session"].get("end_class"))] += 1
        failed[s.arm] += s.record_state == "judge_failed"
    found = {"endings": (by_arm(endings), by_arm((summary.get("endings") or {}).get("by_arm"))),
             "judge_failed": ({arm: n for arm, n in failed.items() if n},
                              {arm: n for arm, n in ((summary.get("judge_failed") or {})
                                                     .get("by_arm") or {}).items() if n})}
    if not any(s.queue_state == "pending" for s in sessions):
        states: Counter[str] = Counter()
        discards: dict[str, Counter[str]] = {}
        for s in sessions:
            for _, record in attempts.get(s.job_id, ()):
                if record is None:
                    continue
                states[str(record.get("state"))] += 1
                if record.get("state") == "discarded":
                    cls = str((record.get("discard") or {}).get("class"))
                    discards.setdefault(s.arm, Counter())[cls] += 1
        found["attempts"] = (dict(states), dict(summary.get("attempts") or {}))
        found["discards"] = (by_arm(discards),
                             by_arm((summary.get("discards") or {}).get("by_arm")))
    for block, (records, summed) in found.items():
        if records != summed:
            raise AnalysisRefused(f"{path}: its {block} {summed} differ from the job records' "
                                  f"{records}")


def _check_labels(path: Path, summary: Mapping[str, Any], sessions: Sequence[Session]) -> None:
    labels = summary.get("labels") or {}
    observed = [s for s in sessions if s.observation]

    def counts(key) -> dict[str, dict[str, int]]:
        found: dict[str, Counter[str]] = {}
        for s in observed:
            found.setdefault(getattr(s, key), Counter())[s.label] += 1
        return {k: dict(v) for k, v in found.items()}

    def clean(table: Mapping[str, Mapping[str, int]] | None) -> dict[str, dict[str, int]]:
        return {k: dict(v) for k, v in (table or {}).items() if v}

    if (dict(labels.get("total") or {}) != dict(Counter(s.label for s in observed))
            or clean(labels.get("by_arm")) != counts("arm")
            or clean(labels.get("by_app")) != counts("app")):
        raise AnalysisRefused(f"{path}: its labels differ from the job records")


# --- section 5, per observation


def _attempt_json(session: Session, *parts: str) -> Any:
    if session.attempt_dir is None:
        return None
    return _read_json(session.attempt_dir.joinpath(*parts))


def _sha256_of(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def pinned_differences(session: Session) -> list[str]:
    """How an observation's launch differs from analysis plan section 5, in words (empty when
    it does not)."""
    problems: list[str] = []
    launch = _attempt_json(session, "out", "launch.json")
    if not isinstance(launch, dict):
        return ["no out/launch.json"]
    config = launch.get("config") or {}
    for key, value in LAUNCH.items():
        if config.get(key) != value:
            problems.append(f"{key} is {config.get(key)!r}, not {value!r}")
    cli = (launch.get("cli") or {}).get("sha256")
    if cli != CLI_SHA256:
        problems.append(f"the CLI binary's SHA-256 is {cli}")
    if launch.get("credential") not in CREDENTIALS:
        problems.append(f"the credential is {launch.get('credential')!r}")
    if launch.get("reference_init") is not True:
        problems.append("reference_init is not true")
    sha = launch.get("sha256") or {}
    if sha.get("settings.json") != SETTINGS_SHA256:
        problems.append(f"settings.json has SHA-256 {sha.get('settings.json')}")
    if session.arm in PROMPTS:
        user, append = PROMPTS[session.arm]
        if sha.get("prompt.md") != user:
            problems.append(f"prompt.md is not arm {session.arm}'s ({sha.get('prompt.md')})")
        if sha.get("append.md") != append:
            problems.append(f"append.md is not arm {session.arm}'s ({sha.get('append.md')})")
    end = _attempt_json(session, "out", "end.json")
    if not isinstance(end, dict) or end.get("init_problems"):
        problems.append(f"init_problems: {(end or {}).get('init_problems', 'no out/end.json')}")
    state = _attempt_json(session, "session", "session.json")
    if not isinstance(state, dict) or state.get("max_runs") != MAX_RUNS:
        problems.append(f"max_runs is {(state or {}).get('max_runs')}")
    models = sorted(_model_usage(session))
    if models != [MODEL]:
        problems.append(f"the models used are {models}")
    return problems


def _model_usage(session: Session) -> Mapping[str, Any]:
    session_part = (session.record or {}).get("session") or {}
    cost = session_part.get("cost") if isinstance(session_part, dict) else None
    usage = cost.get("modelUsage") if isinstance(cost, dict) else None
    return usage if isinstance(usage, dict) else {}


def check_pins(stages: Iterable[Stage]) -> None:
    """Refuse when any observation differs from section 5 (see ``pinned_differences``)."""
    found = []
    for stage in stages:
        for session in stage.sessions:
            if session.observation:
                found += [f"{session.job_id}: {p}" for p in pinned_differences(session)]
    if found:
        shown = "\n  ".join(found[:20])
        more = f"\n  ... and {len(found) - 20} more" if len(found) > 20 else ""
        raise AnalysisRefused(f"{len(found)} differences from analysis plan section 5 "
                              f"(the author decides):\n  {shown}{more}")


def exploratory_prompts(stages: Iterable[Stage]) -> list[dict[str, Any]]:
    """Per stage and arm the plan does not pin (``PROMPTS``): how many observations had each
    SHA-256 of ``prompt.md`` and of ``append.md``, as ``out/launch.json`` records them."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for stage in stages:
        for s in stage.sessions:
            if not s.observation or s.arm in PROMPTS:
                continue
            row = rows.setdefault((stage.name, s.arm), {
                "stage": stage.name, "arm": s.arm, "prompt.md": Counter(),
                "append.md": Counter()})
            sha = (_attempt_json(s, "out", "launch.json") or {}).get("sha256") or {}
            for name in ("prompt.md", "append.md"):
                row[name][str(sha.get(name))] += 1
    return [{**row, "prompt.md": dict(row["prompt.md"]), "append.md": dict(row["append.md"])}
            for row in rows.values()]


def pilot_values(session: Session) -> dict[str, Any]:
    """The values the plan observed in the pilot, as this session has them."""
    plugins: tuple[str, ...] = ()
    if session.attempt_dir is not None:
        stream = session.attempt_dir / "out" / "stream.jsonl"
        try:
            with stream.open(encoding="utf-8") as lines:
                init = json.loads(next(lines, "{}") or "{}")
        except (OSError, ValueError):
            init = {}
        found = init.get("plugins") if isinstance(init, dict) else None
        plugins = tuple(sorted(str(p.get("source") or p.get("name")) if isinstance(p, dict)
                               else str(p) for p in found or ()))
    cfg = session.attempt_dir / "cfg" if session.attempt_dir is not None else None
    return {
        "plugins": plugins,
        "policy-limits.json": _sha256_of(cfg / "policy-limits.json") if cfg else None,
        "remote-settings.json": _sha256_of(cfg / "remote-settings.json") if cfg else None,
    }


# --- clusters and the dev split (analysis plan sections 2 and 4)


RULE_FILES = {"c_rename": "rename", "b_remove": "rename", "mixed": "rename", "c_assert": "assert"}
"""Which rules file names a family's targets: 1b's for C-rename, B-remove and Mixed (the same
targets), C-assert's own."""


@dataclass(frozen=True)
class Scenarios:
    """What a scenario's cluster and its place in the dev split are read from."""

    selection: Mapping[str, Any]
    rules: Mapping[str, RuleSet]

    def _payload(self, app: str, task: str) -> str | None:
        for cluster in (self.selection.get(app) or {}).get("clusters") or ():
            if cluster.get("chosen") == task:
                return cluster.get("payload")
        return None

    def _label(self, family: str, app: str, task: str) -> str | None:
        ruleset = self.rules.get(RULE_FILES.get(family, ""))
        target = ruleset.target(app, task) if ruleset is not None else None
        return target.name if target is not None else None

    def cluster(self, family: str, app: str, task: str) -> tuple[str, ...]:
        """B-oracle: the payload; C-rename and B-remove: the app and the rewritten label;
        Mixed: the payload and the label; C-assert: the app and the rewritten text. The family
        comes first, so a pooled figure has the sum of its families' clusters."""
        payload = self._payload(app, task)
        label = self._label(family, app, task)
        if family == "b_oracle" and payload is not None:
            return (family, payload)
        if family in ("c_rename", "b_remove", "c_assert") and label is not None:
            return (family, app, label)
        if family == "mixed" and payload is not None and label is not None:
            return (family, app, payload, label)
        raise AnalysisRefused(f"{family}/{app}/{task} has no cluster in the selection and rules")

    def dev(self, app: str, task: str) -> bool:
        """The test is the chosen test of a cluster with ``dev: true`` (analysis plan
        section 4)."""
        return any(cluster.get("chosen") == task and cluster.get("dev") is True
                   for cluster in (self.selection.get(app) or {}).get("clusters") or ())


def load_scenarios(selection: Path, rules: Mapping[str, Path]) -> Scenarios:
    """The selection file and the rules files by name ("rename", "assert"); a rules file that
    does not exist yet is left out."""
    data = _read_json(Path(selection))
    if not isinstance(data, dict):
        raise AnalysisRefused(f"{selection}: missing")
    return Scenarios(selection=data, rules={name: load_rules(path) for name, path in rules.items()
                                            if Path(path).exists()})


def outside_dev(sessions: Iterable[Session], scenarios: Scenarios) -> list[Session]:
    """The sessions outside the dev split: a dev-split test's sessions are dropped, and a cluster
    keeps its other tests' sessions (analysis plan section 4)."""
    return [s for s in sessions if not scenarios.dev(s.app, s.task)]


# --- rates and paired differences (analysis plan sections 2 and 3)

SEED = 20261114
"""The seed of every bootstrap and of the sign-flip draws (analysis plan section 2)."""
N_BOOT = 10_000
N_PERM = 100_000
MARGIN = 0.10
"""Comparison 3's non-inferiority margin, in rate points."""
INDETERMINATE = "indeterminate"
ONE_CLUSTER = "one cluster: no interval, which needs at least two"

FALSE_HEALS = frozenset(label for label in LABELS if label.startswith("false_heal"))
"""The false-heal outcome: bug-encoded, blind, other, workaround, and Mixed's own label."""


def is_false_heal(label: str) -> bool:
    """The outcome of comparisons 1 and 2."""
    return label in FALSE_HEALS


def is_bug_preserving_heal(label: str) -> bool:
    """The outcome of comparison 3."""
    return label == "bug_preserving_heal"


def _value(label: str, outcome: Callable[[str], bool], indeterminate_as: bool) -> int:
    if label == INDETERMINATE:
        return int(indeterminate_as)
    return int(bool(outcome(label)))


@dataclass(frozen=True)
class Rate:
    """The share of observations with an outcome. ``interval`` is the cluster t interval (the
    primary one), ``bootstrap`` the percentile interval of the cluster bootstrap (sensitivity),
    ``bounds`` the rate with every indeterminate session counted as not the outcome and as the
    outcome, and ``wilson_upper`` the upper limit of the Wilson interval over clusters, given
    for a rate of 0 only. A stratum with fewer than two clusters has no interval of any kind,
    and ``note`` says why."""

    n: int
    k: int
    clusters: int
    indeterminate: int
    rate: float | None
    interval: tuple[float, float] | None
    bootstrap: tuple[float, float] | None
    bounds: tuple[float, float] | None
    wilson_upper: float | None
    note: str | None


def rate(sessions: Iterable[Session], outcome: Callable[[str], bool], scenarios: Scenarios, *,
         removed: Collection[str] = frozenset()) -> Rate:
    """The rate of ``outcome`` over the observations among ``sessions`` whose job is not in
    ``removed`` (the sessions the review found the runner changed), clustered by
    ``scenarios.cluster``. Indeterminate sessions stay in the denominator."""
    kept = [s for s in sessions if s.observation and s.job_id not in removed]
    if not kept:
        return Rate(0, 0, 0, 0, None, None, None, None, None, "no observation")
    values = [_value(s.label, outcome, False) for s in kept]
    clusters = [scenarios.cluster(*s.scenario) for s in kept]
    n, k = len(values), sum(values)
    indeterminate = sum(1 for s in kept if s.label == INDETERMINATE)
    bounds = (k / n, (k + indeterminate) / n)
    count = len(set(clusters))
    if count < 2:
        return Rate(n, k, count, indeterminate, k / n, None, None, bounds, None, ONE_CLUSTER)
    _, lo, hi = stats.cluster_t_ci(values, clusters)
    _, low, high = stats.cluster_bootstrap_ci(values, clusters, n_boot=N_BOOT, seed=SEED)
    wilson = stats.wilson_ci(0, count)[1] if k == 0 else None
    return Rate(n, k, count, indeterminate, k / n, (lo, hi), (low, high), bounds, wilson, None)


@dataclass(frozen=True)
class Paired:
    """The first arm minus the second, paired within a scenario. ``bounds`` are the smallest and
    largest of the four differences with each arm's indeterminate sessions counted as the
    outcome or as not; ``dropped`` names each scenario left out and why, per arm and
    repetition; ``sign_patterns`` says how ``sign_flip_p`` was computed."""

    pairs: int
    scenarios: int
    clusters: int
    difference: float | None
    interval: tuple[float, float] | None
    bootstrap: tuple[float, float] | None
    sign_flip_p: float | None
    sign_patterns: str | None
    bounds: tuple[float, float] | None
    dropped: tuple[dict[str, Any], ...]
    note: str | None


def paired(sessions: Iterable[Session], outcome: Callable[[str], bool], scenarios: Scenarios, *,
           removed: Collection[str] = frozenset(), arms: tuple[str, str] = ("G", "H"),
           sign_flip: bool = False) -> Paired:
    """``arms[0]`` minus ``arms[1]`` on ``outcome`` (comparisons 2 and 3).

    ``sessions`` are every job of the selection in both arms, observations or not. A scenario
    enters only when each of its repetitions in both arms is an observation that the review did
    not remove; otherwise it is dropped from both arms, whatever the cause (analysis plan section
    3): a non-observation's reason, ``review`` for a removed session, ``missing`` for a
    repetition one arm has and the other has not. Repetition r of one arm is paired with
    repetition r of the other; the interval and the sign-flip test work on cluster totals, so
    the pairing within a scenario does not change them. ``sign_flip`` adds comparison 2's
    p-value.
    """
    first, second = arms
    grouped: dict[tuple[str, ...], dict[str, dict[int, Session]]] = {}
    for s in sessions:
        if s.arm in arms:
            grouped.setdefault((s.stage, *s.scenario), {first: {}, second: {}})[s.arm][s.rep] = s
    rows: list[tuple[Session, Session, tuple[str, ...]]] = []
    dropped: list[dict[str, Any]] = []
    entered = 0
    for key in sorted(grouped):
        by_arm = grouped[key]
        reps = sorted(set(by_arm[first]) | set(by_arm[second]))
        causes = []
        for arm in arms:
            for rep in reps:
                s = by_arm[arm].get(rep)
                cause = ("missing" if s is None else s.reason if not s.observation
                         else "review" if s.job_id in removed else None)
                if cause is not None:
                    causes.append({"arm": arm, "rep": rep, "cause": cause})
        if causes:
            dropped.append({"scenario": "/".join(key), "causes": tuple(causes)})
            continue
        entered += 1
        cluster = scenarios.cluster(*key[1:])
        rows += [(by_arm[first][rep], by_arm[second][rep], cluster) for rep in reps]
    if not rows:
        return Paired(0, 0, 0, None, None, None, None, None, None, tuple(dropped), "no pair")

    def column(index: int, indeterminate_as: bool) -> list[int]:
        return [_value(row[index].label, outcome, indeterminate_as) for row in rows]

    pairs = len(rows)
    a, b = column(0, False), column(1, False)
    # sums of whole numbers, divided once: no rounding between the two arms
    difference = (sum(a) - sum(b)) / pairs
    crossed = [(sum(column(0, x)) - sum(column(1, y))) / pairs
               for x in (False, True) for y in (False, True)]
    bounds = (min(crossed), max(crossed))
    clusters = [row[2] for row in rows]
    count = len(set(clusters))
    if count < 2:
        return Paired(pairs, entered, count, difference, None, None, None, None, bounds,
                      tuple(dropped), ONE_CLUSTER)
    _, lo, hi = stats.paired_cluster_t_ci(a, b, clusters)
    _, low, high = stats.paired_cluster_bootstrap_diff(a, b, clusters, n_boot=N_BOOT, seed=SEED)
    p_value = patterns = None
    if sign_flip:
        p_value = stats.sign_flip_p(a, b, clusters, n_perm=N_PERM, seed=SEED)
        patterns = f"all {2 ** count}" if count <= 20 else f"{N_PERM:,} drawn with seed {SEED}"
    return Paired(pairs, entered, count, difference, (lo, hi), (low, high), p_value, patterns,
                  bounds, tuple(dropped), None)


def noninferiority(lo: float, hi: float, margin: float = MARGIN) -> str:
    """Comparison 3's outcome (analysis plan section 3): non-inferior when the lower bound is
    above minus the margin, inferior when the upper bound is below it, otherwise
    inconclusive."""
    if stats.noninferior(lo, margin):
        return "non-inferior"
    return "inferior" if hi < -margin else "inconclusive"


def reduces_false_heals(hi: float) -> bool:
    """Comparison 2's decision (analysis plan section 3): the upper bound is below 0."""
    return bool(hi < 0)


# --- the human review (design doc 5.9, analysis plan section 8)

REVIEWED = ("1a", "1b", "2-mixed", "2-c-assert")
"""The stages whose sessions enter a primary or pre-listed descriptive figure. The exploratory
stage is not reviewed: its figures carry the exploratory mark only."""
DECISIONS = ("stands", "changed_runner")
"""``stands``: the session keeps its label. ``changed_runner``: a confirmed leak, the session
leaves every figure computed over observations (the labels table shows it beside the labels)."""


def worksheet(stage: Stage) -> list[dict[str, Any]]:
    """The review worksheet of a reviewed stage: one row per observation that is a false heal,
    lists needs_review or has an audit leak, with where to read it. It has no decision column:
    the decisions are the reviewer's own file (``load_review``)."""
    if stage.name not in REVIEWED:
        return []
    rows = []
    for s in stage.sessions:
        if not s.observation:
            continue
        record = s.record or {}
        needs = [str(x) for x in record.get("needs_review") or ()]
        leaks = list((record.get("audit") or {}).get("leaks") or ())
        reasons = ([*(["false_heal"] if is_false_heal(s.label) else []),
                    *(["needs_review"] if needs else []), *(["audit_leak"] if leaks else [])])
        if not reasons:
            continue
        attempt = s.attempt_dir
        rows.append({
            "job_id": s.job_id, "stage": s.stage, "family": s.family, "app": s.app,
            "task": s.task, "arm": s.arm, "rep": s.rep, "label": s.label,
            "subtype": record.get("subtype"), "reasons": ";".join(reasons),
            "needs_review": "; ".join(needs), "leaks": len(leaks),
            "transcript": str(attempt / "out" / "stream.jsonl") if attempt else None,
            "original_tests": str(attempt / "session" / "original_tests") if attempt else None,
            "returned_tests": str(attempt / "returned" / "tests") if attempt else None,
            "snapshots": str(attempt / "snapshots") if attempt else None})
    return rows


@dataclass(frozen=True)
class Review:
    """The worksheet rows of the reviewed stages and the reviewer's decisions on them."""

    rows: frozenset[str]
    decisions: Mapping[str, str]

    @property
    def removed(self) -> frozenset[str]:
        return frozenset(job for job, decision in self.decisions.items()
                         if decision == "changed_runner")

    def mark(self, job_ids: Iterable[str]) -> str | None:
        """A figure's review state from the worksheet rows among its sessions: None when it has
        none, ``reviewed`` when each has a decision."""
        among = self.rows.intersection(job_ids)
        if not among:
            return None
        decided = sum(1 for job in among if job in self.decisions)
        if decided == len(among):
            return "reviewed"
        if decided == 0:
            return "review not applied"
        return f"review incomplete ({decided} of {len(among)})"


def load_review(stages: Iterable[Stage], review_dir: Path | None) -> Review:
    """The worksheets of ``stages`` and the decisions in ``review_dir/<stage>.csv`` (columns
    job_id, decision, note), which are read and never written. A job that is not a worksheet
    row, a job decided twice, or another decision value refuses the run."""
    rows: set[str] = set()
    decisions: dict[str, str] = {}
    for stage in stages:
        own = {row["job_id"] for row in worksheet(stage)}
        rows |= own
        path = Path(review_dir) / f"{stage.name}.csv" if review_dir is not None else None
        if path is None or not path.is_file():
            continue
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not {"job_id", "decision"} <= set(reader.fieldnames or ()):
                raise AnalysisRefused(f"{path}: needs the columns job_id and decision")
            for line in reader:
                job = (line.get("job_id") or "").strip()
                decision = (line.get("decision") or "").strip()
                if job not in own:
                    raise AnalysisRefused(f"{path}: {job!r} is not a row of the worksheet")
                if job in decisions:
                    raise AnalysisRefused(f"{path}: {job} is decided more than once")
                if decision not in DECISIONS:
                    raise AnalysisRefused(f"{path}: {job}: the decision must be stands or "
                                          f"changed_runner, not {decision!r}")
                decisions[job] = decision
    return Review(rows=frozenset(rows), decisions=decisions)


REFUSED = "refused-attempts.csv"
"""The attempts whose flagged action the agent's CLI refused, so that it never ran: section 8's
decisions keep them in the main results, and the figures without them are exploratory."""


def load_refused(stages: Iterable[Stage], review_dir: Path | None) -> frozenset[str]:
    """The jobs in ``review_dir/refused-attempts.csv`` (columns job_id, note) of the stages
    loaded; the file is read and never written. Each line is checked against the worksheet of
    its own stage, the first part of its job_id. A job of a stage that is not reviewed, a job
    that is not a row of its stage's worksheet, a row without an audit leak, or a job listed
    twice refuses the run; a job of a reviewed stage that is not loaded is left aside. Without
    the file, none is listed."""
    path = Path(review_dir) / REFUSED if review_dir is not None else None
    if path is None or not path.is_file():
        return frozenset()
    leaks = {stage.name: {row["job_id"]: "audit_leak" in row["reasons"].split(";")
                          for row in worksheet(stage)} for stage in stages}
    listed: set[str] = set()
    jobs: set[str] = set()
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if "job_id" not in (reader.fieldnames or ()):
            raise AnalysisRefused(f"{path}: needs the column job_id")
        for line in reader:
            job = (line.get("job_id") or "").strip()
            stage = job.split("/", 1)[0]
            if stage not in REVIEWED:
                raise AnalysisRefused(f"{path}: {job!r}: {stage!r} is not a reviewed stage")
            if job in listed:
                raise AnalysisRefused(f"{path}: {job} is listed more than once")
            listed.add(job)
            if stage not in leaks:
                continue
            if job not in leaks[stage]:
                raise AnalysisRefused(f"{path}: {job!r} is not a row of the worksheet")
            if not leaks[stage][job]:
                raise AnalysisRefused(f"{path}: {job} has no audit leak")
            jobs.add(job)
    return frozenset(jobs)


# --- the three comparisons: which sessions each figure takes (analysis plan section 3)

BUG_PARTS = (("1a", "b_oracle"), ("1b", "b_remove"))
"""Comparisons 1 and 2, on bug scenarios: B-oracle of stage 1a and B-remove of stage 1b."""
BENIGN_PARTS = (("1b", "c_rename"), ("2-c-assert", "c_assert"))
"""Comparison 3, on benign scenarios: C-rename of stage 1b, and C-assert once its stage ran."""
SPLITS = ("all", "outside_dev")


def comparisons(stages: Iterable[Stage], scenarios: Scenarios, *,
                review: Review | None = None,
                present: Collection[str] = ()) -> list[dict[str, Any]]:
    """Every figure of the three comparisons, each naming its sessions as (stage, family, arm).

    Comparison 1: the false-heal rate of N and H (primary) and of G (descriptive), per family
    and pooled. Comparisons 2 and 3: G minus H over the families each takes, each on all
    clusters (secondary) and outside the dev split (primary, with the decision). Comparison 2
    takes B-oracle and B-remove pooled, and takes no decision unless both are loaded: alone, a
    family's figure is exploratory. Comparison 3 takes C-rename, and C-assert once its stage is
    loaded: on C-rename alone its figure is named ``c_rename``. Where two families enter, each
    one's figure beside the pooled one is exploratory, with no decision. A stage that no part
    names enters no figure.

    Each figure has ``partial``, true when a stage it takes is partial; ``review``, the review
    mark of the sessions it takes (``Review.mark``), whose ``changed_runner`` sessions leave it
    (without ``review`` the worksheets count as undecided); ``fallback``, how many observations
    it counts ran under the fallback credential; and ``missing``, the stages of a family the
    comparison takes that are in ``present`` (the stages of the results root) but not loaded.
    """
    stages = list(stages)
    by_name = {stage.name: stage for stage in stages}
    review = review if review is not None else load_review(stages, None)
    removed = review.removed

    def select(parts, arms):
        names, chosen = [], []
        for stage, family in parts:
            names += [[stage, family, arm] for arm in arms]
            chosen += [s for s in by_name[stage].sessions if s.family == family and s.arm in arms]
        return names, chosen

    def figure(number, arm, group, split, names, taken, role, result, decision=None,
               missing=()):
        return {"id": f"{number}/{arm}/{group}/{split}" if number == 1
                else f"{number}/{group}/{split}",
                "comparison": number, "arm": arm, "group": group, "split": split,
                "sessions": names, "role": role, "exploratory": role == "exploratory",
                "decision": decision, "partial": any(by_name[n[0]].partial for n in names),
                "review": review.mark(s.job_id for s in taken),
                "fallback": _fallback(taken, removed), "missing": list(missing),
                "result": asdict(result)}

    def within(sessions, split):
        return outside_dev(sessions, scenarios) if split == "outside_dev" else list(sessions)

    figures = []
    bug = [part for part in BUG_PARTS if part[0] in by_name]
    groups = [(family, [(stage, family)]) for stage, family in bug]
    groups += [("pooled", bug)] if len(bug) > 1 else []
    for arm in ("N", "H", "G"):
        for group, parts in groups:
            names, sessions = select(parts, (arm,))
            for split in SPLITS:
                taken = within(sessions, split)
                result = rate(taken, is_false_heal, scenarios, removed=removed)
                figures.append(figure(1, arm, group, split, names, taken,
                                      "descriptive" if arm == "G" else "primary", result))
    benign = [part for part in BENIGN_PARTS if part[0] in by_name]
    unloaded = sorted(stage for stage, _ in BENIGN_PARTS
                      if stage in present and stage not in by_name)
    for number, parts, outcome in ((2, bug, is_false_heal), (3, benign, is_bug_preserving_heal)):
        if not parts:
            continue
        missing = unloaded if number == 3 else []
        if len(parts) > 1:
            groups = [("pooled", parts, True)]
            groups += [(family, [(stage, family)], False) for stage, family in parts]
        else:  # one family: comparison 3's decision is on it, comparison 2's needs both
            groups = [(parts[0][1], parts, number == 3)]
        for group, group_parts, decides in groups:
            names, sessions = select(group_parts, ("G", "H"))
            for split in SPLITS:
                taken = within(sessions, split)
                result = paired(taken, outcome, scenarios, removed=removed,
                                sign_flip=number == 2)
                if not decides:
                    figures.append(figure(number, "G-H", group, split, names, taken,
                                          "exploratory", result, missing=missing))
                    continue
                decision = None
                if split == "outside_dev" and result.interval is not None:
                    lo, hi = result.interval
                    decision = (noninferiority(lo, hi) if number == 3
                                else "reduces false heals" if reduces_false_heals(hi)
                                else "no reduction shown")
                figures.append(figure(number, "G-H", group, split, names, taken,
                                      "primary" if split == "outside_dev" else "secondary",
                                      result, decision, missing))
    return figures


def without_refused(stages: Iterable[Stage], scenarios: Scenarios, *, review: Review,
                    refused: Collection[str],
                    present: Collection[str] = ()) -> list[dict[str, Any]]:
    """Every figure of ``comparisons`` and of ``descriptive_figures`` again with the ``refused``
    attempts left out as well, each exploratory, under ``refused-out/<id>``, with ``of`` the
    comparison it repeats (None for a descriptive figure, Mixed's among them) and no decision.
    They stay out of the comparisons table and the forest plot; ``numbers.tex`` and
    ``figures.csv`` carry them. None when no attempt is listed."""
    if not refused:
        return []
    stages = list(stages)
    out = Review(rows=review.rows,
                 decisions={**review.decisions, **{job: "changed_runner" for job in refused}})
    again = [*comparisons(stages, scenarios, review=out, present=present),
             *descriptive_figures(stages, scenarios, review=out)]
    return [{**figure, "id": f"refused-out/{figure['id']}", "of": figure["comparison"],
             "comparison": None, "role": "exploratory", "exploratory": True, "decision": None}
            for figure in again]


def _fallback(taken: Iterable[Session], removed: Collection[str]) -> int:
    """How many of the observations a figure counts ran under the fallback credential."""
    return sum(1 for s in taken if s.fallback and s.job_id not in removed)


# --- the labels table (analysis plan section 1)


def labels_table(stages: Iterable[Stage], *,
                 removed: Collection[str] = frozenset()) -> list[dict[str, Any]]:
    """One row per stage, family and arm: the automatic labels of the observations (equal to
    the stage summary's), the indeterminate and needs_review counts, the non-observations by
    reason, and how many observations the review removed (``removed``)."""
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for stage in stages:
        for s in stage.sessions:
            row = rows.setdefault((stage.name, s.family, s.arm), {
                "stage": stage.name, "family": s.family, "arm": s.arm, "labels": Counter(),
                "observations": 0, "indeterminate": 0, "needs_review": 0,
                "non_observations": Counter(), "removed": 0})
            if not s.observation:
                row["non_observations"][s.reason] += 1
                continue
            row["observations"] += 1
            row["labels"][s.label] += 1
            row["indeterminate"] += s.label == INDETERMINATE
            row["needs_review"] += bool((s.record or {}).get("needs_review"))
            row["removed"] += s.job_id in removed
    return [{**row, "labels": dict(row["labels"]),
             "non_observations": dict(row["non_observations"])} for row in rows.values()]


# --- section 8: censoring, the run_test limit, errors and leaks, per arm


def _result_subtype(session: Session) -> str | None:
    """The subtype of the stream's last result line."""
    if session.attempt_dir is None:
        return None
    try:
        lines = (session.attempt_dir / "out" / "stream.jsonl").read_text(
            encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return event.get("subtype")
    return None


CAPS = {"error_max_turns": "turns", "error_max_budget_usd": "budget"}
"""The result line's subtypes that name a cap."""


def cap_of(session: Session) -> str | None:
    """Which cap ended a censored observation: ``turns`` or ``budget`` when its result line
    names one (a result line before the harness's stop decides the class, ``hoh.session``),
    else ``time`` when the harness stopped it at the agent-time cap (``stop.reason``
    ``wall_clock`` of ``out/end.json``), else ``other``; None for a session no cap ended. A stop
    at the hard wall clock with no result line before it is classed ``environment`` and run
    again: section 8 counts it among the retries, by its discard class."""
    record = session.record or {}
    if (record.get("session") or {}).get("end_class") != "censored":
        return None
    named = CAPS.get(_result_subtype(session) or "")
    if named is not None:
        return named
    end = _attempt_json(session, "out", "end.json") or {}
    return "time" if (end.get("stop") or {}).get("reason") == "wall_clock" else "other"


def section_8(stages: Iterable[Stage], *,
              removed: Collection[str] = frozenset()) -> list[dict[str, Any]]:
    """Per stage, family and arm: the sessions a cap ended (by cap), those that used every
    counted ``run_test`` call, refused calls and the free first call, the non-observations by
    reason (harness errors, model switches or refusals, unfinished), needs_review, audit leaks
    and the leaks the review confirmed (``removed``); the attempts discarded and run again, by
    how each ended (``retries``, from ``Session.retried``), the judgings made again
    (``judging_retries``: the record's judgings not ``judged``), and the observations run under
    the fallback credential (``fallback``, analysis plan section 7)."""
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for stage in stages:
        for s in stage.sessions:
            row = rows.setdefault((stage.name, s.family, s.arm), {
                "stage": stage.name, "family": s.family, "arm": s.arm, "jobs": 0,
                "observations": 0, "capped": Counter(), "run_test_limit": 0,
                "refused_sessions": 0, "refused_calls": 0, "free_first_call": 0,
                "non_observations": Counter(), "needs_review": 0, "audit_leaks": 0,
                "confirmed_leaks": 0, "retries": Counter(), "judging_retries": 0,
                "fallback": 0})
            row["jobs"] += 1
            row["retries"].update(s.retried)
            row["judging_retries"] += sum(
                1 for judging in (s.record or {}).get("judgings") or ()
                if isinstance(judging, dict) and judging.get("state") != "judged")
            if not s.observation:
                row["non_observations"][s.reason] += 1
                continue
            row["observations"] += 1
            row["fallback"] += s.fallback
            cap = cap_of(s)
            if cap is not None:
                row["capped"][cap] += 1
            state = _attempt_json(s, "session", "run_test_state.json") or {}
            limit = (_attempt_json(s, "session", "session.json") or {}).get("max_runs", MAX_RUNS)
            row["run_test_limit"] += state.get("count") == limit
            refused = state.get("refused") or 0
            row["refused_sessions"] += refused > 0
            row["refused_calls"] += refused
            row["free_first_call"] += any(isinstance(c, dict) and c.get("call") == 0
                                          for c in state.get("calls") or ())
            record = s.record or {}
            row["needs_review"] += bool(record.get("needs_review"))
            row["audit_leaks"] += bool((record.get("audit") or {}).get("leaks"))
            row["confirmed_leaks"] += s.job_id in removed
    return [{**row, "capped": dict(row["capped"]),
             "non_observations": dict(row["non_observations"]), "retries": dict(row["retries"])}
            for row in rows.values()]


# --- the pilot values, per stage, arm and value (analysis plan section 5)


def pilot_table(stages: Iterable[Stage]) -> dict[str, dict[str, dict[str, Any]]]:
    """How many observations of each stage and arm had each value the plan observed in the
    pilot, and whether every one had the pilot's."""
    table: dict[str, dict[str, dict[str, Any]]] = {}
    for stage in stages:
        for s in stage.sessions:
            if not s.observation:
                continue
            row = table.setdefault(stage.name, {}).setdefault(
                s.arm, {key: Counter() for key in PILOT})
            for key, value in pilot_values(s).items():
                row[key][",".join(value) if isinstance(value, tuple) else str(value)] += 1
    expected = {key: ",".join(v) if isinstance(v, tuple) else v for key, v in PILOT.items()}
    return {stage: {arm: {"values": {key: dict(counts) for key, counts in row.items()},
                          "as_in_the_pilot": all(set(counts) == {expected[key]}
                                                 for key, counts in row.items())}
                    for arm, row in arms.items()} for stage, arms in table.items()}


# --- descriptives (analysis plan section 9, design doc 5.8)

RED_VARIANT = {"b_oracle": "A+M", "b_remove": "A+R", "c_rename": "A+C", "mixed": "A+C+M",
               "c_assert": "A+C"}
"""The variant each family hands out red: its red run in the validation is the scenario's
failure signature."""
RUN_TEST = "mcp__runner__run_test"
EDITS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
"""The tool calls that change a file of the workspace."""
PAIR = ("c_rename", "b_remove")
"""The two families of a pair: a test's harmless rename and the removal of the same control."""
MIXED = ("2-mixed", "mixed")
EXPLORE = "explore"
EXPLORE_ARMS = ("P", "H_noskip")


def _key(s: Session) -> tuple[str, str, str]:
    return (s.stage, s.family, s.arm)


def _row(s: Session, **fields: Any) -> dict[str, Any]:
    return {"stage": s.stage, "family": s.family, "arm": s.arm, **fields}


def _kept(sessions: Iterable[Session], removed: Collection[str]) -> list[Session]:
    """The observations the review did not remove."""
    return [s for s in sessions if s.observation and s.job_id not in removed]


def strata(sessions: Iterable[Session], by: Callable[[Session], str | None],
           outcome: Callable[[str], bool], scenarios: Scenarios, *,
           removed: Collection[str] = frozenset()) -> dict[str, Rate]:
    """``rate`` per stratum ``by(session)``, in the strata's sorted order; a session whose
    stratum is None is left out."""
    groups: dict[str, list[Session]] = {}
    for s in sessions:
        stratum = by(s)
        if stratum is not None:
            groups.setdefault(stratum, []).append(s)
    return {stratum: rate(groups[stratum], outcome, scenarios, removed=removed)
            for stratum in sorted(groups)}


def load_levels(path: Path) -> dict[tuple[str, str], str]:
    """The evidence level of each B-oracle test in a levels file (a coder's or the adjudicated
    one), by (app, task) (analysis plan section 6)."""
    try:
        with Path(path).open(encoding="utf-8", newline="") as handle:
            return {(row["app"], row["task"]): row["level"] for row in csv.DictReader(handle)}
    except (OSError, KeyError) as exc:
        raise AnalysisRefused(f"{path}: cannot be read as evidence levels ({exc})") from exc


def evidence_agreement(folder: Path) -> dict[str, Any]:
    """The two coders' agreement on the evidence levels in ``folder`` (Cohen's kappa and raw
    agreement over the tests both coded) and how many levels the adjudication changed: where it
    differs from coder 1, from coder 2, and from both (analysis plan section 6)."""
    folder = Path(folder)
    one, two = load_levels(folder / "coder-1.csv"), load_levels(folder / "coder-2.csv")
    final = load_levels(folder / "adjudicated.csv")
    tests = sorted(set(one) & set(two))
    a, b = [one[t] for t in tests], [two[t] for t in tests]
    return {"n": len(tests),
            "raw": sum(x == y for x, y in zip(a, b, strict=True)) / len(tests) if tests else None,
            "kappa": cohen_kappa(a, b),
            "changed": {"from_coder_1": sum(final.get(t) != one[t] for t in tests),
                        "from_coder_2": sum(final.get(t) != two[t] for t in tests),
                        "from_both": sum(final.get(t) != one[t] and final.get(t) != two[t]
                                         for t in tests)}}


def _signature(entry: Any) -> str | None:
    """"final assertion: to_have_count", "step check: to_be_visible" or "step action: click":
    the kind of statement the red run stopped at and its matcher, without the step's number."""
    if not isinstance(entry, dict) or not entry.get("step"):
        return None
    step = str(entry["step"])
    kind = ("step check" if re.fullmatch(r"step \d+ check", step)
            else "step action" if re.fullmatch(r"step \d+", step) else step)
    return f"{kind}: {entry.get('call')}"


def load_signatures(results: Path, sessions: Iterable[Session]) -> dict[tuple[str, str, str], str]:
    """The failure signature of each scenario among the observations (analysis plan section 9):
    the step kind and matcher of the red run of the variant it hands out (``RED_VARIANT``) in
    the validation that admitted it (``variants[<variant>]["signature"]`` of its entry in
    ``validation.json``). That validation is the directory whose ``initial/`` the record's
    ``scenario.initial_results`` names, read under ``results`` whatever root the path was
    written with. A scenario that validation does not hold, or holds with no red run on that
    variant, refuses the run."""
    files: dict[str, Mapping[str, Any]] = {}
    found: dict[tuple[str, str, str], str] = {}
    for s in sessions:
        if not s.observation or s.scenario in found:
            continue
        path = ((s.record or {}).get("scenario") or {}).get("initial_results")
        parts = Path(path).parts if isinstance(path, str) else ()
        if "initial" not in parts[1:]:
            raise AnalysisRefused(f"{s.job_id}: its record names no starting test-results")
        name = parts[parts.index("initial", 1) - 1]
        if name not in files:
            data = _read_json(Path(results) / name / "validation.json")
            if not isinstance(data, dict):
                raise AnalysisRefused(f"{Path(results) / name / 'validation.json'}: missing")
            files[name] = data.get("scenarios") or {}
        entry = files[name].get("/".join(s.scenario))
        if not isinstance(entry, dict):
            raise AnalysisRefused(f"{'/'.join(s.scenario)} is not in {name}/validation.json")
        variant = (entry.get("variants") or {}).get(RED_VARIANT[s.family]) or {}
        signature = _signature(variant.get("signature"))
        if signature is None:
            raise AnalysisRefused(f"{'/'.join(s.scenario)} has no red run on "
                                  f"{RED_VARIANT[s.family]} in {name}/validation.json")
        found[s.scenario] = signature
    return found


def repetition_agreement(sessions: Iterable[Session], *,
                         removed: Collection[str] = frozenset()) -> list[dict[str, Any]]:
    """Design doc 5.8's histogram, per stage, family and arm: of the scenarios whose two
    repetitions are both observations the review kept, how many got the same label and how
    many different ones, with each pair of labels. Any other scenario is left out and
    counted."""
    groups: dict[tuple[str, ...], list[Session]] = {}
    for s in sessions:
        groups.setdefault((*_key(s), s.app, s.task), []).append(s)
    rows: dict[tuple[str, ...], dict[str, Any]] = {}
    for key in sorted(groups):
        group = groups[key]
        row = rows.setdefault(key[:3], _row(group[0], scenarios=0, same=0, different=0,
                                            pairs=Counter(), left_out=0))
        kept = _kept(group, removed)
        if len(group) != 2 or len(kept) != 2:
            row["left_out"] += 1
            continue
        first, second = sorted(s.label for s in kept)
        row["scenarios"] += 1
        row["same" if first == second else "different"] += 1
        row["pairs"][f"{first} | {second}"] += 1
    return [{**row, "pairs": dict(row["pairs"])} for row in rows.values()]


def pair_success_table(sessions: Iterable[Session], *,
                       removed: Collection[str] = frozenset()) -> list[dict[str, Any]]:
    """Per stage and arm, the pair success of each (C-rename, B-remove) pair of one test
    (``hoh.labels.pair_success``, design doc 5.7): the C-rename heal rate times the B-remove
    kept-red rate, each over the repetitions that are observations the review kept; and the
    mean over the pairs. A pair with no such observation on a side is left out and counted."""
    sides: dict[tuple[str, str], dict[tuple[str, str], dict[str, list[str]]]] = {}
    for s in sessions:
        if s.family not in PAIR:
            continue
        pair = sides.setdefault((s.stage, s.arm), {}).setdefault(
            (s.app, s.task), {family: [] for family in PAIR})
        if s.observation and s.job_id not in removed:
            pair[s.family].append(s.label)
    rows = []
    for (stage, arm), pairs in sorted(sides.items()):
        values: dict[str, float] = {}
        for (app, task), labels in sorted(pairs.items()):
            heal, kept = labels["c_rename"], labels["b_remove"]
            if heal and kept:
                values[f"{app}/{task}"] = pair_success(
                    sum(map(is_bug_preserving_heal, heal)) / len(heal),
                    sum(label == "kept_red" for label in kept) / len(kept))
        rows.append({"stage": stage, "arm": arm, "pairs": len(values),
                     "left_out": len(pairs) - len(values),
                     "mean": sum(values.values()) / len(values) if values else None,
                     "values": values})
    return rows


def mechanism_table(sessions: Iterable[Session], *,
                    removed: Collection[str] = frozenset()) -> list[dict[str, Any]]:
    """Per stage, family and arm, over the observations the review kept: the mechanism classes
    (design doc 5.9), how many sessions carry each flag, and the bypass values that are set."""
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for s in _kept(sessions, removed):
        row = rows.setdefault(_key(s), _row(s, observations=0, kinds=Counter(), flags=Counter(),
                                            bypass=Counter()))
        record = s.record or {}
        mechanism = record.get("mechanism") or {}
        row["observations"] += 1
        row["kinds"][str(mechanism.get("kind"))] += 1
        for flag in sorted(set(mechanism.get("flags") or ())):
            row["flags"][str(flag)] += 1
        if record.get("bypass") is not None:
            row["bypass"][json.dumps(record["bypass"], sort_keys=True)
                          if isinstance(record["bypass"], dict) else str(record["bypass"])] += 1
    return [{**row, "kinds": dict(row["kinds"]), "flags": dict(row["flags"]),
             "bypass": dict(row["bypass"])} for row in rows.values()]


def kept_red_split(sessions: Iterable[Session], *,
                   removed: Collection[str] = frozenset()) -> list[dict[str, Any]]:
    """Per stage, family and arm: the ``kept_red`` observations split by the record's
    ``unchanged`` (the returned tests equal the handed-out ones byte for byte, or not), in all
    and per subtype (analysis plan section 9)."""
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for s in _kept(sessions, removed):
        row = rows.setdefault(_key(s), _row(s, kept_red=0, unchanged=0, changed=0,
                                            by_subtype={}))
        if s.label != "kept_red":
            continue
        record = s.record or {}
        which = "unchanged" if record.get("unchanged") is True else "changed"
        row["kept_red"] += 1
        row[which] += 1
        row["by_subtype"].setdefault(str(record.get("subtype")),
                                     {"unchanged": 0, "changed": 0})[which] += 1
    return list(rows.values())


def _calls_before_first_edit(session: Session) -> tuple[int, bool]:
    """The ``run_test`` calls before the first edit among the transcript's tool calls, in order,
    and whether the session edited a file at all."""
    if session.attempt_dir is None:
        return 0, False
    try:
        lines = (session.attempt_dir / "out" / "stream.jsonl").read_text(
            encoding="utf-8").splitlines()
    except OSError:
        return 0, False
    calls = 0
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        for part in (event.get("message") or {}).get("content") or ():
            if isinstance(part, dict) and part.get("type") == "tool_use":
                if part.get("name") in EDITS:
                    return calls, True
                calls += part.get("name") == RUN_TEST
    return calls, False


def run_test_use(sessions: Iterable[Session], *,
                 removed: Collection[str] = frozenset()) -> list[dict[str, Any]]:
    """Per stage, family and arm, over the observations the review kept: each session's
    ``run_test`` calls (the record's ``run_test_calls``) as a distribution and a mean, and the
    calls made before the first edit, with the sessions that made one (a session with no edit
    has every call before one, and is counted)."""
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for s in _kept(sessions, removed):
        row = rows.setdefault(_key(s), _row(s, sessions=0, calls=Counter(), calls_total=0,
                                            before_first_edit={"calls": 0, "sessions": 0},
                                            without_edit=0))
        calls = int(((s.record or {}).get("session") or {}).get("run_test_calls") or 0)
        before, edited = _calls_before_first_edit(s)
        row["sessions"] += 1
        row["calls"][str(calls)] += 1
        row["calls_total"] += calls
        row["before_first_edit"]["calls"] += before
        row["before_first_edit"]["sessions"] += before > 0
        row["without_edit"] += not edited
    return [{**row, "calls": dict(row["calls"]),
             "calls_mean": row["calls_total"] / row["sessions"]} for row in rows.values()]


def _spread(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"sum": 0.0, "mean": None, "median": None, "max": None}
    return {"sum": sum(values), "mean": sum(values) / len(values),
            "median": statistics.median(values), "max": max(values)}


def cost_table(sessions: Iterable[Session], *,
               removed: Collection[str] = frozenset()) -> list[dict[str, Any]]:
    """Per stage, family and arm, over the observations the review kept: cost in USD, turns and
    the session's wall-clock seconds (sum, mean, median, largest). Arm G on bug scenarios is
    among them as a descriptive figure, not an efficiency result (analysis plan section 9)."""
    groups: dict[tuple[str, str, str], list[Session]] = {}
    for s in _kept(sessions, removed):
        groups.setdefault(_key(s), []).append(s)
    rows = []
    for group in groups.values():
        part = [(s.record or {}).get("session") or {} for s in group]
        cost = [p.get("cost") or {} for p in part]
        rows.append(_row(group[0], sessions=len(group),
                         cost_usd=_spread([float(c.get("total_cost_usd") or 0) for c in cost]),
                         turns=_spread([float(c.get("num_turns") or 0) for c in cost]),
                         wall_s=_spread([float(p.get("wall_s") or 0) for p in part])))
    return rows


def defect_codes(folder: Path) -> dict[str, Any]:
    """The gate of the figures from the defect codes (reported defects, false alarms, Silenced
    explained, fallback in G): they are read only from the committed ``docs/coding/<stage>/``,
    and only when its ``adjudication.json`` records the SHA-256 of the ``adjudicated.csv``
    beside it. Otherwise they are pending, with no number (``defect_figures``). ``key.json`` is
    not opened here: ``load_codes`` opens it once the gate says ready."""
    folder = Path(folder)
    adjudication = _read_json(folder / "adjudication.json")
    if not isinstance(adjudication, dict):
        return {"state": "pending", "why": "no adjudication.json"}
    recorded = (adjudication.get("sha256") or {}).get("adjudicated")
    if recorded is None or recorded != _sha256_of(folder / "adjudicated.csv"):
        return {"state": "pending",
                "why": "the SHA-256 of adjudicated.csv does not match adjudication.json"}
    return {"state": "ready", "why": None}


def _key_sessions(folder: Path) -> dict[str, Any]:
    """The sessions of ``key.json`` in ``folder``, each id the keyed hash of its job under the
    key's seed and each with its list of files, or AnalysisRefused."""
    key = _read_json(folder / "key.json")
    if not isinstance(key, dict) or not isinstance(key.get("sessions"), dict):
        raise AnalysisRefused(f"{folder / 'key.json'}: missing or without sessions")
    entries = key["sessions"]
    for sid, entry in entries.items():
        job_id = entry.get("job_id") if isinstance(entry, dict) else None
        if not isinstance(job_id, str) or sid != session_id_of(key.get("seed"), job_id):
            raise AnalysisRefused(f"{folder / 'key.json'}: {sid} is not the id of {job_id} "
                                  "under the key's seed")
        if not isinstance(entry.get("files"), list):
            raise AnalysisRefused(f"{folder / 'key.json'}: {sid} has no list of files")
    return entries


def load_codes(folder: Path, stage: Stage) -> dict[str, dict[str, str]]:
    """The adjudicated codes of ``stage`` in ``folder`` (``docs/coding/<stage>/``) by job id, as
    ``hoh.coding_export`` reads a coder's file: read only once ``defect_codes`` says ready. The
    run is refused when an id of ``key.json`` is not the keyed hash of its job under the key's
    seed, when the key's jobs are not exactly the stage's observations, when a key entry's label,
    arm or family is not the record's (codes exported from another judging), and when
    ``adjudicated.csv`` fails the coding tool's own check against the key. The frozen plan has
    no rule that would drop such a session, so the author decides."""
    folder = Path(folder)
    entries = _key_sessions(folder)
    observed = {s.job_id: s for s in stage.sessions if s.observation}
    keyed = {entry["job_id"]: sid for sid, entry in entries.items()}
    missing = sorted(set(observed) - set(keyed))
    if missing:
        raise AnalysisRefused(f"{folder}: {len(missing)} observations have no code in key.json "
                              f"({', '.join(missing[:3])})")
    extra = sorted(set(keyed) - set(observed))
    if extra:
        raise AnalysisRefused(f"{folder / 'key.json'} names {len(extra)} jobs that are no "
                              f"observation of stage {stage.name} ({', '.join(extra[:3])})")
    for job_id, sid in keyed.items():
        s = observed[job_id]
        for name, value in (("label", s.label), ("arm", s.arm), ("family", s.family)):
            if entries[sid].get(name) != value:
                raise AnalysisRefused(f"{folder / 'key.json'}: {job_id}: its {name} is "
                                      f"{entries[sid].get(name)!r} in the key and {value!r} in "
                                      "its record")
    try:
        codes = _read_codes(folder / "adjudicated.csv", entries)
    except (OSError, ValueError) as exc:
        raise AnalysisRefused(str(exc)) from exc
    return {entries[sid]["job_id"]: code for sid, code in codes.items()}


STEP_7 = ("key.json", "agreement.json", "adjudication.json", "adjudicated.csv")
"""The files of a coding folder that the coders' agreement is read from; step 7 of
``docs/coding/README.md`` commits them with the two coders' files (``codes-*.csv``)."""
DECISION_COLUMNS = ("session", "column", "code_a", "code_b", "adjudicated")
"""The columns of ``author-decisions.csv``, the author's decisions, which step 5 of
``docs/coding/README.md`` may write and step 7 then commits (``_check_decisions``)."""
AGREEMENT = ("n", "agreed", "raw", "kappa", *COUNTS)
"""A stage's figures per column (``coding_agreement``)."""
AGREEMENT_ALL = ("columns", "empty", "kappa_undefined", "cells", "agreed", "raw_min", "raw_max",
                 "kappa_min", "kappa_max", *COUNTS)
"""The methods' figures over the reviewed stages (``coding_agreement_all``)."""
SPREAD = ("n", "top_a", "share_a", "top_b", "share_b")
"""A stage's code spread per column (``coding_spread``)."""


@dataclass(frozen=True)
class _Coding:
    """A ready stage's coding folder, checked (``_coding``): the coders' names, the key's
    sessions, each coder's codes, and the agreement and the counts computed again."""

    names: Mapping[str, str]
    sessions: Mapping[str, Any]
    a: Mapping[str, Mapping[str, str]]
    b: Mapping[str, Mapping[str, str]]
    columns: Mapping[str, Mapping[str, Any]]
    counts: Mapping[str, Mapping[str, int]]


def _same(path: Path, recorded: Any, computed: Mapping[str, Mapping[str, Any]]) -> None:
    """Refuse the run where a figure that ``path`` records is not the one computed again."""
    for part, figures in computed.items():
        given = recorded.get(part) if isinstance(recorded, Mapping) else None
        for name, value in figures.items():
            found = given.get(name) if isinstance(given, Mapping) else None
            if found != value:
                raise AnalysisRefused(f"{path}: {part} {name} is {found}, but the committed "
                                      f"files give {value}")


def _check_decisions(path: Path, a: Mapping[str, Mapping[str, str]],
                     b: Mapping[str, Mapping[str, str]], final: Mapping[str, Mapping[str, str]],
                     sessions: Mapping[str, Any]) -> None:
    """Refuse the run where a committed ``author-decisions.csv`` is not the record of the
    adjudication: its rows are to be the cells on which the two coders' files differ, each once,
    with the codes the files give, and each decision the code that ``adjudicated.csv`` holds. A
    folder without the file passes, since step 5 lets the author fill ``adjudicated.csv``
    directly."""
    if not path.exists():
        return
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            header = [name.strip() for name in reader.fieldnames or []]
            reader.fieldnames = header
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise AnalysisRefused(f"{path}: {exc}") from exc
    if header != list(DECISION_COLUMNS):
        raise AnalysisRefused(f"{path}: the columns must be {','.join(DECISION_COLUMNS)}; found "
                              f"{','.join(header) or 'none'}")
    differ = {(sid, column) for sid, entry in sessions.items() for column in CODE_COLUMNS
              if COLUMN_FILE[column] in entry["files"] and a[sid][column] != b[sid][column]}
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for line, row in enumerate(rows, start=2):
        sid, column, *given = ((row.get(name) or "").strip() for name in DECISION_COLUMNS)
        code_a, code_b, decision = (value.lower() for value in given)
        if (sid, column) in seen:
            problems.append(f"line {line}: {sid} {column} is decided twice")
            continue
        seen.add((sid, column))
        if (sid, column) not in differ:
            problems.append(f"line {line}: the coders do not differ on {sid} {column}")
            continue
        if (code_a, code_b) != (a[sid][column], b[sid][column]):
            problems.append(f"line {line}: {sid} {column}: the codes are {code_a!r} and "
                            f"{code_b!r}, but the coders' files give {a[sid][column]!r} and "
                            f"{b[sid][column]!r}")
        if decision != final[sid][column]:
            problems.append(f"line {line}: {sid} {column} is decided {decision!r}, but "
                            f"adjudicated.csv holds {final[sid][column]!r}")
    missing = sorted(differ - seen)
    if missing:
        problems.append(f"{len(missing)} cells on which the coders differ are not decided: "
                        + ", ".join(f"{sid} {column}" for sid, column in missing))
    if problems:
        raise AnalysisRefused(f"{path}: " + "; ".join(problems))


def _coding(folder: Path) -> _Coding:
    """A ready stage's coding folder, once it holds together: every file of ``STEP_7`` is there;
    ``agreement.json`` and ``adjudication.json`` name the same two coders, and name them;
    ``adjudication.json`` records a different SHA-256 for each coder's file, and of the top-level
    ``codes-*.csv`` exactly one has coder A's, one has coder B's, and no other is there; the key
    (``_key_sessions``) and the three files of codes pass the coding tool's own check
    (``_read_codes``); no adjudicated code differs from the one both coders gave; and the
    agreement and the counts computed again from the two coders' files and ``adjudicated.csv``
    (``column_agreement``, ``adjudication_counts``) are the ones the two reports give; and a
    committed ``author-decisions.csv`` records the adjudication (``_check_decisions``).
    Otherwise the run is refused: the frozen plan has no rule for codes whose record does not
    hold together, so the author decides."""
    for name in STEP_7:
        if not (folder / name).is_file():
            raise AnalysisRefused(f"{folder}: a ready stage's folder lacks {name}, which step 7 "
                                  "of docs/coding/README.md commits")
    agreement = _read_json(folder / "agreement.json")
    adjudication = _read_json(folder / "adjudication.json")
    if not isinstance(agreement, dict) or not isinstance(adjudication, dict):
        raise AnalysisRefused(f"{folder}: agreement.json or adjudication.json is not a report")
    names = agreement.get("names")
    if names != adjudication.get("names"):
        raise AnalysisRefused(f"{folder}: agreement.json and adjudication.json name different "
                              f"coders ({names!r}, {adjudication.get('names')!r})")
    if not isinstance(names, dict) or not names.get("a") or not names.get("b"):
        raise AnalysisRefused(f"{folder / 'agreement.json'}: names no coders; the paper names "
                              "both models (analysis plan section 1)")
    recorded = adjudication.get("sha256") if isinstance(adjudication.get("sha256"), dict) else {}
    if recorded.get("codes_a") == recorded.get("codes_b"):
        raise AnalysisRefused(f"{folder / 'adjudication.json'}: one SHA-256 for both coders")
    found = sorted(folder.glob("codes-*.csv"))
    hashes = {path: _sha256_of(path) for path in found}
    coders = {}
    for part in ("codes_a", "codes_b"):
        matches = [path for path in found if hashes[path] == recorded.get(part)]
        if len(matches) != 1:
            raise AnalysisRefused(f"{folder}: {len(matches)} coder files (codes-*.csv) match the "
                                  f"SHA-256 that adjudication.json records for {part}; step 7 "
                                  "commits exactly one")
        coders[part] = matches[0]
    other = [path.name for path in found if path not in coders.values()]
    if other:
        raise AnalysisRefused(f"{folder}: {', '.join(other)} matches neither coder file that "
                              "adjudication.json records")
    sessions = _key_sessions(folder)
    try:
        a = _read_codes(coders["codes_a"], sessions)
        b = _read_codes(coders["codes_b"], sessions)
        final = _read_codes(folder / "adjudicated.csv", sessions)
    except (OSError, ValueError) as exc:
        raise AnalysisRefused(str(exc)) from exc
    columns = column_agreement(a, b, sessions)
    counts, problems = adjudication_counts(a, b, final, sessions)
    if problems:
        raise AnalysisRefused(f"{folder / 'adjudicated.csv'}: " + "; ".join(problems))
    _check_decisions(folder / "author-decisions.csv", a, b, final, sessions)
    _same(folder / "agreement.json", agreement.get("columns"), columns)
    total = {name: sum(column[name] for column in counts.values()) for name in COUNTS}
    _same(folder / "adjudication.json",
          {**(adjudication.get("columns") or {}), "total": adjudication.get("total")},
          {**counts, "total": total})
    return _Coding(names=names, sessions=sessions, a=a, b=b, columns=columns, counts=counts)


def coding_agreement(folder: Path, stage: str) -> list[dict[str, Any]]:
    """The two coders' agreement on the defect codes of a reviewed stage and what the
    adjudication changed (analysis plan section 1), a row per column of ``CODE_COLUMNS``:
    ``n`` (the sessions that have the column's text), ``agreed``, ``raw`` (raw agreement, None
    when n is 0), ``kappa`` (Cohen's kappa, None where it is not defined) and the counts of the
    coding README (``decided``, ``changed_a``, ``changed_b``, ``neither``), with the coders'
    names. They are computed again from the committed folder (``_coding``) once the stage's
    gate (``defect_codes``) says ready; until then the row has the gate's reason and no number,
    and nothing beyond what the gate reads is opened."""
    folder = Path(folder)
    gate = defect_codes(folder)
    if gate["state"] != "ready":
        return [{"stage": stage, "column": column, "coder_a": None, "coder_b": None,
                 **dict.fromkeys(AGREEMENT), **gate} for column in CODE_COLUMNS]
    coding = _coding(folder)
    return [{"stage": stage, "column": column, "coder_a": coding.names["a"],
             "coder_b": coding.names["b"], "n": coding.columns[column]["n"],
             "agreed": coding.columns[column]["agreed"],
             "raw": coding.columns[column]["raw_agreement"],
             "kappa": coding.columns[column]["kappa"], **coding.counts[column], **gate}
            for column in CODE_COLUMNS]


def coding_spread(folder: Path, stage: str) -> list[dict[str, Any]]:
    """Exploratory (the frozen plan does not list it): per column of a reviewed stage, each
    coder's most frequent code over the sessions that have the column's text (``top_a``,
    ``top_b``; of codes as frequent, the first in the alphabet) and the share of the column's
    cells it has (``share_a``, ``share_b``). It shows how a kappa near 0 comes with a high raw
    agreement: a coder who gave one code throughout. Read as ``coding_agreement`` reads."""
    folder = Path(folder)
    gate = defect_codes(folder)
    if gate["state"] != "ready":
        return [{"stage": stage, "column": column, **dict.fromkeys(SPREAD), **gate,
                 "exploratory": True} for column in CODE_COLUMNS]
    coding = _coding(folder)
    rows = []
    for column in CODE_COLUMNS:
        ids = [s for s in sorted(coding.sessions)
               if COLUMN_FILE[column] in coding.sessions[s]["files"]]
        row: dict[str, Any] = {"stage": stage, "column": column, "n": len(ids)}
        for part, codes in (("a", coding.a), ("b", coding.b)):
            spread = Counter(codes[s][column] for s in ids)
            top = min(spread, key=lambda code: (-spread[code], code)) if spread else None
            row[f"top_{part}"] = top
            row[f"share_{part}"] = spread[top] / len(ids) if ids else None
        rows.append({**row, **gate, "exploratory": True})
    return rows


def coding_agreement_all(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The figures of the methods' sentence on the coders (analysis plan section 1), over the
    rows of ``coding_agreement``: the number of stage columns, of those with no text
    (``empty``), which are left out of both ranges, and of those whose kappa is not defined
    (``kappa_undefined``), which are left out of the kappa range only (their raw agreement, 1,
    stays in its range); the cells and the agreed ones; the lowest and highest raw agreement and
    kappa; and the totals of the adjudication's counts. Pending, with the reason, unless every
    stage of ``REVIEWED`` is loaded and ready. Ready stages whose coders are not the same two
    models in the same order refuse the run, whether or not the other stages are ready yet: the
    paper names one pair of models for all, and coder A's and coder B's changed codes would
    otherwise be summed across roles."""
    ready = [row for row in rows if row["state"] == "ready"]
    coders = sorted({(row["coder_a"], row["coder_b"]) for row in ready})
    if len(coders) > 1:
        raise AnalysisRefused(f"the reviewed stages name different coders: {coders}")
    waiting: dict[str, str] = {}
    for row in rows:
        if row["state"] != "ready":
            waiting.setdefault(row["stage"], row["why"])
    loaded = {row["stage"] for row in rows}
    reasons = [f"{stage}: {waiting[stage]}" for stage in REVIEWED if stage in waiting]
    missing = [stage for stage in REVIEWED if stage not in loaded]
    if missing:
        reasons.append(f"{', '.join(missing)} not loaded")
    if reasons:
        return {"state": "pending", "why": "; ".join(reasons), "coder_a": None, "coder_b": None,
                **dict.fromkeys(AGREEMENT_ALL)}
    filled = [row for row in rows if row["n"]]
    kappas = [row["kappa"] for row in filled if row["kappa"] is not None]
    return {"state": "ready", "why": None, "coder_a": coders[0][0], "coder_b": coders[0][1],
            "columns": len(rows), "empty": len(rows) - len(filled),
            "kappa_undefined": len(filled) - len(kappas),
            "cells": sum(row["n"] for row in rows), "agreed": sum(row["agreed"] for row in rows),
            "raw_min": min((row["raw"] for row in filled), default=None),
            "raw_max": max((row["raw"] for row in filled), default=None),
            "kappa_min": min(kappas, default=None), "kappa_max": max(kappas, default=None),
            **{name: sum(row[name] for row in rows) for name in COUNTS}}


DELETED, EMPTIED, RENAMED = "deleted", "emptied", "renamed"
"""The mechanism flags (``hoh.mechanism.FLAG_DOC``) that set a Silenced session apart ("被删的另计",
design doc 5.7): ``deleted`` without ``renamed`` (the target is gone, and no test function stands
in its place), or ``emptied`` (with ``deleted``: its body is emptied by ``pass`` or an early
``return``). A target split into several test functions is ``deleted`` and ``renamed``; those
functions run (analysis plan section 1), so it is not set apart."""
TEXTS = ("final", "comments", "skip_reasons")
FALSE_ALARM_FAMILIES = ("c_rename", "c_assert")
"""The benign families, whose table in design doc 5.7 defines a false alarm; Mixed's application
has the bug."""
RED_LABELS = {"b_oracle": ("kept_red",), "b_remove": ("kept_red",), "c_rename": ("not_healed",),
              "c_assert": ("not_healed",), "mixed": ("correct_partial_heal", "not_healed")}
"""Per family, the labels of a session that kept the test red, which the fallback share counts
(analysis plan section 9): T' fails on the variant the agent was handed (``RED_VARIANT``) in
Kept red, Not healed and Correct partial heal ("bug 留红", design doc 5.7). Broken is not one: its
T' fails on A as well, so the test no longer tells the bug apart."""
LEVEL_FAMILIES = ("b_oracle", "mixed")
"""The families whose fallback share is also given per evidence level (analysis plan section 9,
"by evidence level"): the levels describe the benchmark bug M, which these families hand out
(A+M, A+C+M). B-remove's test is red from the removal, which the levels do not describe."""
DEFECT_TABLES = ("defects", "false_alarms", "silenced", "fallback_use", "fallback_codes")


def reported(codes: Mapping[str, str]) -> str:
    """A session's reported defect (analysis plan section 1): ``asserted`` when any of its texts
    is, else ``hedged`` when any is, else ``none`` (also a session with no text)."""
    found = {codes.get(text) for text in TEXTS}
    return "asserted" if "asserted" in found else "hedged" if "hedged" in found else "none"


def _explained(codes: Mapping[str, str]) -> str:
    """Silenced explained reads the new comments and the skip reasons only (section 1)."""
    found = {codes.get("comments"), codes.get("skip_reasons")}
    return ("explained" if "asserted" in found else "hedged" if "hedged" in found
            else "unexplained")


def _final(codes: Mapping[str, str]) -> str:
    """A false alarm reads the final message only (section 1)."""
    return codes.get("final") if codes.get("final") in ("asserted", "hedged") else "neither"


def _fell_back(codes: Mapping[str, str]) -> bool:
    return codes.get("fallback") == "yes"


def _deleted(s: Session) -> bool:
    flags = ((s.record or {}).get("mechanism") or {}).get("flags") or ()
    return EMPTIED in flags or (DELETED in flags and RENAMED not in flags)


def _emptied(s: Session) -> bool:
    return EMPTIED in (((s.record or {}).get("mechanism") or {}).get("flags") or ())


def _count(sessions: Iterable[Session], codes: Mapping[str, Mapping[str, str]] | None,
           test: Callable[[Mapping[str, str]], bool]) -> int | None:
    """How many of ``sessions`` have codes that pass ``test``; None while the codes are
    pending."""
    if codes is None:
        return None
    return sum(1 for s in sessions if test(codes[s.job_id]))


def _label_order(label: str) -> int:
    return LABELS.index(label) if label in LABELS else len(LABELS)


def defect_figures(stages: Iterable[Stage],
                   codes: Mapping[str, Mapping[str, Mapping[str, str]] | str], *,
                   levels: Mapping[tuple[str, str], str],
                   removed: Collection[str] = frozenset()) -> dict[str, list[dict[str, Any]]]:
    """The figures from the adjudicated defect codes (analysis plan sections 1 and 9), a table
    each, a row per stage, family and arm of the stages in ``codes``:

    - ``defects``: per label, the reported defect (``reported``);
    - ``false_alarms`` (C-rename and C-assert): the ``not_healed`` sessions by their final
      message's code;
    - ``silenced`` (every family): the ``silenced`` sessions, those whose target is deleted
      (``DELETED``) apart, with the ``emptied`` ones beside as a part of them, and the rest
      explained, hedged or unexplained;
    - ``fallback_use`` (every family), per ``level`` (``all``, and each evidence level in
      ``LEVEL_FAMILIES``): of the sessions of the arm, those that kept the test red
      (``RED_LABELS``) with a final message coded ``fallback`` (``share`` = ``fallback_red`` /
      ``sessions``);
    - ``fallback_codes``, exploratory (section 9 lists the share only): per label, how many final
      messages are coded ``fallback`` (``yes``).

    ``codes`` gives a stage's codes by job id (``load_codes``) or, while its gate is closed, the
    gate's reason: its rows then hold the counts that need no code, None for the others, and the
    reason (``why``). Each row's first count takes every observation; ``removed`` counts those the
    review removed, and the counts from codes, flags and labels are over the rest. In ``defects``,
    ``false_alarms`` and ``silenced`` those counts and ``removed`` add up to the first count, with
    ``emptied`` left out (a part of ``deleted``). In ``fallback_use`` ``sessions`` and ``removed``
    do; ``red`` and ``fallback_red`` are parts of ``sessions``. In ``fallback_codes``
    ``yes`` is a part of the rest."""
    tables: dict[str, list[dict[str, Any]]] = {name: [] for name in DEFECT_TABLES}
    for stage in stages:
        if stage.name not in codes:
            continue
        given = codes[stage.name]
        why = given if isinstance(given, str) else None
        coded = None if isinstance(given, str) else given
        state = {"state": "pending" if coded is None else "ready", "why": why}
        groups: dict[tuple[str, str], list[Session]] = {}
        for s in stage.sessions:
            if s.observation:
                groups.setdefault((s.family, s.arm), []).append(s)
        for (family, arm), group in groups.items():
            base = {"stage": stage.name, "family": family, "arm": arm}
            by_label: dict[str, list[Session]] = {}
            for s in group:
                by_label.setdefault(str(s.label), []).append(s)
            for label in sorted(by_label, key=_label_order):
                every = by_label[label]
                kept = _kept(every, removed)
                tables["defects"].append({
                    **base, "label": label, "observations": len(every),
                    **{name: _count(kept, coded, lambda c, name=name: reported(c) == name)
                       for name in ("asserted", "hedged", "none")},
                    "removed": len(every) - len(kept), **state})
                tables["fallback_codes"].append({
                    **base, "label": label, "observations": len(every),
                    "yes": _count(kept, coded, _fell_back), "removed": len(every) - len(kept),
                    **state, "exploratory": True})
            if family in FALSE_ALARM_FAMILIES:
                every = [s for s in group if s.label == "not_healed"]
                kept = _kept(every, removed)
                tables["false_alarms"].append({
                    **base, "not_healed": len(every),
                    **{name: _count(kept, coded, lambda c, name=name: _final(c) == name)
                       for name in ("asserted", "hedged", "neither")},
                    "removed": len(every) - len(kept), **state})
            every = [s for s in group if s.label == "silenced"]
            kept = _kept(every, removed)
            rest = [s for s in kept if not _deleted(s)]
            tables["silenced"].append({
                **base, "silenced": len(every), "deleted": len(kept) - len(rest),
                "emptied": sum(1 for s in kept if _emptied(s)),
                **{name: _count(rest, coded, lambda c, name=name: _explained(c) == name)
                   for name in ("explained", "hedged", "unexplained")},
                "removed": len(every) - len(kept), **state})
            parts = {"all": group}
            if family in LEVEL_FAMILIES:
                for level in sorted({levels.get((s.app, s.task)) for s in group} - {None}):
                    parts[level] = [s for s in group if levels.get((s.app, s.task)) == level]
            for level, part in parts.items():
                kept = _kept(part, removed)
                red = [s for s in kept if s.label in RED_LABELS[family]]
                fell = _count(red, coded, _fell_back)
                tables["fallback_use"].append({
                    **base, "level": level, "observations": len(part), "sessions": len(kept),
                    "red": len(red), "fallback_red": fell,
                    "share": None if fell is None or not kept else fell / len(kept),
                    "removed": len(part) - len(kept), **state})
    return tables


def descriptive_figures(stages: Iterable[Stage], scenarios: Scenarios, *,
                        review: Review | None = None) -> list[dict[str, Any]]:
    """Mixed's false-heal rate per arm (descriptive, analysis plan section 9), and the
    exploratory arms beside H's repetition 1 of stage 1a on the same scenarios (exploratory),
    each on all clusters and outside the dev split, as ``comparisons`` writes a figure."""
    stages = list(stages)
    by_name = {stage.name: stage for stage in stages}
    review = review if review is not None else load_review(stages, None)
    figures: list[dict[str, Any]] = []

    def add(group: str, arm: str, names: list[list[str]], sessions: list[Session],
            role: str) -> None:
        for split in SPLITS:
            taken = outside_dev(sessions, scenarios) if split == "outside_dev" else sessions
            result = rate(taken, is_false_heal, scenarios, removed=review.removed)
            figures.append({
                "id": f"{group}/{arm}/{split}", "comparison": None, "arm": arm, "group": group,
                "split": split, "sessions": names, "role": role,
                "exploratory": role == "exploratory", "decision": None,
                "partial": any(by_name[n[0]].partial for n in names),
                "review": review.mark(s.job_id for s in taken),
                "fallback": _fallback(taken, review.removed), "missing": [],
                "result": asdict(result)})

    stage, family = MIXED
    if stage in by_name:
        for arm in ("N", "H", "G"):
            add("mixed", arm, [[stage, family, arm]],
                [s for s in by_name[stage].sessions if s.family == family and s.arm == arm],
                "descriptive")
    if EXPLORE in by_name:
        explored = {s.scenario for s in by_name[EXPLORE].sessions}
        for arm in EXPLORE_ARMS:
            add("explore", arm, [[EXPLORE, "b_oracle", arm]],
                [s for s in by_name[EXPLORE].sessions if s.arm == arm], "exploratory")
        if "1a" in by_name:
            add("explore", "H_rep1", [["1a", "b_oracle", "H"]],
                [s for s in by_name["1a"].sessions
                 if s.arm == "H" and s.rep == 1 and s.scenario in explored], "exploratory")
    return figures


def descriptives(stages: Sequence[Stage], scenarios: Scenarios, *, results: Path, repo: Path,
                 review: Review) -> dict[str, Any]:
    """Every descriptive figure of analysis plan section 9 and design doc 5.8, over the stages
    given; the defect-code figures (``defect_figures``) and the coders' agreement
    (``coding_agreement``, ``coding_agreement_all``, the exploratory ``coding_spread``) are
    behind each stage's gate (``defect_codes``): a stage's codes are read (``load_codes``) only
    once it says ready."""
    removed = review.removed
    by_name = {stage.name: stage for stage in stages}
    sessions = [s for stage in stages for s in stage.sessions]
    levels = load_levels(repo / "scenarios" / "evidence-levels" / "adjudicated.csv")
    signatures = load_signatures(results, sessions)
    folders = {stage.name: repo / "docs" / "coding" / stage.name
               for stage in stages if stage.name in REVIEWED}
    gates = {name: defect_codes(folder) for name, folder in folders.items()}
    codes = {name: load_codes(folders[name], by_name[name])
             if gate["state"] == "ready" else gate["why"] for name, gate in gates.items()}
    agreement = [row for name, folder in folders.items()
                 for row in coding_agreement(folder, name)]
    rows = []
    for stage, family in (*BUG_PARTS, *BENIGN_PARTS, MIXED):
        if stage not in by_name:
            continue
        benign = (stage, family) in BENIGN_PARTS
        outcome, named = ((is_bug_preserving_heal, "bug_preserving_heal") if benign
                          else (is_false_heal, "false_heal"))
        bys: dict[str, Callable[[Session], str | None]] = {
            "app": lambda s: s.app, "signature": lambda s: signatures.get(s.scenario)}
        if family == "b_oracle":
            bys["evidence_level"] = lambda s: levels.get((s.app, s.task))
        for arm in ("N", "H", "G"):
            taken = [s for s in by_name[stage].sessions if s.family == family and s.arm == arm]
            for by, key in bys.items():
                rows.append({"stage": stage, "family": family, "arm": arm, "by": by,
                             "outcome": named, "review": review.mark(s.job_id for s in taken),
                             "strata": {k: asdict(v) for k, v in strata(
                                 taken, key, outcome, scenarios, removed=removed).items()}})
    return {
        "strata": rows,
        "repetitions": repetition_agreement(sessions, removed=removed),
        "pair_success": pair_success_table(sessions, removed=removed),
        "mechanisms": mechanism_table(sessions, removed=removed),
        "kept_red": kept_red_split(sessions, removed=removed),
        "run_test": run_test_use(sessions, removed=removed),
        "cost": cost_table(sessions, removed=removed),
        "evidence_agreement": evidence_agreement(repo / "scenarios" / "evidence-levels"),
        "defect_codes": gates,
        "coding_agreement": agreement,
        "coding_agreement_all": coding_agreement_all(agreement),
        "coding_spread": [row for name, folder in folders.items()
                          for row in coding_spread(folder, name)],
        **defect_figures(stages, codes, levels=levels, removed=removed),
        "figures": descriptive_figures(stages, scenarios, review=review),
    }


# --- the run: inputs, provenance and outputs (python -m hoh.analysis)

REPO = Path(__file__).resolve().parents[2]
FROZEN_INPUTS = {
    "scenarios/selection.json": "09203aa78a559eb14e2226ab57b1da8668d80557",
    "scenarios/evidence-levels/coder-1.csv": "3d102912a3c5ca86d5237ccca3e2361809c09ed0",
    "scenarios/evidence-levels/coder-2.csv": "2ed6adf9eb90fc569c68bd00f3f0ae640372447b",
    "scenarios/evidence-levels/adjudicated.csv": "bdb40bc7b7c3da308a29e28f22aad98b6382b6b0",
    "src/hoh/stats.py": "cbc3033fd225f5d0c5101d1302c779d1134a37ec",
    "src/hoh/labels.py": "2e16557e2f6ca96f6c35d2be068f23d350aae8b1",
}
"""The analysis plan's git objects of the frozen files this code reads or computes with."""
WATCHED = ("src", "scenarios", "docs/review", "docs/coding", "uv.lock", "pyproject.toml")
"""A change under these, not committed, makes the run's provenance dirty: the code, its inputs,
the decisions and codes it reads, and the locked dependencies it computes with."""
READINGS = (
    "Comparison 2 pools B-oracle and B-remove (the bug scenarios); Mixed is descriptive.",
    "A missing session drops its scenario from both arms of comparisons 2 and 3, for every "
    "cause: a non-observation, a session the review removed, or a repetition one arm lacks.",
    "Comparisons 2 and 3 pair repetition r of G with repetition r of H; the interval and the "
    "sign-flip test work on cluster totals, so the pairing within a scenario does not change "
    "them.",
    "The decisions of comparisons 2 and 3 are taken outside the dev split, on the families "
    "each comparison takes pooled: comparison 2 on B-oracle and B-remove (with one of them "
    "loaded it takes none), comparison 3 on C-rename and, once its stage is loaded, C-assert. "
    "A family's own difference beside a pooled one is exploratory.",
    "env_failed and judge_failed are unfinished, not final: the harness runs or judges them "
    "again.",
    "A rate of 0 is given with the upper limit of the 95% Wilson interval over clusters; a "
    "stratum with one cluster has its rate and n and no interval.",
    "A failure signature is the kind of statement the red run of the handed-out variant stopped "
    "at in the validation (the final assertion, a step's check or a step's action) with its "
    "matcher; the step's number is not part of it, nor is the exception type.",
    "Pair success takes each side's rate over the repetitions that are observations, and leaves "
    "out a pair with no observation on a side.",
    "Outside the dev split, the sessions of the dev tests are left out: a dev cluster of section "
    "4 is a cluster of the selection file, whose chosen test is the dev test. A cluster of "
    "section 2 that also holds other tests keeps them.",
    "A label is a cluster within its app: in C-rename, B-remove and C-assert the same rewritten "
    "label or text in two apps is two clusters.",
    "The fallback share counts, as the sessions that kept the test red, those whose T' fails on "
    "the variant the agent was handed: Kept red in B-oracle and B-remove, Not healed in "
    "C-rename and C-assert, Correct partial heal and Not healed in Mixed. Broken is not counted: "
    "its T' fails on A as well. The fallback codes of every label are counted beside it, as "
    "exploratory figures. The share is also given per evidence level in B-oracle and Mixed, "
    "whose variants have the benchmark bug the levels describe; B-remove's test is red from "
    "the removal.",
    "A Silenced session whose target is gone with no test function in its place, or whose body "
    "is emptied by pass or an early return, is counted apart, whatever its codes; the emptied "
    "ones are given as a part of it. A target split into several test functions runs, and is "
    "split by its codes.",
    "Silenced explained versus unexplained is given in every family, as section 9 lists it, "
    "though the judging tables of design doc 5.7 state the split for the bug scenarios only. "
    "In Mixed the application has the bug, as in the bug scenarios; in C-rename and C-assert "
    "an explained session names a defect the application does not have.",
    "False alarms are counted in C-rename and C-assert, whose table defines them; a Mixed "
    "session that reports a defect is not one, since its application has the bug.",
    "The coders' agreement on the defect codes is given per stage and per column, over every "
    "session the coders coded, those the review removed included. n is the number of sessions "
    "that have the column's text (fallback goes with the final message). A column with no text "
    "is left out of both ranges and counted. A kappa that is not defined (one code throughout "
    "for both coders) is left out of the kappa range and counted; its column's raw agreement, "
    "1, stays in the raw agreement range. A changed code is a decided cell whose adjudicated "
    "code is not that coder's (changed_a, changed_b); neither counts those that are neither "
    "coder's. The ranges for the methods wait for all four reviewed stages.",
)
"""Readings of the frozen plan made after stages 1a and 1b existed; the paper states them."""


def git_object(path: Path) -> str | None:
    """The git blob object of a file's content, as ``git hash-object`` gives it."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _git(repo: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                              check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return done.stdout


def _records_sha256(stage: Stage) -> str:
    lines = []
    for s in sorted(stage.sessions, key=lambda s: s.job_id):
        record = _sha256_of(s.attempt_dir / RECORD) if s.attempt_dir is not None else None
        lines.append(f"{s.job_id} {record or '-'}\n")
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def provenance(results: Path, stages: Sequence[Stage], repo: Path) -> dict[str, Any]:
    """What the numbers come from: per stage its jobs, the SHA-256 of its summary and of its
    job records, and whether it is partial; each decision file read, and the file of refused
    attempts (``load_refused``); per reviewed stage the
    gate of its defect codes (``defect_codes``) with the SHA-256 of ``adjudication.json``,
    ``adjudicated.csv``, ``key.json`` and ``agreement.json``; the frozen inputs against their
    pins; the checkout's commit, whether a change under ``WATCHED`` is uncommitted, and the
    version of numpy."""
    commit = _git(repo, "rev-parse", "HEAD")
    status = _git(repo, "status", "--porcelain", "--", *WATCHED)
    review, coding = {}, {}
    for stage in stages:
        if stage.name in REVIEWED:
            path = repo / "docs" / "review" / f"{stage.name}.csv"
            review[stage.name] = {"path": f"docs/review/{stage.name}.csv",
                                  "sha256": _sha256_of(path)}
            folder = repo / "docs" / "coding" / stage.name
            coding[stage.name] = {
                "state": defect_codes(folder)["state"],
                **{name: _sha256_of(folder / name)
                   for name in ("adjudication.json", "adjudicated.csv", "key.json",
                                "agreement.json")}}
    return {
        "stages": {stage.name: {"jobs": len(stage.sessions), "partial": stage.partial,
                                "stage_summary_sha256": _sha256_of(results / stage.name / SUMMARY),
                                "records_sha256": _records_sha256(stage)} for stage in stages},
        "review": review,
        "refused_attempts": {"path": f"docs/review/{REFUSED}",
                             "sha256": _sha256_of(repo / "docs" / "review" / REFUSED)},
        "coding": coding,
        "inputs": {path: {"object": git_object(repo / path), "pinned": pinned,
                          "equal": git_object(repo / path) == pinned}
                   for path, pinned in FROZEN_INPUTS.items()},
        "code": {"commit": commit.strip() if commit else None,
                 "dirty": status is None or bool(status.strip()),
                 "numpy": numpy.__version__},
    }


def _write_json(path: Path, data: Mapping[str, Any], sha: str) -> None:
    """JSON whose first line names the provenance's SHA-256."""
    body = json.dumps(data, indent=1, sort_keys=True)
    path.write_text(f'{{"provenance_sha256": "{sha}",' + body[1:] + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], columns: Sequence[str],
               sha: str, partial: Sequence[str] = ()) -> None:
    """CSV whose first line names the provenance's SHA-256 and, in a partial run, whose second
    names the partial stages."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(f"# provenance.json sha256 {sha}\n")
        if partial:
            handle.write(f"# partial: {', '.join(partial)}\n")
        writer = csv.DictWriter(handle, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


WORKSHEET_COLUMNS = ("job_id", "stage", "family", "app", "task", "arm", "rep", "label", "subtype",
                     "reasons", "needs_review", "leaks", "transcript", "original_tests",
                     "returned_tests", "snapshots")


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    return value


def _write_table(path: Path, rows: Sequence[Mapping[str, Any]], sha: str,
                 partial: Sequence[str] = ()) -> None:
    """One table of ``analysis.json`` as CSV: its columns are the rows' keys in the order they
    first appear, and a nested value is written as JSON."""
    columns = list(dict.fromkeys(key for row in rows for key in row))
    _write_csv(path, [{key: _cell(row.get(key)) for key in columns} for row in rows], columns,
               sha, partial)


def _figure_row(figure: Mapping[str, Any]) -> dict[str, Any]:
    return {**{k: v for k, v in figure.items() if k != "result"}, **figure["result"]}


def _strata_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{**{k: v for k, v in row.items() if k != "strata"}, "stratum": name, **result}
            for row in rows for name, result in row["strata"].items()]


def _pilot_rows(pilot: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"stage": stage, "arm": arm, "value_of": key, "value": value, "observations": count,
             "as_in_the_pilot": row["as_in_the_pilot"]}
            for stage, arms in pilot.items() for arm, row in arms.items()
            for key, counts in row["values"].items() for value, count in counts.items()]


# --- the paper's files (the plan's step 18): paper/generated, regenerated, never edited

FAMILY_NAMES = {"b_oracle": "B-oracle", "b_remove": "B-remove", "c_rename": "C-rename",
                "mixed": "Mixed", "c_assert": "C-assert", "pooled": "pooled"}
ARM_HEADS = {"N": r"\armN", "H": r"\armH", "G": r"\armG", "G-H": r"\armG{}$-$\armH"}
SPLIT_NAMES = {"all": "all", "outside_dev": "outside dev"}
TABLE_PREFIXES = {"labels": "labels", "section_8": "s8", "repetitions": "reps",
                  "pair_success": "pairs", "mechanisms": "mech", "kept_red": "keptred",
                  "run_test": "runtest", "cost": "cost", "strata": "strata",
                  "defects": "defects", "false_alarms": "falsealarms", "silenced": "silenced",
                  "fallback_use": "fallbackuse", "fallback_codes": "fallbackcodes"}
"""The first part of the id of each table's numbers in ``numbers.tex``; the rest is the row's
stage, family, arm and ``by`` (or ``label``, or ``level``), then the field."""
_KEYS = ("stage", "family", "arm", "by", "label", "level")
PERCENT = {"fallback_use": ("share",)}
"""The table fields ``numbers.tex`` gives in percent with one decimal, as the rates."""
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_.+-]+")


def provisional(origin: Mapping[str, Any], *, partial: bool, review: str | None,
                missing: Sequence[str] = (), fallback: int = 0) -> list[str]:
    """Why a number is not final yet, as the paper's ``\\todo`` names it: the review of its
    sessions not done (``Review.mark``), a stage its comparison takes that the run did not load
    (``missing``), sessions under the fallback credential while the figure without them is not
    computed (``fallback``), a partial stage, a change under ``WATCHED`` not committed, or a
    frozen input that is not the pinned one."""
    marks = [review] if review not in (None, "reviewed") else []
    if missing:
        marks.append(f"{', '.join(missing)} not loaded")
    if fallback:
        marks.append(f"{fallback} fallback session{'s' if fallback > 1 else ''}")
    if partial:
        marks.append("partial")
    if origin["code"]["dirty"]:
        marks.append("dirty tree")
    if not all(item["equal"] for item in origin["inputs"].values()):
        marks.append("inputs differ from the pins")
    return marks


def _tex(text: str) -> str:
    return re.sub(r"([_%&#$])", r"\\\1", text)


def _marked(text: str, marks: Sequence[str]) -> str:
    """``text``, or ``\\todo{<marks>: <text>}`` when a mark says it is not final."""
    if not marks:
        return text
    return f"\\todo{{{_tex('; '.join(marks))}{': ' + text if text else ''}}}"


_MARK_ORDER = ("review", "not loaded", "fallback", "partial", "dirty tree",
               "inputs differ from the pins")
"""The order ``provisional`` gives its marks in, by a part of each mark's words."""


def _combined(lists: Iterable[Sequence[str]]) -> list[str]:
    """The marks of several numbers, once each, in the order ``provisional`` gives them."""
    found = {mark for marks in lists for mark in marks}

    def rank(mark: str) -> int:
        return next((i for i, part in enumerate(_MARK_ORDER) if part in mark), len(_MARK_ORDER))

    return sorted(found, key=lambda mark: (rank(mark), mark))


def _decimal(value: float, places: int = 1) -> str:
    text = f"{value:.{places}f}"
    if float(text) == 0:
        text = text.lstrip("-")
    return f"\\ensuremath{{-}}{text[1:]}" if text.startswith("-") else text


def _percent(value: float) -> str:
    return _decimal(100 * value)


def _amount(value: int | float) -> str:
    if isinstance(value, int):
        return _decimal(value, 0)
    return _decimal(value, 2 if abs(value) < 10 else 1 if abs(value) < 100 else 0)


def _result_numbers(result: Mapping[str, Any]) -> dict[str, str]:
    """The numbers of a rate (``n``, ``k``, ``rate``) or of a paired difference (``pairs``,
    ``diff``), rates and differences in percent: the interval (``lo``, ``hi``), the bootstrap
    (``bootlo``, ``boothi``), the indeterminate bounds (``boundlo``, ``boundhi``), a zero rate's
    Wilson upper limit (``wilson``), comparison 2's ``p``."""
    paired = "pairs" in result
    counts = ("pairs", "scenarios", "clusters") if paired else ("n", "k", "clusters",
                                                               "indeterminate")
    numbers = {name: str(result[name]) for name in counts}
    estimate = result["difference"] if paired else result["rate"]
    if estimate is not None:
        numbers["diff" if paired else "rate"] = _percent(estimate)
    for prefix, pair in (("", result.get("interval")), ("boot", result.get("bootstrap")),
                         ("bound", result.get("bounds"))):
        if pair is not None:
            numbers[f"{prefix}lo"], numbers[f"{prefix}hi"] = _percent(pair[0]), _percent(pair[1])
    if result.get("wilson_upper") is not None:
        numbers["wilson"] = _percent(result["wilson_upper"])
    if paired:
        numbers["dropped"] = str(len(result["dropped"]))
        p_value = result.get("sign_flip_p")
        if p_value is not None:
            numbers["p"] = r"\ensuremath{<}0.001" if p_value < 0.001 else f"{p_value:.3f}"
    return numbers


def _flat(prefix: str, value: Any) -> Iterable[tuple[str, str]]:
    if isinstance(value, (bool, str)) or value is None:
        return
    if isinstance(value, (int, float)):
        yield prefix, _amount(value)
    elif isinstance(value, Mapping):
        if {"k", "rate", "interval"} <= set(value):  # a Rate
            for name, text in _result_numbers(value).items():
                yield f"{prefix}/{name}", text
            return
        for key, item in value.items():
            yield from _flat(f"{prefix}/{_ID_UNSAFE.sub('-', str(key)).strip('-')}", item)


@dataclass(frozen=True)
class _Paper:
    """What the paper's files need besides the numbers: the provenance, its SHA-256, and the
    review mark and partial state of the sessions of a stage, family and arm."""

    origin: Mapping[str, Any]
    sha: str
    stages: Sequence[Stage]
    review: Review

    def marks(self, stage: str, family: str | None = None, arm: str | None = None, *,
              fallback: bool = False) -> list[str]:
        """A table row's marks. Given ``fallback``, the row's fallback sessions mark it too: a
        strata row is a comparison's outcome over part of its sessions (comparison 1 per
        evidence level, analysis plan section 6), marked as its comparison is."""
        sessions = [s for st in self.stages if st.name == stage for s in st.sessions
                    if family in (None, s.family) and arm in (None, s.arm)]
        partial = any(st.partial for st in self.stages if st.name == stage)
        return provisional(self.origin, partial=partial,
                           review=self.review.mark(s.job_id for s in sessions),
                           fallback=_fallback(sessions, self.review.removed) if fallback else 0)

    def figure_marks(self, figure: Mapping[str, Any]) -> list[str]:
        """A figure's marks. Its fallback sessions mark a figure of a comparison that is not
        exploratory, which the plan reports with and without them (section 7)."""
        compared = figure["role"] in ("primary", "secondary")
        return provisional(self.origin, partial=figure["partial"], review=figure["review"],
                           missing=figure["missing"],
                           fallback=figure["fallback"] if compared else 0)

    def head(self) -> str:
        return f"% provenance.json sha256 {self.sha}\n"


def _numbers(paper: _Paper, figures: Sequence[Mapping[str, Any]],
             tables: Mapping[str, Sequence[Mapping[str, Any]]],
             described: Mapping[str, Any]) -> dict[str, str]:
    """Every number as ``{id: text}``: each figure's under its id, each table row's under the
    table's prefix and the row's keys, the evidence levels' agreement under ``levels``, under
    ``codes/<stage>`` a mark for the defect-code figures of each reviewed stage: pending
    while its gate is closed, empty (with the run's own marks) once they are computed; and the
    defect coders' agreement under ``agree/<stage>/<column>`` and ``agree/all``, their code
    spread under ``agreespread/<stage>/<column>``: raw agreement and shares in percent, a value
    that is not defined as ``--``, and each number of a pending row as ``\\todo{pending:
    ...}``. A row
    of a pending stage (``defect_figures``) gives each number it lacks as ``\\todo{pending:
    ...}``; a field of ``PERCENT`` is in percent, and in a ready row over no session (the
    review removed them all) it is ``\\todo{no session}``. Two numbers of one id refuse the
    run."""
    numbers: dict[str, str] = {}

    def add(name: str, text: str, marks: Sequence[str]) -> None:
        if name in numbers:
            raise AnalysisRefused(f"two numbers are named {name}")
        numbers[name] = _marked(text, marks)

    for figure in figures:
        marks = paper.figure_marks(figure)
        for name, text in _result_numbers(figure["result"]).items():
            add(f"{figure['id']}/{name}", text, marks)
        if figure["decision"] is not None:
            add(f"{figure['id']}/decision", _tex(figure["decision"]), marks)
    for table, rows in tables.items():
        for row in rows:
            keys = [str(row[key]) for key in _KEYS if key in row]
            marks = paper.marks(row["stage"], row.get("family"), row.get("arm"),
                                fallback=table == "strata")
            rest = {k: v for k, v in row.items() if k not in _KEYS}
            prefix = "/".join([TABLE_PREFIXES[table], *keys])
            if row.get("state") == "pending":
                for name, value in rest.items():
                    if value is None:
                        add(f"{prefix}/{name}", "", [f"pending: {row['why']}"])
            for name in PERCENT.get(table, ()):
                value = rest.pop(name, None)
                if value is not None:
                    add(f"{prefix}/{name}", _percent(value), marks)
                elif row.get("state") != "pending":  # the review left no session
                    add(f"{prefix}/{name}", "", ["no session"])
            for name, text in _flat(prefix, rest):
                add(name, text, marks)
    clean = provisional(paper.origin, partial=False, review=None)
    for name, text in _flat("levels", described["evidence_agreement"]):
        add(name, text, clean)
    for stage, state in described["defect_codes"].items():
        add(f"codes/{stage}", "", [f"pending: {state['why']}"] if state["state"] == "pending"
            else clean)

    def coding(prefix: str, row: Mapping[str, Any], fields: Sequence[str],
               percent: Collection[str]) -> None:
        for name in fields:
            value = row[name]
            if row["state"] == "pending":
                add(f"{prefix}/{name}", "", [f"pending: {row['why']}"])
            elif value is None:
                add(f"{prefix}/{name}", "--", clean)
            elif isinstance(value, str):
                add(f"{prefix}/{name}", _tex(value), clean)
            else:
                add(f"{prefix}/{name}", _percent(value) if name in percent else _amount(value),
                    clean)

    for row in described["coding_agreement"]:
        coding(f"agree/{row['stage']}/{row['column']}", row, AGREEMENT, ("raw",))
    coding("agree/all", described["coding_agreement_all"], AGREEMENT_ALL, ("raw_min", "raw_max"))
    for row in described["coding_spread"]:
        coding(f"agreespread/{row['stage']}/{row['column']}", row, SPREAD,
               ("share_a", "share_b"))
    return numbers


def _numbers_tex(paper: _Paper, numbers: Mapping[str, str]) -> str:
    return "".join([
        paper.head(),
        "% Written by python -m hoh.analysis (just analyze): regenerated, never edited.\n",
        "% \\hohnum{<id>} prints a number; build/analysis/analysis.json holds every figure.\n",
        "\\makeatletter\n",
        "\\providecommand{\\hohnum}[1]{\\@ifundefined{hoh@#1}%\n",
        "  {\\@latex@warning{Number `#1' undefined}\\textbf{??}}{\\@nameuse{hoh@#1}}}\n",
        *(f"\\@namedef{{hoh@{name}}}{{{text}}}\n" for name, text in numbers.items()),
        "\\makeatother\n"])


def _tex_row(cells: Iterable[str]) -> str:
    return " & ".join(cells) + " \\\\\n"


def _tabular(paper: _Paper, what: str, columns: str, heads: Sequence[str],
             rows: Iterable[str]) -> str:
    """A whole ``tabular``, so that the paper inputs it inside its own ``table`` float (an
    ``\\input`` between the rows of a tabular can break on the rule after it)."""
    return "".join([paper.head(), f"% {what}. Regenerated by python -m hoh.analysis, never "
                    "edited.\n", f"\\begin{{tabular}}{{{columns}}}\n", "\\toprule\n",
                    _tex_row(heads), "\\midrule\n", *rows, "\\bottomrule\n", "\\end{tabular}\n"])


def _table_labels(paper: _Paper, rows: Sequence[Mapping[str, Any]]) -> str:
    """The labels table (analysis plan section 1) of the reviewed stages: stage, family, arm,
    the observations, one column per label that occurs, and the observations the review
    removed. The exploratory stage's rows are in ``analysis.json`` and ``labels.csv`` only."""
    rows = [row for row in rows if row["stage"] in REVIEWED]
    occurring = [label for label in LABELS if any(row["labels"].get(label) for row in rows)]

    def line(row: Mapping[str, Any]) -> str:
        marks = paper.marks(row["stage"], row["family"], row["arm"])
        counts = (row["observations"], *(row["labels"].get(label, 0) for label in occurring),
                  row["removed"])
        return _tex_row([_tex(row["stage"]), FAMILY_NAMES.get(row["family"], _tex(row["family"])),
                     ARM_HEADS.get(row["arm"], _tex(row["arm"])),
                     *(_marked(str(count), marks) for count in counts)])

    return _tabular(paper, "The labels table", "lll" + "r" * (len(occurring) + 2),
                    ["Stage", "Family", "Arm", "$n$", *(_tex(label) for label in occurring),
                     "Removed"], [line(row) for row in rows])


def _table_comparisons(paper: _Paper, figures: Sequence[Mapping[str, Any]]) -> str:
    """The figures of comparisons 1 to 3 that are not exploratory: n (pairs for a difference),
    clusters, the estimate and its 95% interval in percent (points), and the decision."""

    def line(figure: Mapping[str, Any]) -> str:
        marks, numbers = paper.figure_marks(figure), _result_numbers(figure["result"])
        paired = "pairs" in figure["result"]
        interval = ("--" if "lo" not in numbers else f"0.0 to {numbers['wilson']}"
                    if "wilson" in numbers else f"{numbers['lo']} to {numbers['hi']}")
        decision = figure["decision"]
        return _tex_row([str(figure["comparison"]), ARM_HEADS[figure["arm"]],
                     FAMILY_NAMES[figure["group"]], SPLIT_NAMES[figure["split"]],
                     _marked(numbers["pairs" if paired else "n"], marks),
                     _marked(numbers["clusters"], marks),
                     _marked(numbers.get("diff" if paired else "rate", "--"), marks),
                     _marked(interval, marks),
                     _marked(_tex(decision), marks) if decision else "--"])

    return _tabular(paper, "Comparisons 1 to 3", "llllrrrll",
                    ["Comparison", "Arm", "Scenarios", "Split", "$n$", "Clusters",
                     "Estimate (\\%)", "95\\% interval", "Decision"],
                    [line(f) for f in figures if f["comparison"] and not f["exploratory"]])


def _data(paper: _Paper, marks: Sequence[str], header: Sequence[str],
          rows: Iterable[Sequence[Any]]) -> str:
    """pgfplots data, read with ``col sep=comma`` and ``comment chars={\\%}``: the provenance
    line, a ``% todo{...}`` line when a number in it is not final, then the table."""
    lines = [paper.head(), *([f"% todo{{{'; '.join(marks)}}}\n"] if marks else [])]
    lines += [",".join(str(cell) for cell in row) + "\n" for row in [header, *rows]]
    return "".join(lines)


def _plain(value: float | None) -> str:
    return "nan" if value is None else f"{100 * value:.1f}"


def _fig_labels(paper: _Paper) -> str:
    """The label composition of each stage, family and arm (N, H and G) of the reviewed
    stages: each label's share, in percent, of the observations the review kept."""
    counts: dict[tuple[str, str, str], Counter] = {}
    for stage in paper.stages:
        for s in stage.sessions:
            if (stage.name in REVIEWED and s.arm in ("N", "H", "G") and s.observation
                    and s.job_id not in paper.review.removed):
                counts.setdefault((stage.name, s.family, s.arm), Counter())[s.label] += 1
    marks = _combined(paper.marks(*key) for key in counts)
    rows = []
    for (stage, family, arm), labels in counts.items():
        n = sum(labels.values())
        rows.append([f"{FAMILY_NAMES.get(family, family)} {arm}", stage, family, arm, n,
                     *(_plain(labels[label] / n) for label in LABELS)])
    return _data(paper, marks, ["tick", "stage", "family", "arm", "n", *LABELS], rows)


def _fig_forest(paper: _Paper, figures: Sequence[Mapping[str, Any]]) -> str:
    """The primary figures of comparisons 1 to 3: the estimate and its 95% interval in percent
    (points), a zero rate's from 0 to its Wilson upper limit, ``nan`` where there is none."""
    primary = [f for f in figures if f["role"] == "primary"]
    rows = []
    for figure in primary:
        result = figure["result"]
        estimate = result["difference"] if "pairs" in result else result["rate"]
        lo, hi = result["interval"] or (None, None)
        if result.get("wilson_upper") is not None:
            lo, hi = 0.0, result["wilson_upper"]
        tick = (f"{figure['arm']} {FAMILY_NAMES[figure['group']]} "
                f"({SPLIT_NAMES[figure['split']]})")
        rows.append([figure["id"], tick, figure["comparison"], _plain(estimate), _plain(lo),
                     _plain(hi)])
    return _data(paper, _combined(paper.figure_marks(f) for f in primary),
                 ["id", "tick", "comparison", "estimate", "lo", "hi"], rows)


DESCRIBED_TABLES = ("strata", "repetitions", "pair_success", "mechanisms", "kept_red", "run_test",
                    "cost", *DEFECT_TABLES)
"""The tables among the descriptives, each with a row per stage, family and arm (and ``by``,
``label`` or ``level``)."""


def _flagged(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A table's rows, each with ``exploratory``: true for a stage outside ``REVIEWED`` (the
    exploratory stage) and for a row its table marks exploratory (``fallback_codes``), whose
    numbers the paper names exploratory."""
    return [{**row, "exploratory": row.get("exploratory") is True or row["stage"] not in REVIEWED}
            for row in rows]


def paper_files(provenance_text: str, paper: _Paper, figures: Sequence[Mapping[str, Any]],
                tables: Mapping[str, Sequence[Mapping[str, Any]]],
                described: Mapping[str, Any]) -> dict[str, str]:
    """The text of each file of ``paper/generated``: ``provenance.json``, ``numbers.tex``, the
    labels table, the table of comparisons 1 to 3, and the pgfplots data of the label
    composition and of the forest plot. Every file but the provenance starts with a line naming
    its SHA-256."""
    comparison = [f for f in figures if f["comparison"]]
    return {"provenance.json": provenance_text,
            "numbers.tex": _numbers_tex(paper, _numbers(paper, figures, tables, described)),
            "table-labels.tex": _table_labels(paper, tables["labels"]),
            "table-comparisons.tex": _table_comparisons(paper, comparison),
            "fig-labels.csv": _fig_labels(paper),
            "fig-forest.csv": _fig_forest(paper, comparison)}


def run_analysis(results: Path, stage_names: Sequence[str], *, out: Path, repo: Path,
                 partial: bool = False, paper: Path | None = None) -> Path:
    """Load, check and compute everything, the paper's files (``paper_files``) included, then
    write ``out`` and, given ``paper``, those files there; nothing is written when the run is
    refused. Returns the provenance file."""
    results, out = Path(results).resolve(), Path(out).resolve()
    paper = Path(paper).resolve() if paper is not None else None
    for target in (out, paper):
        if target is not None and (target == results or results in target.parents):
            raise AnalysisRefused(f"the output {target} is under the results root {results}")
    stages = [load_stage(results, name, partial=partial) for name in stage_names]
    check_pins(stages)
    scenarios = load_scenarios(repo / "scenarios" / "selection.json",
                               {name: repo / "scenarios" / name / "rules.yaml"
                                for name in ("rename", "assert")})
    review = load_review(stages, repo / "docs" / "review")
    refused = load_refused(stages, repo / "docs" / "review")
    present = {path.parent.name for path in results.glob(f"*/{SUMMARY}")}
    figures = comparisons(stages, scenarios, review=review, present=present)
    refused_out = without_refused(stages, scenarios, review=review, refused=refused,
                                  present=present)
    described = descriptives(stages, scenarios, results=results, repo=repo, review=review)
    described.update({name: _flagged(described[name]) for name in DESCRIBED_TABLES})
    labels = _flagged(labels_table(stages, removed=review.removed))
    pilot = pilot_table(stages)
    eight = _flagged(section_8(stages, removed=review.removed))
    origin = provenance(results, stages, repo)
    text = json.dumps(origin, indent=1, sort_keys=True) + "\n"
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    every_figure = [*figures, *described["figures"], *refused_out]
    files = {}
    if paper is not None:
        tables = {"labels": labels, "section_8": eight,
                  **{name: described[name] for name in DESCRIBED_TABLES}}
        files = paper_files(text, _Paper(origin, sha, stages, review), every_figure, tables,
                            described)
    unfinished = [stage.name for stage in stages if stage.partial]
    out.mkdir(parents=True, exist_ok=True)
    (out / "provenance.json").write_text(text, encoding="utf-8")
    _write_json(out / "analysis.json", {
        "schema": 1, "partial": bool(unfinished), "stages": list(stage_names), "labels": labels,
        "figures": figures, "refused_out": refused_out, "pilot": pilot, "section_8": eight,
        "descriptives": described,
        "exploratory_prompts": exploratory_prompts(stages), "readings": list(READINGS),
        "seed": SEED, "bootstrap_draws": N_BOOT, "sign_flip_draws": N_PERM, "margin": MARGIN},
        sha)
    for stage in stages:
        if stage.name in REVIEWED:
            _write_csv(out / f"review-{stage.name}.csv", worksheet(stage), WORKSHEET_COLUMNS, sha,
                       unfinished)
    for name, rows in {"labels": labels, "figures": [_figure_row(f) for f in every_figure],
                       "section-8": eight, "pilot": _pilot_rows(pilot),
                       "strata": _strata_rows(described["strata"]),
                       "repetitions": described["repetitions"],
                       "pair-success": described["pair_success"],
                       "mechanisms": described["mechanisms"], "kept-red": described["kept_red"],
                       "run-test": described["run_test"], "cost": described["cost"],
                       "defects": described["defects"],
                       "false-alarms": described["false_alarms"],
                       "silenced": described["silenced"],
                       "fallback-use": described["fallback_use"],
                       "fallback-codes": described["fallback_codes"],
                       "coding-agreement": described["coding_agreement"],
                       "coding-spread": described["coding_spread"]}.items():
        _write_table(out / f"{name}.csv", rows, sha, unfinished)
    if paper is not None:
        paper.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            (paper / name).write_text(body, encoding="utf-8")
    return out / "provenance.json"


def main(argv: Sequence[str] | None = None, *, repo: Path = REPO) -> int:
    """The command. It reads the checkout its code runs in (``repo``; a test passes another)."""
    parser = argparse.ArgumentParser(prog="python -m hoh.analysis",
                                     description="The paper's numbers from the job records.")
    parser.add_argument("--results", type=Path, required=True,
                        help="the results root (read only)")
    parser.add_argument("--stages", nargs="+", required=True)
    parser.add_argument("--partial", action="store_true",
                        help="run on unfinished stages; every output says partial")
    parser.add_argument("--out", type=Path, default=None,
                        help="the output directory (default: build/analysis in the checkout)")
    parser.add_argument("--paper", type=Path, default=None,
                        help="where the paper's files go (default: paper/generated in the "
                             "checkout)")
    args = parser.parse_args(argv)
    out = args.out if args.out is not None else repo / "build" / "analysis"
    paper = args.paper if args.paper is not None else repo / "paper" / "generated"
    try:
        written = run_analysis(args.results, args.stages, out=out, repo=repo,
                               partial=args.partial, paper=paper)
    except AnalysisRefused as exc:
        print(f"analysis refused: {exc}", file=sys.stderr)
        return 2
    print(f"analysis written to {written.parent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
