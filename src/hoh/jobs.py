"""The session matrix and the order it is run in.

A job is one (stage, family, app, task, arm, rep). `build_jobs` lists them, `order` puts them in
the sequence the queue runs, chosen so that stopping at any moment leaves balanced data.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

# Words that must never appear in anything an agent can read (CLAUDE.md, rule 6). Job ids become
# directory names, so they are checked here.
FORBIDDEN_WORDS = ("bug", "inject", "benign", "scenario")
_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


def _check_part(name: str, value: str) -> None:
    if not isinstance(value, str) or not _PART.match(value) or ".." in value:
        raise ValueError(f"{name} {value!r} is not a safe path component")
    low = value.lower()
    for word in FORBIDDEN_WORDS:
        if word in low:
            raise ValueError(f"{name} {value!r} contains the reserved word {word!r}")


@dataclass(frozen=True, order=True)
class Job:
    stage: str
    family: str
    app: str
    task: str
    arm: str
    rep: int

    def __post_init__(self) -> None:
        for name in ("stage", "family", "app", "task", "arm"):
            _check_part(name, getattr(self, name))
        if not isinstance(self.rep, int) or isinstance(self.rep, bool) or self.rep < 1:
            raise ValueError(f"rep must be an integer >= 1, got {self.rep!r}")

    @property
    def job_id(self) -> str:
        return f"{self.stage}/{self.family}/{self.app}/{self.task}/{self.arm}/{self.rep}"

    @property
    def path(self) -> PurePosixPath:
        """Relative path, to be used only under an output directory."""
        return PurePosixPath(self.job_id)


def build_jobs(
    stage: str,
    family: str,
    tasks: Mapping[str, Sequence[str]],
    arms: Sequence[str],
    reps: int,
) -> list[Job]:
    """Every (app, task, arm, rep) for one stage and family, repetition first."""
    arms = list(arms)
    if not arms:
        raise ValueError("at least one arm is required")
    if len(set(arms)) != len(arms):
        raise ValueError("arms must be unique")
    if reps < 1:
        raise ValueError("reps must be >= 1")
    jobs: list[Job] = []
    for app, app_tasks in tasks.items():
        if len(set(app_tasks)) != len(app_tasks):
            raise ValueError(f"duplicate task in {app!r}")
    for rep in range(1, reps + 1):
        for app, app_tasks in tasks.items():
            for task in app_tasks:
                for arm in arms:
                    jobs.append(Job(stage, family, app, task, arm, rep))
    return jobs


def _first_seen(items) -> list:
    return list(dict.fromkeys(items))


def order(jobs: Sequence[Job]) -> list[Job]:
    """Run order: repetition first; inside a repetition, for each (stage, family) group, rounds
    over the apps' tasks (round k takes every app's k-th task), and inside a round apps one after
    the other with all arms of one task together.

    Every prefix is balanced: arm counts differ by at most one overall and inside each
    (repetition, app); apps differ by at most one task block while all of them have tasks left.
    The result depends only on the jobs and on the order in which apps, tasks and arms first
    appear in the input (arms are never reordered, apps and tasks are not sorted).
    """
    if len(set(jobs)) != len(jobs):
        raise ValueError("duplicate jobs")
    result: list[Job] = []
    for rep in sorted({j.rep for j in jobs}):
        in_rep = [j for j in jobs if j.rep == rep]
        for stage, family in _first_seen((j.stage, j.family) for j in in_rep):
            group = [j for j in in_rep if (j.stage, j.family) == (stage, family)]
            apps = _first_seen(j.app for j in group)
            arms = _first_seen(j.arm for j in group)
            tasks = {a: _first_seen(j.task for j in group if j.app == a) for a in apps}
            by_key = {(j.app, j.task, j.arm): j for j in group}
            for k in range(max(len(t) for t in tasks.values())):
                for app in apps:
                    if k >= len(tasks[app]):
                        continue
                    for arm in arms:
                        job = by_key.get((app, tasks[app][k], arm))
                        if job is not None:
                            result.append(job)
    return result
