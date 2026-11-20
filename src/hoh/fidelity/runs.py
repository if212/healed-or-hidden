"""The fidelity check's sessions in the order of the draw, on one slot (design doc 5.6; the fidelity
plan's decisions 1, 9, 11 and 12, step 7).

``python -m hoh.fidelity.runs sessions --config <yaml> --draw build/fidelity/draw.json --slot 7
--digests <json>`` runs the draw's sessions one after another. For each one, while it holds slot
N's lock (``hoh.docker_backend``'s, which the judge's runs also take), it

1. stops and removes the slot's previous stack, assembles the session's variant (A+M, or A+C with
   the rename rule) in ``<stacks_root>/slot-N`` as the project ``hohfid-N``, on the slot's network
   (``hoh.fidelity.slot``);
2. removes a server container left from before, builds the server's ``docker run`` line, and
   prepares the CLI's slot ``<work_root>/N`` (``hoh.fidelity.session.prepare_slot``), with the
   control directory ``<stacks_root>/control-N``;
3. runs the session with a reset watcher (``hoh.fidelity.session.run_session``); the fixture's
   first request makes the stack, so nothing is started before the session;
4. removes the server's container and stops the stack, whatever happened, and keeps the files.

Every attempt has its own directory, ``<output_root>/sessions/<NN>-<app>-<task>-<AM|AC>/attempt-K``:
``session/`` (the slot's link pointed there: the test as handed out, the server's call times),
``out/`` (the stream, ``launch.json``, ``end.json``), ``workspace/`` (what the agent handed back,
``test-results/`` left out), ``cfg/`` (the CLI's own record: its transcript, with the shape of
each request it sent, as H's attempts keep it), ``resets.json`` (the watcher's record),
``snapshots/NN`` (the workspace's ``tests/`` as the run that asked for reset NN found it, which
the judge reads for the Kept-red subtype) and ``attempt.json``, written last, whatever happened.

What an attempt's class does to the runs:

- ``completed`` and ``censored``: kept, for the judge; the next session.
- ``environment`` and ``capacity`` (an environment failure, the stack's or the API's): discarded
  and run again at once (decision 9; rule 9). The attempt records what was running: the port as
  given or the agent's edit, and the last tool call. The second discard of the same session stops
  the runs, and the author decides.
- every other class (``check_failed``, ``credential``, ``limit``, ``model_changed``), an attempt
  that raised, and an attempt directory without its record: the runs stop for a look. The next
  command stops at the same place until that attempt is set aside (renamed so that it no longer
  reads ``attempt-K``).

The spend (decision 11) is the spike's, from the config, and every attempt's, set aside or not:
its result line's ``total_cost_usd``, or the session cap when it has none. An attempt without its
record (one that another command is running, or one that was killed, set aside or not) counts at
the session cap. Before each session the runs stop if that session could take the spend past the
cap ($35). A command reads the spend and makes the session's attempt directory under one lock
(``spend_lock``), so that two commands cannot both pass the cap. An environment failure before
the session started costs nothing.

``guard.hold`` holds the command (the check never runs beside a stage or a validation), and the
guard looks again before each session and each reset; an overlap stops the runs. Two commands may
run sessions on two slots: each session is taken by one at a time (``item_lock``), which a command
takes before it reads the session's attempts, and skips while another holds it. A file named
``STOP`` in the output root (``stop_file``) stops each command before its next session, at a
window's end: each command finishes the session it runs, runs again at once a session that was
discarded, and starts no other.

``python -m hoh.fidelity.runs digests --race <dir> --out <json>`` writes the digests of decision
12 as a race recorded them, the same in every cell: port validation's races (step 6) for the
runs, and the checkpoint's for the spike, which comes before port validation. ``sessions`` and
``judge`` refuse to start when one differs now.

``python -m hoh.fidelity.runs judge --config <yaml> --draw <json> --slot N --digests <json>``
judges each kept attempt without a finished judgment on slot N's TypeScript backend
(``hoh.fidelity.judge.judge_spec``, decision 8): T' as the attempt kept it (``workspace/tests``)
against the test the session handed out (``session/original_tests``), by the protocol of the
study's family (B-oracle for A+M, C-rename for A+C), with the scenario's record in the baselines
of stages 1a and 1b. It writes ``judging-J`` in the attempt's directory, numbered so that an
unfinished one is kept for a look; a judgment is finished once it has its ``fidelity`` section
and its ``state`` is ``judged``.
It may run beside the sessions on another slot, and again: a session not kept yet waits, and
the command exits 1 while one does. It makes the sessions' checks first (the commit, the images,
the digests) and refuses a baseline measured with another Python runner, or a rename target
other than the rules file's.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from hoh import docker_backend, envs, worker
from hoh import rules as rules_module
from hoh import session as study
from hoh.backend import EnvFailure, Variant
from hoh.benchmark import REPO_ROOT
from hoh.fidelity import guard as guard_module
from hoh.fidelity import race, validations
from hoh.fidelity import session as fid
from hoh.fidelity import slot as slot_module
from hoh.fidelity import stack as stack_module
from hoh.fidelity.judge import judge_spec
from hoh.queue import atomic_write_json

log = logging.getLogger(__name__)

FAMILIES: Mapping[str, str] = {"A+M": "AM", "A+C": "AC"}
"""The variant each family's session gets, and its short name: B-oracle sessions run on A+M,
C-rename sessions on A+C (decision 1)."""
STUDY_FAMILIES: Mapping[str, str] = {"A+M": "b_oracle", "A+C": "c_rename"}
"""The study's family whose protocol judges each session (decision 8)."""
SNAPSHOTS = "snapshots"
"""The attempt's directory of the test as each run found it (``hoh.fidelity.slot.ResetWatcher``)."""
KEPT = frozenset({"completed", "censored"})
DISCARDED = frozenset({"environment", "capacity"})
MAX_DISCARDS = 2
SPEND_CAP_USD = 35.0
DIGEST_KEYS = ("source_digest", "runner_source_digest", "images", "timing", "environment",
               "rules_source_sha256")
