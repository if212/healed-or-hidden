"""A resumable, pausable job queue.

The work of a job is a function passed in. All state changes happen in the calling thread: worker
threads only run the function and report back through a thread-safe queue, so there is no shared
mutable state to race on. Records are written atomically.

Every attempt at a job runs in a directory of its own, `out_dir/<job.path>/attempt-<k>`, so a
retried job never finds the files of an earlier attempt. The job's record, `out_dir/<job.path>/
job.json`, names the attempt that produced its state (`attempt_dir`, relative to the record's
directory) and lists every attempt with its state.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from typing import Any, Literal

from hoh.jobs import Job

State = Literal["done", "env_failed", "limit", "error"]
DEFAULT_PAUSE_SECONDS = 15 * 60
LOCK_NAME = ".queue.lock"
_RECORDED = ("done", "error", "env_failed")
# A restart runs every job whose record does not say one of these: an error is recorded and not
# retried, while an environment failure is retried (CLAUDE.md, rule 9).
_FINAL = ("done", "error")


@dataclass(frozen=True)
class Outcome:
    state: State
    detail: str = ""
    resume_at: float | None = None


MIN_FREE_GIB = 10
"""Free space a run of jobs needs where it writes, to start: every attempt keeps its runs."""


def disk_problem(out_dir: Path, min_free_gib: float = MIN_FREE_GIB) -> str | None:
    """Why the disk under ``out_dir`` is too full to start, or None."""
    existing = Path(out_dir).resolve()
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    free = shutil.disk_usage(existing).free / 2**30
    if free < min_free_gib:
        return (f"only {free:.1f} GiB free under {out_dir}; at least {min_free_gib:g} GiB "
                f"are needed (every attempt keeps its runs, logs and screenshots)")
    return None


def atomic_write_json(path: Path, data: Any) -> None:
    """Write to a temporary name in the same directory, fsync, then rename over `path`.

    A reader sees the old file or the new file, never a part of one. On failure the temporary
    file is removed and `path` is untouched.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, sort_keys=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    try:  # make the rename itself durable; not available everywhere
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


_ATTEMPT_NAME = re.compile(r"attempt-(\d+)")


