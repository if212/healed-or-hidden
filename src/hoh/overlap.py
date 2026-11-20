"""Which judged jobs of a stage met a test run on the same machine (design doc §7).

Some test runs met the study's stages; design doc §7 asks each stage report to name them. For
one stage this lists the judged jobs any part of which, from the start of the session to the
end of the last judging, on any slot, fell inside a test run, from its start to its end. A job
is in one of three groups: it met a run of the whole suite (a pytest run over the whole test
directory that ran its tests, whatever it left out); it met only shorter runs (a few test
files); or it met no test run. The module counts the groups per family, arm and repetition,
counts apart the jobs that met a run with browser tests (on a local Chrome), and sets
exploratory, descriptive indicators for the groups side by side. The groups differ in length,
arm and repetition, so the indicators are not evidence of a load effect either way. It reads
the stage's results and the windows file
(``docs/runs/test-run-windows.yaml``: per stage, each run's start and end with their offset,
what it was, whether it was the whole suite and whether it ran browser tests) and writes one
Markdown file, which names the commit it was written at:

    uv run python -m hoh.overlap --stage 1b --out docs/runs/1b-overlap.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

import yaml

from hoh.analysis import AnalysisRefused, Session, Stage, _git, git_object, load_stage

WINDOWS = Path("docs/runs/test-run-windows.yaml")
MODULE = Path("src/hoh/overlap.py")
#: The module that reads a stage's records for the lists (``load_stage``).
LOADER = Path("src/hoh/analysis.py")


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    what: str
    whole: bool = False  # pytest over the whole test directory that ran its tests
    browser: bool = False  # it ran browser tests, on a local Chrome

    def meets(self, start: float, end: float) -> bool:
        return start < self.end.timestamp() and self.start.timestamp() < end


@dataclass(frozen=True)
class Row:
    session: Session
    met: tuple[int, ...]  # the windows, numbered from 1, that any part of the job met
    judging_met: tuple[int, ...]  # those that one of its judgings met
    whole: bool = False  # one of the windows it met was a whole-suite run
    browser: bool = False  # one of the windows it met ran browser tests

    @property
    def where(self) -> str:
        return "whole suite" if self.whole else "short runs only" if self.met else "no test run"


@dataclass
class Indicators:
    jobs: int = 0
    cells: int = 0  # judging cells (one variant of one judging) that ran
    baseline_cells: int = 0  # cells taken from the baseline: T' unchanged, the judge ran nothing
    third_run: int = 0  # of them, those that needed a third run
    judge_runs: int = 0
    judge_timed_out: int = 0
    judge_broken: int = 0
    agent_runs: int = 0  # the agent's own run_test runs
    agent_timed_out: int = 0
    agent_broken: int = 0
    retried: int = 0  # attempts before the counting one: sessions discarded and queued again
    env_retries: int = 0  # the judgings' environment retries


def _moment(value: object, where: str) -> datetime:
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(moment, datetime):
        raise ValueError(f"{where}: not a time")
    if moment.utcoffset() is None:
        raise ValueError(f"{where}: give the time with its offset")
    return moment


def load_windows(path: Path, stage: str) -> list[Window]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} is not YAML: {exc}") from exc
    entries = data.get(stage) if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: no test runs listed for {stage}")
    windows = []
    for n, entry in enumerate(entries, start=1):
        where = f"{path}: {stage}, run {n}"
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: {stage}, run {n} is not a mapping: {entry!r}")
        start, end = (_moment(entry.get(key), f"{where}, {key}") for key in ("start", "end"))
        if end <= start:
            raise ValueError(f"{where}: its end must come after its start")
        flags = {key: entry.get(key, False) for key in ("whole", "browser")}
        for key, flag in flags.items():
            if not isinstance(flag, bool):
                raise ValueError(f"{where}: {key} must be true or false")
        windows.append(Window(start, end, str(entry.get("what") or ""), **flags))
    return windows


def _spans(record: dict, job_id: str) -> tuple[tuple[float, float], list[tuple[float, float]]]:
    """The whole job's span, and each judging's."""
    timings = record.get("timings") or {}
    start, end = timings.get("started_at"), timings.get("finished_at")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        raise ValueError(f"{job_id}: its record has no start or end time")
    if not record.get("judgings"):
        raise ValueError(f"{job_id}: judged, but its record lists no judgings")
    judgings = []
    for n, judging in enumerate(record["judgings"], start=1):
        begun, seconds = judging.get("started_at"), judging.get("seconds")
        if not isinstance(begun, (int, float)) or not isinstance(seconds, (int, float)):
            raise ValueError(f"{job_id}: judging {judging.get('n', n)} has no start or length")
        judgings.append((begun, begun + seconds))
    return (start, max([end, *(e for _, e in judgings)])), judgings


def rows(stage: Stage, windows: list[Window]) -> list[Row]:
    """A row for each judged job, in the order of the stage's index."""
    found = []
    for session in stage.sessions:
        if session.record_state != "judged":
            continue
        job, judgings = _spans(session.record, session.job_id)
        met = tuple(n for n, w in enumerate(windows, start=1) if w.meets(*job))
        judging_met = tuple(n for n, w in enumerate(windows, start=1)
                            if any(w.meets(*span) for span in judgings))
        found.append(Row(session, met, judging_met, any(windows[n - 1].whole for n in met),
                         any(windows[n - 1].browser for n in met)))
    return found