"""What ``digests`` records and ``sessions`` compares (decision 12): the TypeScript runner's
sources (the fixture, its config and lock file), the Python runner's, both images, the timing,
the stacks' files and the rules' code."""
_NAME = re.compile(r"[a-z0-9_]+")
_ATTEMPT = re.compile(r"attempt-([1-9][0-9]*)")
_JUDGING = re.compile(r"judging-([1-9][0-9]*)")
_MEASURED_WITH = ("test_sha256", "target", "reference_fix_sha256", "rules_source_sha256")
"""What a baseline entry records of the inputs it was measured with, not for the judge."""


# --- the order


@dataclass(frozen=True)
class Item:
    """One session of the draw: its place in the order (from 1), the test and the family's
    variant."""

    place: int
    app: str
    task: str
    code: str

    @property
    def name(self) -> str:
        return f"{self.place:02d}-{self.app}-{self.task}-{FAMILIES[self.code]}"

    def record(self) -> dict[str, Any]:
        return {"place": self.place, "app": self.app, "task": self.task, "code": self.code}


def items(draw: Mapping[str, Any]) -> list[Item]:
    """The draw's sessions (``draw.json``'s ``sessions``: ``[app, task, variant]`` in the order
    of decision 1). Raises ``ValueError`` for a session the check does not run or one named
    twice."""
    sessions = draw.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        raise ValueError("the draw has no sessions")
    found: list[Item] = []
    seen: set[tuple[str, str, str]] = set()
    for place, entry in enumerate(sessions, 1):
        if not (isinstance(entry, list) and len(entry) == 3
                and all(isinstance(part, str) for part in entry)):
            raise ValueError(f"session {place}: {entry!r} is not [app, task, variant]")
        app, task, code = entry
        if code not in FAMILIES:
            raise ValueError(f"session {place}: the check runs {', '.join(FAMILIES)}, not {code!r}")
        if not (_NAME.fullmatch(app) and _NAME.fullmatch(task)):
            raise ValueError(f"session {place}: {app!r}/{task!r} is not an app and a task")
        if (app, task, code) in seen:
            raise ValueError(f"session {place}: {app}/{task} {code} is named twice")
        seen.add((app, task, code))
        found.append(Item(place, app, task, code))
    return found


# --- the configuration


@dataclass(frozen=True, kw_only=True)
class RunsConfig:
    launch: study.LaunchConfig
    credential_kind: str
    env_file: Path
    agent_file: Path
    """The file ``playwright init-agents --loop=claude`` wrote (decision 2)."""
    agent_sha256: str
    rules: Path
    """The rules file of the rename variant."""
    ports: Path
    """The converter's ``ports.json``, recorded with every attempt."""
    validations: tuple[Path, ...]
    """Stages 1a and 1b's validation directories (``hoh.fidelity.validations``), whose baselines
    the judge compares the shadow oracle with."""
    tests_root: Path
    """The ports: ``<tests_root>/<app>/<task>.spec.ts``."""
    work_root: Path
    """The CLI's slots: ``<work_root>/<N>``."""
    output_root: Path
    stacks_root: Path = slot_module.STACKS_ROOT
    project_prefix: str = slot_module.PROJECT_PREFIX
    image_prefix: str = "hoh"
    spend_cap_usd: float = SPEND_CAP_USD
    spent_before_usd: float = 0.0
    """The spike's spend, which the cap counts (decision 11)."""


_KEYS = {"launch", "credential", "agent", "rules", "ports", "validations", "tests_root",
         "work_root", "output_root", "spend"}


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{where} must be a number, got {value!r}")
    return float(value)


