"""The fidelity check never runs beside a stage or a validation of the study (design doc §7).

Every command of the check that starts a container (the image build, the race, and later the
judge and the session launcher) runs inside ``hold``. It refuses to start (``Refused``) when

- a container of one of the study's Compose projects is up: ``hoh-<k>`` (a stage's slots),
  ``hohv-<k>`` (a validation's) or ``hoh-check-<k>`` (the real-Docker test). Names are matched
  whole, so the check's own ``hohfid-<k>`` never counts, nor does the snapshot command's
  ``hohs-<k>``. A ``docker ps`` that does not answer in time refuses too: Docker may be busy
  with the study's stacks;
- a stage holds its first slot's lock, ``/Users/Shared/w/1.lock`` (``hoh.busy``): every stage
  config uses that slots root, and ``SlotPool.claim`` locks slot 1 first, for as long as the
  stage runs.

While the command runs it holds a shared lock on that file (``hoh.busy.shared_hold``). The
check's processes (the race runs three) hold it together, and a stage started meanwhile is
refused with ``SlotsBusy`` before it does anything. A validation takes no lock, so
``Guard.before_run`` looks at the containers again before each run of a stack. If a study
project is up, it writes a record into ``build/fidelity/overlaps/`` (the time, the command, the
projects and the cells that were running) and raises ``Overlap``. The command stops, and those
cells are run again later from the start. The record goes to the stage session and into the
report, so that the author can decide whether that validation is repeated. The stage session's
written confirmation of each window is the main control; this is the backup. The guard only
takes and releases a lock: it never creates or deletes a lock file.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from hoh import busy
from hoh.benchmark import REPO_ROOT

#: The study's Compose projects, by prefix: a stage's, a validation's, the real-Docker test's.
PREFIXES = ("hoh", "hohv", "hoh-check")
OVERLAPS = REPO_ROOT / "build" / "fidelity" / "overlaps"


class Refused(Exception):
    """The command may not start: the study is running, or Docker cannot tell."""


class Overlap(Exception):
    """A study project came up while the command ran: it stopped, and a record says so."""


def _study(run: busy.Run) -> tuple[list[str], str | None]:
    """The study projects that are up, and why it cannot tell (None when it can)."""
    try:
        return busy.study_projects(run, PREFIXES), None
    except busy.CannotTell as exc:
        return [], f"cannot tell whether study stacks are up ({exc})"


@dataclass(frozen=True)
class Guard:
    """What ``hold`` gives the command: the check before each run of a stack."""

    command: str
    run: busy.Run
    overlaps: Path

    def before_run(self, cells: Sequence[str]) -> None:
        """Raise ``Overlap``, after writing its record, when a study project is up now."""
        projects, unknown = _study(self.run)
        if not projects and unknown is None:
            return
        at = datetime.now().astimezone()
        record = {
            "at": at.isoformat(timespec="seconds"),
            "command": self.command,
            "projects": projects,
            "unknown": unknown,
            "cells": list(cells),
        }
        self.overlaps.mkdir(parents=True, exist_ok=True)
        path = self.overlaps / f"{at:%Y%m%dT%H%M%S}-{os.getpid()}.json"
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        what = f"study stacks are up ({', '.join(projects)})" if projects else unknown
        raise Overlap(f"{self.command}: {what}; stopped, and {path} records it")


@contextmanager
def hold(
    command: str,
    *,
    lock: Path = busy.STAGE_SLOT_LOCK,
    run: busy.Run = subprocess.run,
    overlaps: Path = OVERLAPS,
) -> Iterator[Guard]:
    """Run the block only when no stage or validation runs, holding the stage's first slot
    lock shared meanwhile; raise ``Refused`` otherwise."""
    projects, unknown = _study(run)
    if unknown is not None:
        raise Refused(f"{command}: {unknown}")
    if projects:
        raise Refused(f"{command}: study stacks are up ({', '.join(projects)})")
    with busy.shared_hold(Path(lock)) as free:
        if not free:
            raise Refused(f"{command}: a stage holds {lock}")
        yield Guard(command, run, Path(overlaps))
