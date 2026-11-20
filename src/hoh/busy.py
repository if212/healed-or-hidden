"""Is a stage or a validation of the study running on this machine?

Work that would load the machine beside the study's runs asks here first and stands aside
(design doc §7). The test suite does (``tests/conftest.py``), and so does the fidelity check
(design doc §5.6), which runs its stacks as the Compose projects ``hohfid-<k>``. Two signs,
either one enough:

- a container of a study Compose project is up: ``hoh-<k>`` (a stage's slots) or ``hohv-<k>``
  (a validation's), and for the test suite also ``hohfid-<k>`` (``WITH_FIDELITY``; the
  fidelity check leaves its own out, or its parallel processes would stop each other). The
  names are matched whole, so the real-Docker test's ``hoh-check-<k>`` never counts;
- a stage holds its first slot's lock. ``SlotPool.claim`` locks every slot of its slots root,
  slot 1 first, exclusively and without waiting, for as long as the stage runs; every stage
  config uses ``/Users/Shared/w``.

``shared_hold`` takes a shared lock on that file without waiting and keeps it while a block
runs, so a stage started meanwhile is refused (``SlotsBusy``) before it does anything, and can
be started again afterwards; any number of shared holds go together. Nothing here creates the
lock file or its directory, so where it does not exist yet a hold holds nothing; every stage
leaves it behind. A validation holds no file lock; its containers show it, except in
the seconds between two of its stacks, when a check sees nothing of it.

``log_test_run`` keeps a line for each start and end of a test session, outside the
repositories, so that a later audit of what ran beside a stage reads one file. ``TEST_LOCKS``
is the test sessions' Compose lock directory, beside that log.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

STAGE_SLOT_LOCK = Path("/Users/Shared/w/1.lock")
# The Compose projects of the study's runs: a stage's slots (hoh-<k>, the read-block checks
# included) and a validation's (hohv-<k>). Not counted: the real-Docker test's hoh-check-<k>,
# and the snapshot command's hohs-<k>, which is made to run beside them.
STUDY = ("hoh", "hohv")
WITH_FIDELITY = (*STUDY, "hohfid")
PS = ["docker", "ps", "--format", '{{.Label "com.docker.compose.project"}}']
PS_TIMEOUT_S = 30
TEST_LOG = Path.home() / ".local" / "state" / "hoh" / "test-runs.log"
# Every test session's Compose locks (``HOH_LOCK_DIR``), apart from the stages', which live in
# ``$HOH_LOCK_DIR/hoh-compose`` when the stage's shell exports it and in the temp directory's
# hoh-compose otherwise: a test never takes a stage's, and the real-Docker tests of two
# sessions (one project, hoh-check-1) wait for each other.
TEST_LOCKS = TEST_LOG.parent / "test-locks"

Run = Callable[..., subprocess.CompletedProcess]


class CannotTell(Exception):
    """Docker did not answer in time: it may be busy with the study's stacks."""


def study_projects(run: Run = subprocess.run, prefixes: Sequence[str] = STUDY) -> list[str]:
    """The study Compose projects (``<prefix>-<k>``) with a container up, sorted. An empty list
    when Docker is missing or refuses: no stage or validation runs without it. Raises
    ``CannotTell`` when `docker ps` does not answer in time."""
    try:
        done = run(PS, capture_output=True, text=True, timeout=PS_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise CannotTell(f"docker ps did not answer within {PS_TIMEOUT_S} s") from exc
    except OSError:
        return []
    if done.returncode != 0:
        return []
    project = re.compile(rf"(?:{'|'.join(map(re.escape, prefixes))})-\d+")
    names = {line.strip() for line in (done.stdout or "").splitlines()}
    return sorted(name for name in names if project.fullmatch(name))


@contextmanager
def shared_hold(lock: Path = STAGE_SLOT_LOCK) -> Iterator[bool]:
    """Hold a shared lock on ``lock`` while the block runs, and yield True; yield False, holding
    nothing, when a stage holds it. A missing file yields True: no stage has used that root."""
    try:
        handle = open(lock)  # noqa: SIM115 - read mode: a missing file is not created
    except FileNotFoundError:
        yield True
        return
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True  # closing the file releases the lock


def stage_holds(lock: Path = STAGE_SLOT_LOCK) -> bool:
    """True when a stage holds ``lock``; the probe's own lock is let go at once."""
    with shared_hold(lock) as free:
        return not free


def study_active(
    lock: Path = STAGE_SLOT_LOCK, run: Run = subprocess.run, prefixes: Sequence[str] = STUDY
) -> str | None:
    """Why the study's machine is busy, in a sentence, or None when it is not."""
    reasons = []
    try:
        projects = study_projects(run, prefixes)
    except CannotTell as exc:
        reasons.append(f"cannot tell whether study stacks are up ({exc})")
    else:
        if projects:
            reasons.append(f"study stacks are up ({', '.join(projects)})")
    if stage_holds(lock):
        reasons.append(f"a stage holds {lock}")
    return "; ".join(reasons) or None


def checkout_state(directory: Path, run: Run = subprocess.run) -> dict[str, object]:
    """The commit checked out in ``directory`` and whether its tree is clean (``git status
    --porcelain`` prints nothing), each None when git cannot tell. The log of test runs keeps
    both, so that a run can be tied to the commit it ran at."""

    def git(*args: str) -> str | None:
        try:
            done = run(["git", "--no-optional-locks", "-C", str(directory), *args],
                       capture_output=True, text=True, timeout=PS_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return done.stdout if done.returncode == 0 else None

    commit, status = git("rev-parse", "HEAD"), git("status", "--porcelain")
    return {"commit": None if commit is None else commit.strip(),
            "clean": None if status is None else status.strip() == ""}


def log_test_run(fields: Mapping[str, object], log: Path | None = None) -> None:
    """Append one JSON line to the log of test runs (``HOH_TEST_LOG``, else ``TEST_LOG``): the
    local time with its offset, the process, and ``fields``."""
    log = log or Path(os.environ.get("HOH_TEST_LOG") or TEST_LOG)
    line = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "pid": os.getpid(),
            **fields}
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