def load_config(path: Path) -> RunsConfig:
    """Read the runs' configuration (YAML; a local file, as a stage's). ``launch`` is a stage's
    launch block (``hoh.worker``); relative paths are the repository's. Raises ``ValueError``."""
    path = Path(path)
    data = worker._check_keys(yaml.safe_load(path.read_text(encoding="utf-8")), _KEYS, _KEYS,
                              str(path))
    credential = worker._check_keys(data["credential"], {"kind", "env_file"},
                                    {"kind", "env_file"}, "credential")
    if credential["kind"] != "api_key":
        raise ValueError(f"credential.kind: the fidelity check runs on the API key (decision "
                         f"11), not {credential['kind']!r}")
    agent = worker._check_keys(data["agent"], {"file", "sha256"}, {"file", "sha256"}, "agent")
    if not isinstance(agent["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", agent["sha256"]):
        raise ValueError(f"agent.sha256 must be a SHA-256, got {agent['sha256']!r}")
    spend = worker._check_keys(data["spend"], {"cap_usd", "before_usd"}, {"cap_usd", "before_usd"},
                               "spend")
    cap = _number(spend["cap_usd"], "spend.cap_usd")
    before = _number(spend["before_usd"], "spend.before_usd")
    if not 0 < cap <= SPEND_CAP_USD:
        raise ValueError(f"spend.cap_usd must be above 0 and at most {SPEND_CAP_USD:g} (decision "
                         f"11), got {cap:g}")
    if before < 0:
        raise ValueError(f"spend.before_usd must not be negative, got {before:g}")
    found = data["validations"]
    if not isinstance(found, list) or not found:
        raise ValueError(f"validations must list the validation directories, got {found!r}")
    return RunsConfig(
        launch=worker._launch(data["launch"]),
        credential_kind=credential["kind"],
        env_file=worker._path(credential["env_file"], "credential.env_file"),
        agent_file=worker._path(agent["file"], "agent.file"),
        agent_sha256=agent["sha256"],
        rules=worker._path(data["rules"], "rules"),
        ports=worker._path(data["ports"], "ports"),
        validations=tuple(worker._path(p, f"validations[{n}]") for n, p in enumerate(found)),
        tests_root=worker._path(data["tests_root"], "tests_root"),
        work_root=worker._path(data["work_root"], "work_root"),
        output_root=worker._path(data["output_root"], "output_root"),
        spend_cap_usd=cap,
        spent_before_usd=before,
    )


# --- where things are


def slot_dir(config: RunsConfig, number: int) -> Path:
    return Path(config.work_root).resolve() / str(number)


def control_dir(config: RunsConfig, number: int) -> Path:
    return Path(config.stacks_root).resolve() / f"control-{number}"


def item_dir(config: RunsConfig, item: Item) -> Path:
    return Path(config.output_root) / "sessions" / item.name


def attempt_dir(config: RunsConfig, item: Item, number: int) -> Path:
    return item_dir(config, item) / f"attempt-{number}"


def lock_path(config: RunsConfig, name: str) -> Path:
    """A lock of the output root's runs: beside the Compose locks (``HOH_LOCK_DIR``, else the
    temp directory), keyed by the output root, so that nothing of it enters the results."""
    root = Path(os.environ.get("HOH_LOCK_DIR") or tempfile.gettempdir()) / "hoh-fidelity"
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(Path(config.output_root).resolve()).encode()).hexdigest()[:12]
    return root / f"{key}-{name}.lock"