def _read_record(out_dir: Path, job: Job) -> dict | None:
    """The job's `job.json`, or None when there is none or it cannot be read."""
    try:
        rec = json.loads((out_dir / job.path / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _attempts_on_disk(job_dir: Path) -> list[int]:
    """The numbers of the `attempt-<k>` entries in `job_dir`, smallest first."""
    if not job_dir.is_dir():
        return []
    return sorted(
        int(m.group(1)) for p in job_dir.iterdir() if (m := _ATTEMPT_NAME.fullmatch(p.name))
    )


@contextmanager
def _sole_queue(out_dir: Path) -> Iterator[None]:
    """Hold an exclusive lock on `out_dir` while the queue runs.

    Two queues on one directory would run every pending job twice. The lock file stays when the
    queue ends (closing it releases the lock): deleting it would let a third queue lock a new
    file while a second one still holds the old one.
    """
    lock_path = out_dir / LOCK_NAME
    with open(lock_path, "a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                f"another queue is already running on {out_dir} ({lock_path} is locked)"
            ) from None
        yield


def run_queue(
    jobs: Sequence[Job],
    worker: Callable[[Job, Path], Outcome],
    out_dir: Path,
    *,
    slots: int,
    max_env_retries: int = 2,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    on_summary: Callable[[dict], None] | None = None,
    retry: Collection[Job] = (),
) -> dict:
    """Run `jobs` in the given order with at most `slots` workers at once; return the summary.

    Jobs whose `job.json` says `done` or `error` are skipped, so calling this again with the same
    jobs and directory runs exactly the others (no record, or `env_failed`), in the same order.
    A job in `retry` whose record says `error` is run again too.

    The worker gets the job and the directory of this attempt, `out_dir/<job.path>/attempt-<k>`,
    new and empty, where k is one more than the highest attempt number already on disk.

    Only one queue runs on an output directory at a time: while one holds `out_dir/.queue.lock`,
    another raises RuntimeError before it reads or runs anything.
    """
    if slots < 1:
        raise ValueError("slots must be >= 1")
    jobs = list(jobs)
    if len({j.job_id for j in jobs}) != len(jobs):
        raise ValueError("duplicate jobs")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with _sole_queue(out_dir):
        return _run(jobs, worker, out_dir, slots, max_env_retries, clock, sleep, on_summary,
                    frozenset(retry))


def _run(
    jobs: list[Job],
    worker: Callable[[Job, Path], Outcome],
    out_dir: Path,
    slots: int,
    max_env_retries: int,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    on_summary: Callable[[dict], None] | None,
    retry: frozenset[Job] = frozenset(),
) -> dict:
    """The body of `run_queue`, run while the lock is held."""
    index = {j: i for i, j in enumerate(jobs)}

    states: dict[Job, str] = {}  # recorded state of each job, from disk or from this run
    history: dict[Job, list[dict]] = {}  # finished attempts, from earlier records and this run
    for j in jobs:
        rec = _read_record(out_dir, j)
        if rec is None:
            continue
        if rec.get("state") in _FINAL and not (rec.get("state") == "error" and j in retry):
            states[j] = rec["state"]
        earlier = rec.get("attempts")
        if isinstance(earlier, list):
            history[j] = [
                a for a in earlier if isinstance(a, dict) and isinstance(a.get("attempt"), int)
            ]
    pending: deque[Job] = deque(j for j in jobs if j not in states)
    env_failures: dict[Job, int] = {}  # environment failures of each job in this run
    current: dict[Job, int] = {}  # attempt number of each running job
    env_retries = 0
    pauses: list[dict] = []
    pause: dict | None = None

    def summarize() -> dict:
        def empty():
            return {"done": 0, "error": 0, "env_failed": 0}

        by_arm: dict[str, dict] = {}
        by_app: dict[str, dict] = {}
        counts = empty()
        for j in jobs:
            by_arm.setdefault(j.arm, empty())
            by_app.setdefault(j.app, empty())
            if j in states:
                counts[states[j]] += 1
                by_arm[j.arm][states[j]] += 1
                by_app[j.app][states[j]] += 1
        counts["pending"] = len(jobs) - sum(counts.values())
        return {
            "total": len(jobs),
            "counts": counts,
            "by_arm": by_arm,
            "by_app": by_app,
            "env_retries": env_retries,
            "paused": pause is not None,
            "pauses": [dict(p) for p in pauses] + ([pause_view()] if pause else []),
            "last_change": clock(),
        }

    def pause_view() -> dict:
        assert pause is not None
        return {
            "start": pause["start"],
            "end": None,
            "cause": pause["cause"],
            "resume_at": max(pause["resume_ats"]),
            "jobs": list(pause["jobs"]),
        }

    def publish() -> dict:
        summary = summarize()
        atomic_write_json(out_dir / "summary.json", summary)
        if on_summary is not None:
            on_summary(summary)
        return summary

    def record(job: Job, state: str, detail: str, attempt: int) -> None:
        job_dir = out_dir / job.path
        job_dir.mkdir(parents=True, exist_ok=True)
        listed = {a["attempt"]: a for a in history.get(job, [])}
        for k in _attempts_on_disk(job_dir):  # attempts whose end nobody recorded
            listed.setdefault(
                k, {"attempt": k, "dir": f"attempt-{k}", "state": "unknown", "detail": ""}
            )
        atomic_write_json(
            job_dir / "job.json",
            {
                "job_id": job.job_id,
                "stage": job.stage,
                "family": job.family,
                "app": job.app,
                "task": job.task,
                "arm": job.arm,
                "rep": job.rep,
                "state": state,
                "detail": detail,
                "attempt_dir": f"attempt-{attempt}",
                "attempts": [listed[k] for k in sorted(listed)],
                "finished_at": clock(),
            },
        )
        states[job] = state

    completions: Queue[tuple[Job, Outcome | KeyboardInterrupt]] = Queue()
    threads: list[threading.Thread] = []

    def run_one(job: Job, attempt_dir: Path) -> None:
        # Every way out of here must report, or the main thread waits for this job forever.
        outcome: Outcome | KeyboardInterrupt
        try:
            outcome = worker(job, attempt_dir)
            if not isinstance(outcome, Outcome) or outcome.state not in (
                "done", "env_failed", "limit", "error"
            ):
                outcome = Outcome("error", f"worker returned {outcome!r}, not an Outcome")
        except KeyboardInterrupt as stop:  # an operator's stop: the main thread raises it
            outcome = stop
        except BaseException:  # noqa: BLE001 - SystemExit too: a worker may fail in any way
            outcome = Outcome("error", traceback.format_exc())
        completions.put((job, outcome))

    running: set[Job] = set()
    try:
        while True:
            if pause is None:
                while pending and len(running) < slots:
                    job = pending.popleft()
                    job_dir = out_dir / job.path
                    current[job] = max(_attempts_on_disk(job_dir), default=0) + 1
                    attempt_dir = job_dir / f"attempt-{current[job]}"
                    attempt_dir.mkdir(parents=True)  # new: never an earlier attempt's directory
                    t = threading.Thread(target=run_one, args=(job, attempt_dir), daemon=True)
                    running.add(job)
                    threads.append(t)
                    t.start()
            if not running:
                if pause is not None:
                    resume_at = max(pause["resume_ats"])
                    remaining = resume_at - clock()
                    if remaining > 0:
                        sleep(remaining)
                    for j in sorted(pause["jobs_objs"], key=index.__getitem__, reverse=True):
                        pending.appendleft(j)
                    pauses.append(
                        {
                            "start": pause["start"],
                            "end": clock(),
                            "cause": pause["cause"],
                            "resume_at": resume_at,
                            "jobs": list(pause["jobs"]),
                        }
                    )
                    pause = None
                    publish()
                    continue
                if not pending:
                    break
                continue
            job, outcome = completions.get()
            running.discard(job)
            if isinstance(outcome, KeyboardInterrupt):
                raise outcome  # the job stays unrecorded, so a restart runs it again
            attempt = current.pop(job)
            history.setdefault(job, []).append(
                {
                    "attempt": attempt,
                    "dir": f"attempt-{attempt}",
                    "state": outcome.state,
                    "detail": outcome.detail,
                }
            )
            if outcome.state == "limit":
                now = clock()
                if pause is None:
                    pause = {
                        "start": now,
                        "cause": outcome.detail or "limit",
                        "resume_ats": [],
                        "jobs": [],
                        "jobs_objs": [],
                    }
                pause["resume_ats"].append(
                    outcome.resume_at if outcome.resume_at is not None
                    else now + DEFAULT_PAUSE_SECONDS
                )
                pause["jobs"].append(job.job_id)
                pause["jobs_objs"].append(job)
                publish()
            elif outcome.state == "env_failed":
                env_failures[job] = env_failures.get(job, 0) + 1
                if env_failures[job] <= max_env_retries:
                    env_retries += 1
                    # Back at the end of its own repetition, before any later one: stopping
                    # early must leave the repetitions balanced.
                    later = (i for i, j in enumerate(pending) if j.rep > job.rep)
                    pending.insert(next(later, len(pending)), job)
                else:
                    record(job, "env_failed", outcome.detail, attempt)
                    publish()
            else:
                record(job, outcome.state, outcome.detail, attempt)
                publish()
    finally:
        for t in threads:
            t.join()
    return publish()