def _read(path: Path, job_id: str) -> dict:
    """A JSON object the job must have: a missing one is refused, never counted as zero."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{job_id}: cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{job_id}: {path} is not a JSON object")
    return value


def _count(row: Row, into: Indicators) -> None:
    attempt, record, job_id = row.session.attempt_dir, row.session.record, row.session.job_id
    into.jobs += 1
    into.retried += len(row.session.retried)
    for judging in record.get("judgings") or []:
        judge = attempt / str(judging.get("dir") or "")
        judgment = _read(judge / "judgment.json", job_id)
        for cell in judgment.get("cells") or []:
            runs = cell.get("runs") or []
            if cell.get("source") == "baseline":
                into.baseline_cells += 1
                continue
            if not runs:
                raise ValueError(f"{job_id}: a cell of {judge / 'judgment.json'} that ran lists "
                                 "no runs")
            into.cells += 1
            into.third_run += len(runs) >= 3
            for listed in runs:
                run = _read(judge / str(listed.get("dir") or "") / "result.json", job_id)
                into.judge_runs += 1
                into.judge_timed_out += run.get("timed_out") is True
                into.judge_broken += run.get("status") == "broken"
        if not isinstance(judgment.get("env_retries"), int):
            raise ValueError(f"{job_id}: {judge / 'judgment.json'} gives no env_retries")
        into.env_retries += judgment["env_retries"]
    agent = attempt / "session" / "runs"
    for folder in sorted(agent.iterdir()) if agent.is_dir() else []:
        if folder.is_dir():
            run = _read(folder / "result.json", job_id)
            into.agent_runs += 1
            into.agent_timed_out += run.get("timed_out") is True
            into.agent_broken += run.get("status") == "broken"


def indicators(found: list[Row]) -> dict[tuple[str, str, str], Indicators]:
    """Per family, arm and group (``Row.where``)."""
    table: dict[tuple[str, str, str], Indicators] = defaultdict(Indicators)
    for row in found:
        _count(row, table[(row.session.family, row.session.arm, row.where)])
    return dict(table)


def _numbers(ns: tuple[int, ...]) -> str:
    return ", ".join(map(str, ns)) or "none"


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


GROUPS = ("whole suite", "short runs only", "no test run")


def _counts(found: list[Row], reps: list[int], groups: list[tuple[str, str]],
            counted: Callable[[Row], bool]) -> list[str]:
    lines = ["| Family | Arm | " + " | ".join(f"Repetition {r}" for r in reps) + " | All |",
             "| :- | :- | " + " | ".join("-:" for _ in reps) + " | -: |"]
    for family, arm in groups:
        cells = []
        for rep in (*reps, None):
            chosen = [row for row in found if (row.session.family, row.session.arm) ==
                      (family, arm) and rep in (None, row.session.rep)]
            cells.append(f"{sum(counted(row) for row in chosen)}/{len(chosen)}")
        lines.append(f"| {family} | {arm} | " + " | ".join(cells) + " |")
    return lines


def written_at(repo: Path, windows: Path) -> str:
    """The commit the list is written at, and whether the module or the windows file differ
    from it."""
    head = _git(repo, "rev-parse", "HEAD")
    if head is None:
        return "outside a git checkout"
    status = _git(repo, "status", "--porcelain", "--", str(MODULE), str(LOADER),
                  str(windows.resolve()))
    where = f"at commit `{head.strip()[:12]}`"
    if status != "":
        where += (", with changes to the module, `hoh.analysis` or the windows file not "
                  "committed,")
    return where


def render(stage: str, windows: list[Window], found: list[Row],
           source: str = "from the stage's results and `docs/runs/test-run-windows.yaml`"
           ) -> str:
    reps = sorted({row.session.rep for row in found})
    groups = sorted({(row.session.family, row.session.arm) for row in found})
    names = [f.name for f in fields(Indicators)]
    lines = [
        f"# Stage {stage}: the jobs that met a test run", "",
        f"Written by `python -m hoh.overlap --stage {stage}` {source}; not edited by hand.", "",
        "A judged job met a test run when any part of it, from the start of its session to the "
        "end of its last judging, on any slot, fell inside the run, from the run's start to its "
        "end. Each job is in one group: it met a run of the whole suite; it met only shorter "
        "runs; or it met no test run. A whole-suite run is a pytest run over the whole test "
        "directory that ran its tests, whatever it left out: the table of runs says what each "
        "one ran. Apart from "
        "the groups, a job met a run with browser tests when one of the runs it met ran browser "
        "tests on a local Chrome.", "",
        "## The test runs", "", "| Run | Start | End | Whole suite | Browser tests | What |",
        "| -: | :- | :- | :- | :- | :- |",
        *(f"| {n} | {w.start.isoformat()} | {w.end.isoformat()} | {_yes(w.whole)} | "
          f"{_yes(w.browser)} | {w.what} |" for n, w in enumerate(windows, start=1)),
        "", "## Judged jobs that met a whole-suite run", "",
        *_counts(found, reps, groups, lambda row: row.where == "whole suite"),
        "", "## Judged jobs that met only shorter runs", "",
        *_counts(found, reps, groups, lambda row: row.where == "short runs only"),
        "", "## Judged jobs that met a run with browser tests", "",
        *_counts(found, reps, groups, lambda row: row.browser),
        "", "## Indicators per group", "",
        "Judge and agent runs are counted apart: the agent's are its own `run_test` runs. A cell "
        "is one variant of one judging; a cell taken from the baseline (T' unchanged, so the "
        "judge ran nothing) is counted apart from the cells that ran. `retried` counts a job's "
        "attempts before the one that counts: sessions the harness discarded and queued "
        "again. These indicators are "
        "exploratory and descriptive: the analysis plan does not name them, and the groups "
        "differ in length, arm and repetition.", "",
        "| Family | Arm | Group | " + " | ".join(names) + " |",
        "| :- | :- | :- | " + " | ".join("-:" for _ in names) + " |"]
    table = indicators(found)
    for family, arm in groups:
        for where in GROUPS:
            counts = table.get((family, arm, where))
            if counts is not None:
                lines.append(f"| {family} | {arm} | {where} | "
                             + " | ".join(str(getattr(counts, name)) for name in names) + " |")
    lines += ["", "## The jobs that met a test run", "",
              "| Job | Slot | Runs met | Met by a judging | Group | Browser tests |",
              "| :- | -: | :- | :- | :- | :- |"]
    for row in found:
        if row.met:
            slot = (row.session.record.get("slot") or {}).get("index", "")
            lines.append(f"| {row.session.job_id} | {slot} | {_numbers(row.met)} | "
                         f"{_numbers(row.judging_met)} | {row.where} | {_yes(row.browser)} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hoh.overlap", description=__doc__.split(
        "\n\n")[0])
    parser.add_argument("--stage", required=True)
    parser.add_argument("--results", type=Path, default=Path("results"))
    parser.add_argument("--windows", type=Path, default=WINDOWS)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path("."),
                        help="the checkout whose commit the list names (default: here)")
    args = parser.parse_args(argv)
    try:
        windows = load_windows(args.windows, args.stage)
        found = rows(load_stage(args.results, args.stage), windows)
        try:
            name = args.windows.resolve().relative_to(args.repo.resolve()).as_posix()
        except ValueError:
            name = args.windows.name
        source = (f"{written_at(args.repo, args.windows)} from the stage's results and "
                  f"`{name}` (git object `{git_object(args.windows)}`)")
        text = render(args.stage, windows, found, source)
    except (AnalysisRefused, ValueError, OSError) as exc:
        print(f"overlap: {exc}", file=sys.stderr)
        return 2
    args.out.write_text(text, encoding="utf-8")
    print(f"overlap: {args.stage}: of {len(found)} judged jobs, "
          f"{sum(row.whole for row in found)} met a whole-suite run and "
          f"{sum(row.where == 'short runs only' for row in found)} only shorter runs; "
          f"{sum(row.browser for row in found)} met a run with browser tests; "
          f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