@contextlib.contextmanager
def item_lock(config: RunsConfig, item: Item, *, role: str = "session") -> Iterator[bool]:
    """Take the item's session (``role="session"``) or its judging (``"judge"``) for this
    process, if no other holds it: yields False when one does."""
    with open(lock_path(config, f"{role}-{item.name}"), "a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True  # closing the file releases the lock


@contextlib.contextmanager
def spend_lock(config: RunsConfig) -> Iterator[None]:
    """Hold the spend of the output root's runs, waiting for another command that holds it: a
    command reads the spend and makes its attempt's directory under it (module docstring)."""
    with open(lock_path(config, "spend"), "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield  # closing the file releases the lock


# --- the slot's stack


class SlotStack:
    """Slot ``number``'s stack, for sessions (module docstring): a ``TsBackend``'s slot, project
    and lock, on the slot's network, with the server's container named after the slot."""

    def __init__(
        self,
        number: int,
        *,
        stacks_root: Path = slot_module.STACKS_ROOT,
        project_prefix: str = slot_module.PROJECT_PREFIX,
        rules: Path | None,
        guard: guard_module.Guard | None = None,
        run: slot_module.Run = subprocess.run,
    ) -> None:
        self.number = number
        self.network = slot_module.network_name(number)
        self.container = f"{project_prefix}-{number}-mcp"
        self.backend = stack_module.TsBackend(stacks_root, number, project_prefix=project_prefix,
                                              rules=rules)
        self.guard = guard
        self.overlap: Exception | None = None
        self._run = run

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        with self.backend._slot_lock():
            yield

    def assemble(self, variant: Variant) -> Path:
        """Stop and remove the slot's previous stack, assemble ``variant``'s, and put it on the
        slot's network. Raises ``EnvFailure``."""
        self.backend._clear_slot()
        stack_dir = envs.assemble(variant, self.backend.stack_dir, project=self.backend.project,
                                  rules=self.backend._rules(variant))
        slot_module.join_network(stack_dir, slot_module.ensure_network(self.number, run=self._run))
        return stack_dir

    def recreate(self, stack_dir: Path, *, timeout_s: float) -> None:
        """The watcher's reset: the guard looks first, and an overlap is kept for the runs."""
        if self.guard is not None:
            try:
                self.guard.before_run([f"reset slot {self.number}"])
            except guard_module.Overlap as exc:
                self.overlap = exc
                raise
        slot_module.recreate(stack_dir, timeout_s=timeout_s)

    def stop(self, stack_dir: Path) -> str | None:
        """Stop the stack; returns the error instead of raising it (the next session stops it
        again)."""
        try:
            envs.stop(stack_dir, timeout_s=docker_backend.STOP_TIMEOUT_S)
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    def remove_server(self) -> str | None:
        """Remove the server's container if it is there; the CLI ends the ``docker run``
        client, and the container may outlive it. Returns the error instead of raising it."""
        try:
            done = self._run(["docker", "rm", "--force", self.container], capture_output=True,
                             text=True, timeout=slot_module.DOCKER_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"{type(exc).__name__}: {exc}"
        if done.returncode != 0 and "no such container" not in (done.stderr or "").lower():
            return (done.stderr or "").strip() or f"docker rm exited with {done.returncode}"
        return None


# --- one attempt


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_attempt(
    item: Item,
    directory: Path,
    *,
    slot: int,
    stack: SlotStack,
    config: RunsConfig,
    credential: study.Credential,
    run_session: Callable[..., fid.Result] = fid.run_session,
    make_watcher: Callable[..., fid.Watcher] = slot_module.ResetWatcher,
    provenance: Callable[..., dict[str, Any]] = race.provenance,
) -> dict[str, Any]:
    """One session of ``item`` in slot ``slot`` (module docstring, steps 1 to 4); returns the
    attempt's record, also written to ``attempt.json``. An environment failure before the session
    is an ``environment`` attempt; anything else that raises is recorded and raised again."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)  # the queue makes it when it takes the spend
    if any(directory.iterdir()):
        raise FileExistsError(f"{directory}: an attempt needs a directory of its own")
    variant = race.parse_variant(item.app, item.task, item.code)
    test_file = Path(config.tests_root) / item.app / f"{item.task}.spec.ts"
    workspace, control = slot_dir(config, slot), control_dir(config, slot)
    record: dict[str, Any] = {
        "schema": 1, "name": item.name, "item": item.record(), "attempt": directory.name,
        "slot": slot, "started_at": time.time(), "ended_at": None, "class": None, "kept": False,
        "outcome": None, "cost_usd": None, "spend_usd": 0.0, "discard": None, "error": None,
        "stops": {}, "prepared": False, "returned": None, "provenance": None,
    }
    try:
        sha = _sha256(config.agent_file)
        if sha != config.agent_sha256:
            raise ValueError(f"{config.agent_file}: its SHA-256 is {sha}, not the recorded "
                             f"{config.agent_sha256}")
        record["provenance"] = provenance(
            test_file, variant, ports=config.ports,
            rules=config.rules if variant.rule else None, image_prefix=config.image_prefix)
        with stack.lock():
            _attempt(item, directory, record, variant=variant, test_file=test_file,
                     workspace=workspace, control=control, stack=stack, config=config,
                     credential=credential, run_session=run_session, make_watcher=make_watcher)
    except BaseException as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        if record["prepared"] and (workspace / "out" / "launch.json").exists():
            record["spend_usd"] = float(config.launch.max_budget_usd)  # the CLI may have spent
        raise
    finally:
        record["ended_at"] = time.time()
        atomic_write_json(directory / "attempt.json", record)
    return record


def _attempt(
    item: Item, directory: Path, record: dict[str, Any], *, variant: Variant, test_file: Path,
    workspace: Path, control: Path, stack: SlotStack, config: RunsConfig,
    credential: study.Credential, run_session: Callable[..., fid.Result],
    make_watcher: Callable[..., fid.Watcher],
) -> None:
    stack_dir = None
    ended = False  # run_session returned: what the session left is data
    try:
        try:
            stack_dir = stack.assemble(variant)
            leftover = stack.remove_server()
            if leftover is not None:
                raise EnvFailure(f"the server container {stack.container} could not be "
                                 f"removed: {leftover}")
        except EnvFailure as exc:
            reason = f"environment: {type(exc).__name__}: {exc}"
            record.update({"class": "environment", "outcome": {"state": "env_failed",
                                                               "detail": reason},
                           "discard": {"reason": reason, "tests_as_given": None,
                                       "last_mcp_call": None}})
            log.warning("%s: %s", item.name, reason)
            return
        argv = slot_module.server_command(
            network=stack.network, workspace=workspace / "ws", control_dir=control,
            environment=slot_module.server_environment(stack_dir),
            image_prefix=config.image_prefix, name=stack.container)
        fid.prepare_slot(workspace, test_file=test_file, session_dir=directory / "session",
                         agent_file=config.agent_file, control_dir=control, server_argv=argv,
                         config=config.launch)
        record["prepared"] = True
        watcher = make_watcher(stack_dir, control, recreate=stack.recreate,
                               log=directory / "resets.json", tests_dir=workspace / "ws" / "tests",
                               snapshot_root=directory / SNAPSHOTS)
        log.info("%s: session in slot %s (%s)", item.name, record["slot"], variant.code)
        result = run_session(workspace, directory / "session", credential, config.launch,
                             control_dir=control, watcher=watcher)
        ended = True
    finally:
        if stack_dir is not None:
            record["stops"] = {"server": stack.remove_server(), "stack": stack.stop(stack_dir)}
        if record["prepared"]:
            try:
                worker._copy_tree(workspace / "out", directory / "out")
                worker._copy_workspace(workspace / "ws", directory / "workspace")
                # the CLI's own record, its transcript with each request's shape behind
                # end.json's snapshot counts: the next session in the slot empties cfg/
                worker._copy_tree(workspace / "cfg", directory / "cfg")
            except OSError:
                if ended:
                    # a T' not copied would read as a deleted target, Silenced: the attempt is
                    # not kept and the runs stop (CLAUDE.md, rule 9)
                    raise
                # the session raised: what was copied is kept for a look, and its error goes on
            if not ended:
                # run_session searches the slot for the credential only when it returns: the
                # slot, which keeps its files until the next session, and the copies, here
                leaks = study._redact_everywhere(credential.value,
                                                 {"slot": workspace, "attempt": directory})
                if leaks:
                    record["credential_found_in"] = leaks
                    log.error("%s: the credential value was found, and redacted, in %s",
                              item.name, ", ".join(leaks))
    if not (directory / "out" / "end.json").is_file():
        raise FileNotFoundError(f"{directory / 'out' / 'end.json'}: the session's ending was not "
                                f"kept")
    returned = directory / "workspace" / "tests" / test_file.name
    record["returned"] = str(returned.relative_to(directory)) if returned.is_file() else None
    cls = result.end_class
    cost = (result.result or {}).get("total_cost_usd")
    cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
    record.update({
        "class": cls,
        "kept": cls in KEPT,
        "outcome": {"state": result.outcome.state, "detail": result.outcome.detail},
        "cost_usd": cost,
        "spend_usd": cost if cost is not None else float(config.launch.max_budget_usd),
        "session_id": result.session_id,
        "wall_s": round(result.wall_s, 3),
    })
    if cls in DISCARDED:
        end = _read(directory / "out" / "end.json") or {}
        record["discard"] = {"reason": result.outcome.detail,
                             "tests_as_given": end.get("tests_as_given"),
                             "last_mcp_call": end.get("last_mcp_call")}
    log.info("%s: %s", item.name, result.outcome.detail)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --- the queue


@dataclass(frozen=True)
class Ended:
    done: int
    """Sessions with a kept attempt."""
    stopped: str | None
    """Why the runs stopped before the end, or None."""
    spent_usd: float


def _attempts(config: RunsConfig, item: Item) -> tuple[list[dict[str, Any]], int, str | None]:
    """The item's attempt records in order, the highest attempt number, and the problem that
    stops the runs (an attempt without its record, or of a class that needs a look)."""
    root = item_dir(config, item)
    numbered = sorted(
        (int(found.group(1)), path) for path in (root.iterdir() if root.is_dir() else ())
        if (found := _ATTEMPT.fullmatch(path.name)) and path.is_dir()
    )
    records = []
    for _, path in numbered:
        record = _read(path / "attempt.json")
        if record is None:
            return records, numbered[-1][0], (
                f"{item.name}/{path.name} has no record: look at it, and set it aside")
        if not record.get("kept") and record.get("class") not in DISCARDED:
            what = record.get("error") or (record.get("outcome") or {}).get("detail")
            return records, numbered[-1][0], (
                f"{item.name}/{path.name} ended as {record.get('class')}: {what}; look at it, "
                f"and set it aside")
        records.append(record)
    return records, numbered[-1][0] if numbered else 0, None


def spent(config: RunsConfig, all_items: Sequence[Item]) -> float:
    """The spend so far (module docstring): every attempt's record, and an attempt without its
    record at the session cap, set aside or not."""
    total = float(config.spent_before_usd)
    for item in all_items:
        root = item_dir(config, item)
        for path in sorted(root.iterdir()) if root.is_dir() else ():
            if not path.is_dir():
                continue
            record = _read(path / "attempt.json")
            if record is not None:
                total += float(record.get("spend_usd") or 0.0)
            else:  # still running, or killed and perhaps set aside since
                total += float(config.launch.max_budget_usd)
    return total


def stop_file(config: RunsConfig) -> Path:
    """The file whose presence stops each ``sessions`` command before a session with no attempt
    yet: a discarded session runs again first (decision 9)."""
    return Path(config.output_root) / "STOP"


def run_sessions(
    all_items: Sequence[Item],
    *,
    slot: int,
    stack: SlotStack,
    config: RunsConfig,
    credential: study.Credential,
    run_session: Callable[..., fid.Result] = fid.run_session,
    make_watcher: Callable[..., fid.Watcher] = slot_module.ResetWatcher,
    provenance: Callable[..., dict[str, Any]] = race.provenance,
    guard: guard_module.Guard | None = None,
) -> Ended:
    """Run the items' sessions in order (module docstring). Raises ``Overlap`` when the guard
    finds a study stack up, and what an attempt raised."""
    done = 0
    for item in all_items:
        while True:
            with item_lock(config, item) as mine:
                if not mine:
                    log.info("%s: another command runs it", item.name)
                    break
                records, last, problem = _attempts(config, item)
                if problem is not None:
                    return Ended(done, problem, spent(config, all_items))
                if any(r.get("kept") for r in records):
                    done += 1
                    break
                if len(records) >= MAX_DISCARDS:
                    return Ended(done, f"{item.name}: two discards; the author decides "
                                       f"(decision 9)", spent(config, all_items))
                if not records and stop_file(config).exists():
                    # a discarded session is run again first (decision 9), so the sessions that
                    # ended stay the first ones of the draw's order (runbook 2.5)
                    return Ended(done, f"the stop file {stop_file(config)}: no session starts",
                                 spent(config, all_items))
                if guard is not None:
                    guard.before_run([f"session {item.name}"])
                directory = attempt_dir(config, item, last + 1)
                with spend_lock(config):
                    so_far = spent(config, all_items)
                    if so_far + config.launch.max_budget_usd > config.spend_cap_usd:
                        return Ended(done, (
                            f"the spend: ${so_far:.2f} so far, and the next session could take "
                            f"it past the ${config.spend_cap_usd:g} cap (decision 11)"), so_far)
                    directory.mkdir(parents=True)  # counted at the session cap until its record
                run_attempt(item, directory, slot=slot, stack=stack, config=config,
                            credential=credential, run_session=run_session,
                            make_watcher=make_watcher, provenance=provenance)
            if stack.overlap is not None:
                raise stack.overlap
            # the next pass reads the attempt: kept, discarded, or a stop
    return Ended(done, None, spent(config, all_items))


# --- the judge


def baseline_entries(vals: Sequence[validations.Validation], all_items: Sequence[Item], *,
                     ruleset: rules_module.RuleSet, runner: str) -> dict[str, dict[str, Any]]:
    """Each item's scenario as a stage's judge gets it (``hoh.worker.StageConfig.scenario``),
    by item name: its record in the validation that holds it (stage 1a's for B-oracle, 1b's for
    C-rename), without what it was measured with. As the study's worker does, it refuses a
    baseline measured with another Python runner than ``runner`` and a rename target other than
    the rules file's; the environment and ``rules.py`` digests are the verdict's to bound (design
    doc 5.10). Raises ``ValueError`` naming every problem."""
    entries: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for item in all_items:
        family = STUDY_FAMILIES[item.code]
        key = f"{family}/{item.app}/{item.task}"
        try:
            found, _, record = validations.scenario(vals, key)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        measured = found.baseline.get("runner")
        if measured != runner:
            problems.append(f"{key}: {found.path.name}'s baseline was measured with another runner "
                            f"({str(measured)[:12]}, now {runner[:12]})")
        entry = dict(record)
        validated = {name: entry.pop(name, None) for name in _MEASURED_WITH}
        if family in rules_module.RULE_FAMILIES:
            target = ruleset.target(item.app, item.task)
            now = asdict(target) if target is not None else None
            if validated["target"] != now:
                problems.append(f"{key}: the validation ran with the target "
                                f"{validated['target']}, the rules file gives {now}")
        entries[item.name] = entry
    if problems:
        raise ValueError("; ".join(problems))
    return entries


def kept_attempt(config: RunsConfig, item: Item) -> Path | None:
    """The item's kept attempt, or None."""
    root = item_dir(config, item)
    for path in sorted(root.iterdir()) if root.is_dir() else ():
        if (_ATTEMPT.fullmatch(path.name) and path.is_dir()
                and (_read(path / "attempt.json") or {}).get("kept")):
            return path
    return None


def _finished(judgment: dict[str, Any] | None) -> bool:
    """A judgment with its ``fidelity`` section (the study's judge writes its own file first)
    and a label: one that the environment ended (``env_failed``) is judged again (rule 9)."""
    return judgment is not None and "fidelity" in judgment and judgment.get("state") == "judged"


def _judgment(attempt: Path) -> tuple[dict[str, Any] | None, int]:
    """The attempt's finished judgment, if any, and the highest ``judging-J`` number."""
    numbered = sorted(
        (int(found.group(1)), path) for path in attempt.iterdir()
        if (found := _JUDGING.fullmatch(path.name)) and path.is_dir()
    )
    for _, path in numbered:
        judgment = _read(path / "judgment.json")
        if _finished(judgment):
            return judgment, numbered[-1][0]
    return None, numbered[-1][0] if numbered else 0


@dataclass(frozen=True)
class Judged:
    judged: int
    """Sessions with a finished judgment."""
    waiting: int
    """Sessions without a kept attempt yet, or judged by another command now."""
    labels: dict[str, Any]
    """The label of each judged session, by item name."""
    stopped: str | None = None
    """Why the judge stopped before the end: a judgment the environment ended."""


def snapshots(attempt: Path, name: str) -> list[str | None]:
    """The text of the test ``name`` in each snapshot the attempt's reset watcher kept, in the
    order of the resets; None where the file was gone or is a symbolic link, which the judge
    counts as edited, as it does a linked T' (``hoh.fidelity.judge``). A file that is not UTF-8
    is read with its bad bytes replaced, so that it reads as no port does."""
    root = Path(attempt) / SNAPSHOTS
    taken = sorted((int(path.name), path) for path in (root.iterdir() if root.is_dir() else ())
                   if path.is_dir() and path.name.isdigit())
    return [None if (path / name).is_symlink() or not (path / name).is_file()
            else (path / name).read_text(encoding="utf-8", errors="replace")
            for _, path in taken]


RUN_TOOLS = ("test_run", "test_debug")
"""The healer's tools that run a test, each of which asks for a reset once a test starts."""


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def calls_without_reset(attempt: Path) -> int | None:
    """How many of the session's ``test_run`` and ``test_debug`` calls had no reset in their span
    (``hoh.fidelity.judge``, point 7): a run that starts no test asks for none, and leaves no
    snapshot. From the proxy's call times (``session/run_test_state.json``) and the watcher's
    record (``resets.json``, written at its first reset: none, no reset). A call still open at
    the end, or with no time, spans to the end. None when either file cannot be read."""
    attempt = Path(attempt)
    try:
        state = json.loads((attempt / "session" / fid.STATE_FILE).read_text(encoding="utf-8"))
        calls = [c for c in state["tool_calls"]
                 if isinstance(c, dict) and c.get("tool") in RUN_TOOLS]
        log_file = attempt / "resets.json"
        record = json.loads(log_file.read_text(encoding="utf-8")) if log_file.exists() else {}
        starts = [r.get("started_at") for r in record.get("resets") or () if isinstance(r, dict)]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    starts = [s for s in starts if _finite(s)]
    count = 0
    for call in calls:
        begin = call.get("started_at") if _finite(call.get("started_at")) else -math.inf
        end = begin + call["seconds"] if _finite(call.get("seconds")) else math.inf
        count += not any(begin <= s <= end for s in starts)
    return count


def judge_kept(
    all_items: Sequence[Item],
    *,
    backend: Any,
    config: RunsConfig,
    entries: Mapping[str, dict[str, Any]],
    judge: Callable[..., dict[str, Any]],
    guard: guard_module.Guard | None = None,
) -> Judged:
    """Judge each kept attempt that has no finished judgment (``hoh.fidelity.judge.judge_spec``
    as ``judge``): T' as the attempt kept it, against the test the session handed out, in the
    attempt's next ``judging-J`` directory, so that an unfinished judging is kept for a look.
    ``entries`` are ``baseline_entries``'. Raises what the judge raised (an ``Overlap`` of the
    guard among them)."""
    judged = waiting = 0
    labels: dict[str, Any] = {}
    for item in all_items:
        attempt = kept_attempt(config, item)
        if attempt is None:
            waiting += 1
            continue
        with item_lock(config, item, role="judge") as mine:
            if not mine:
                log.info("%s: another command judges it", item.name)
                waiting += 1
                continue
            judgment, last = _judgment(attempt)
            if judgment is None:
                name = f"{item.task}.spec.ts"
                port = (attempt / "session" / study.ORIGINAL_TESTS / name).read_text(
                    encoding="utf-8")
                log.info("%s: judging %s", item.name, attempt.name)
                judgment = judge(attempt / "workspace" / "tests", port, STUDY_FAMILIES[item.code],
                                 item.app, item.task, backend, attempt / f"judging-{last + 1}",
                                 entries[item.name], snapshots=snapshots(attempt, name),
                                 calls_without_reset=calls_without_reset(attempt), guard=guard)
                if not _finished(judgment):
                    # the judge's own retries are spent: the next session would meet the same
                    # environment, so stop; the next command judges this one again
                    return Judged(judged, waiting, labels, (
                        f"{item.name}: judging-{last + 1} ended {judgment.get('state')} "
                        f"({judgment.get('env_failure')}); run the command again"))
        judged += 1
        labels[item.name] = judgment.get("label")
    return Judged(judged, waiting, labels)


# --- decision 12


def digests(image_prefix: str = "hoh", repo_root: Path = REPO_ROOT,
            run: slot_module.Run = subprocess.run) -> dict[str, Any]:
    """The digests of ``DIGEST_KEYS``, as ``race.provenance`` records them."""
    images = (f"{image_prefix}/{envs.RUNNER_IMAGE}",
              f"{image_prefix}/{stack_module.RUNNER_TS_IMAGE}")
    return {
        "source_digest": stack_module.source_digest(repo_root),
        "runner_source_digest": docker_backend.runner_source_digest(repo_root),
        "images": {image: race._image_id(run, image) for image in images},
        "timing": dict(envs.RUNNER_TIMING),
        "environment": envs.environment_digest(repo_root),
        "rules_source_sha256": rules_module.rules_source_digest(),
    }


def race_digests(race_dir: Path) -> tuple[dict[str, Any], int]:
    """The digests of ``DIGEST_KEYS`` that the race under ``race_dir`` recorded (decision 12), in
    each cell's ``race.json``, and the number of cells. Every cell must hold the same ones, and
    all of them."""
    sets: dict[str, int] = {}
    for cell in sorted(Path(race_dir).glob("*/race.json")):
        provenance = json.loads(cell.read_text(encoding="utf-8")).get("provenance") or {}
        found = json.dumps({key: provenance.get(key) for key in DIGEST_KEYS}, sort_keys=True)
        sets[found] = sets.get(found, 0) + 1
    if not sets:
        raise ValueError(f"no race.json under {race_dir}")
    if len(sets) > 1:
        raise ValueError(f"the race's cells recorded {len(sets)} sets of digests")
    [(found, cells)] = sets.items()
    recorded = json.loads(found)
    missing = [key for key in DIGEST_KEYS if not recorded.get(key)]
    if missing:
        raise ValueError(f"the race recorded no {', '.join(missing)}")
    return recorded, cells


def digest_problems(recorded: Mapping[str, Any], now: Mapping[str, Any]) -> list[str]:
    """What differs from the digests recorded (decision 12)."""
    return [f"{key}: recorded {recorded.get(key)!r}, now {now.get(key)!r}"
            for key in DIGEST_KEYS if recorded.get(key) != now.get(key)]


# --- the command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hoh.fidelity.runs", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    sessions = commands.add_parser("sessions", help="run the draw's sessions on one slot")
    sessions.add_argument("--config", required=True, type=Path)
    sessions.add_argument("--draw", required=True, type=Path)
    sessions.add_argument("--slot", required=True, type=int, choices=slot_module.SLOTS)
    sessions.add_argument("--digests", required=True, type=Path,
                          help="the digests a race recorded (the digests command)")
    judging = commands.add_parser("judge", help="judge the kept sessions on one slot")
    record = commands.add_parser("digests", help="write the digests of decision 12 that a "
                                                 "race recorded")
    record.add_argument("--race", required=True, type=Path, help="the race's directory")
    record.add_argument("--out", required=True, type=Path)
    judging.add_argument("--config", required=True, type=Path)
    judging.add_argument("--draw", required=True, type=Path)
    judging.add_argument("--slot", required=True, type=int, choices=slot_module.SLOTS)
    judging.add_argument("--digests", required=True, type=Path,
                         help="the digests a race recorded (the digests command)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "digests":
        if args.out.exists():
            print(f"not written: {args.out} exists", file=sys.stderr)
            return 2
        try:
            recorded, cells = race_digests(args.race)
        except (OSError, ValueError) as exc:
            print(f"not written: {exc}", file=sys.stderr)
            return 2
        atomic_write_json(args.out, {"written_at": time.time(), "race": str(args.race),
                                     "cells": cells, **recorded})
        print(f"digests: {args.out}, as the race's {cells} cells recorded them")
        return 0

    try:
        config = load_config(args.config)
        all_items = items(json.loads(args.draw.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        print(f"not started: {exc}", file=sys.stderr)
        return 2
    changed = race.uncommitted()
    problems = [f"changes the commit does not hold: {', '.join(changed)}"] if changed else []
    problems += [p for p in (docker_backend.runner_image_problem(config.image_prefix),
                             stack_module.image_problem(config.image_prefix)) if p]
    now: dict[str, Any] = {}
    try:
        recorded = json.loads(args.digests.read_text(encoding="utf-8"))
        now = digests(config.image_prefix)
        problems += digest_problems(recorded, now)
    except (OSError, ValueError) as exc:
        problems.append(f"the digests: {exc}")
    if args.command == "judge":
        try:
            ruleset = rules_module.load_rules(config.rules)
            entries = baseline_entries([validations.load(p) for p in config.validations],
                                       all_items, ruleset=ruleset,
                                       runner=str(now.get("runner_source_digest")))
        except (OSError, ValueError, KeyError) as exc:
            problems.append(f"the baselines: {exc}")
    else:
        missing = [str(p) for p in (config.agent_file, config.rules, config.ports,
                                    *(config.tests_root / i.app / f"{i.task}.spec.ts"
                                      for i in all_items)) if not Path(p).is_file()]
        if missing:
            problems.append(f"missing: {', '.join(missing)}")
    if problems:
        for problem in problems:
            print(f"not started: {problem}", file=sys.stderr)
        return 2
    if args.command == "judge":
        return _judge(args.slot, config, all_items, entries, ruleset)
    try:
        credential = study.load_credential(config.credential_kind, config.env_file)
    except (OSError, ValueError) as exc:
        print(f"not started: the credential: {exc}", file=sys.stderr)
        return 2
    command = f"fidelity sessions slot {args.slot}"
    try:
        with guard_module.hold(command) as held:
            stack = SlotStack(args.slot, stacks_root=config.stacks_root,
                              project_prefix=config.project_prefix, rules=config.rules,
                              guard=held)
            ended = run_sessions(all_items, slot=args.slot, stack=stack, config=config,
                                 credential=credential, guard=held)
    except (guard_module.Refused, guard_module.Overlap) as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - the session's directory is left for a look
        traceback.print_exc()
        print(f"stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"{ended.done} of {len(all_items)} sessions kept; spend ${ended.spent_usd:.2f}")
    if ended.stopped is not None:
        print(f"stopped: {ended.stopped}", file=sys.stderr)
        return 1
    return 0


def _judge(slot: int, config: RunsConfig, all_items: Sequence[Item],
           entries: Mapping[str, dict[str, Any]], ruleset: rules_module.RuleSet) -> int:
    """The ``judge`` command, once its checks passed: 0 when every session is judged, 1 when
    some wait (run it again later), 3 on the guard."""
    try:
        with guard_module.hold(f"fidelity judge slot {slot}") as held:
            backend = stack_module.TsBackend(
                config.stacks_root, slot, project_prefix=config.project_prefix,
                rules_for=ruleset.specs_for, oracle_snippet_for=ruleset.oracle_snippet_for)
            judged = judge_kept(all_items, backend=backend, config=config, entries=entries,
                                judge=judge_spec, guard=held)
    except (guard_module.Refused, guard_module.Overlap) as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - the judging directory is left for a look
        traceback.print_exc()
        print(f"stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    for name, label in judged.labels.items():
        print(f"{name}: {label}")
    print(f"{judged.judged} of {len(all_items)} sessions judged; {judged.waiting} waiting")
    if judged.stopped is not None:
        print(f"stopped: {judged.stopped}", file=sys.stderr)
        return 1
    return 0 if judged.waiting == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
