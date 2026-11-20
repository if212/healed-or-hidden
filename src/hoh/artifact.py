"""The public artifact's session tables, one entry per session (design doc section 1).

    python -m hoh.artifact --results RESULTS --stages 1a 1b 2-mixed 2-c-assert explore \
        --provenance build/analysis/provenance.json --out DIR

The stages are loaded as ``hoh.analysis`` loads them, with every check of ``load_stage``. The
provenance file must be the frozen analysis's own (its SHA-256 is ``FROZEN_PROVENANCE``), and
what the export reads must be what that analysis read: each stage's job records
(``check_provenance``), and the coding files and the selection (``check_inputs``). The hash of
the job records does not cover the final messages or the test files, which are read as they are
on disk. The defect codes are the adjudicated codes of ``docs/coding/<stage>/``, read with
``hoh.analysis.load_codes`` once their gate says ready; a reviewed stage without them refuses the
export, and the exploratory stage has none. Nothing is written under the results root, and
nothing when the export is refused.

``sessions.csv`` has one row per session (``COLUMNS``): what the session was, its label and the
name the paper gives that outcome (``OUTCOMES``), whether its test is in the dev split, how its
returned test changed (the record's mechanism kind), its use of ``run_test``, turns and cost,
and its four adjudicated codes. No recorded session ended at a cap or was run again, so neither
has a column.

``sessions.jsonl`` has one line per session, in the same order: the texts the coders read (the
final message, the comments and the skip reasons the agent added: ``hoh.coding_export``), and
per test file the lines T' removed from T and the lines it added, each with its line number,
and no other line (design doc section 14.1, item 20). T is generated from the benchmark's tests,
whose files are not distributed; a file the agent deleted is marked deleted, without its lines.
So that no more of T goes out than that, a session without its original test, a file T' has and
T does not, and a file that lost more than half its lines refuse the export; none occurs in the
recorded sessions.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from hoh import analysis
from hoh.analysis import AnalysisRefused, Scenarios, Session, Stage
from hoh.coding_export import _sources, added_comments, added_skip_reasons, final_message

OUTCOMES: dict[str, str] = {
    "false_heal_bug_encoded": "false heal",
    "false_heal_blind": "false heal",
    "false_heal_other": "false heal",
    "false_heal_workaround": "false heal",
    "false_heal": "false heal",
    "kept_red": "kept red",
    "broken": "broken",
    "silenced": "skipped",
    "bug_preserving_heal": "correct heal",
    "correct_partial_heal": "correct heal",
    "heal_with_oracle_edits": "relaxed",
    "not_healed": "not repaired",
}
"""The labels the paper's Tables II and III name, by the name they give the outcome (Mixed
scenarios follow Table III, so their correct partial heal is a correct heal)."""

FROZEN_PROVENANCE = "3cba7b203328184216d9c9d36ac330779e090a2c8a555f090d359ac04a639b85"
"""The SHA-256 of the frozen analysis's ``provenance.json`` (tag ``data-freeze-2026-11-17``)."""

COLUMNS = ("session", "stage", "analysis", "app", "test", "family", "arm", "rep", "split",
           "outcome", "label", "edit", "run_test_calls", "turns", "cost_usd", "defect_final",
           "defect_comments", "defect_skip_reasons", "could_not_tell")
"""``analysis`` is ``main`` for the reviewed stages, whose sessions give the paper's main
results, and ``exploratory`` for the exploratory stage."""


def outcome_of(label: str) -> str:
    """The paper's name for a label's outcome; a label it does not name refuses the export."""
    if label not in OUTCOMES:
        raise AnalysisRefused(f"the label {label} has no outcome named in the paper")
    return OUTCOMES[label]


def changed_lines(original: str, returned: str) -> dict[str, list[list[Any]]]:
    """The lines of ``original`` that ``returned`` removed and the lines it added, each as
    [line number, text], numbered in its own file; no line kept in place (a moved line is both
    removed and added)."""
    old, new = original.splitlines(), returned.splitlines()
    removed: list[list[Any]] = []
    added: list[list[Any]] = []
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend([i + 1, old[i]] for i in range(i1, i2))
        if tag in ("replace", "insert"):
            added.extend([j + 1, new[j]] for j in range(j1, j2))
    return {"removed": removed, "added": added}


def changed_files(original: Mapping[str, str],
                  returned: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Per test file, its changed lines (``changed_lines``); a file of T that T' does not have
    is marked deleted, without its lines. A file of T' that T does not have, and a file that
    lost more than half its lines, refuse the export: either would give out most of T."""
    files: dict[str, dict[str, Any]] = {}
    for name in sorted(set(original) | set(returned)):
        if name not in original:
            raise AnalysisRefused(f"{name}: a file the original test does not have")
        if name not in returned:
            files[name] = {"deleted": True}
            continue
        files[name] = changed_lines(original[name], returned[name])
        if 2 * len(files[name]["removed"]) > len(original[name].splitlines()):
            raise AnalysisRefused(f"{name}: more than half its lines removed")
    return files


def _observed(stage: Stage) -> list[Session]:
    for s in stage.sessions:
        if not s.observation:
            raise AnalysisRefused(f"{s.job_id}: not an observation ({s.reason})")
    return sorted(stage.sessions, key=lambda s: s.job_id)


def session_rows(stage: Stage, codes: Mapping[str, Mapping[str, str]],
                 scenarios: Scenarios) -> list[dict[str, Any]]:
    """The rows of ``sessions.csv`` for one stage, by job id."""
    rows = []
    for s in _observed(stage):
        record = s.record or {}
        session = record.get("session") or {}
        cost = session.get("cost") or {}
        code = codes.get(s.job_id) or {}
        rows.append({
            "session": s.job_id, "stage": s.stage,
            "analysis": "main" if s.stage in analysis.REVIEWED else "exploratory",
            "app": s.app, "test": s.task,
            "family": s.family, "arm": s.arm, "rep": s.rep,
            "split": "dev" if scenarios.dev(s.app, s.task) else "held-out",
            "outcome": outcome_of(s.label), "label": s.label,
            "edit": (record.get("mechanism") or {}).get("kind") or "",
            "run_test_calls": session.get("run_test_calls"),
            "turns": cost.get("num_turns"),
            "cost_usd": ("" if cost.get("total_cost_usd") is None
                         else round(float(cost["total_cost_usd"]), 6)),
            "defect_final": code.get("final") or "",
            "defect_comments": code.get("comments") or "",
            "defect_skip_reasons": code.get("skip_reasons") or "",
            "could_not_tell": code.get("fallback") or "",
        })
    return rows


def session_texts(stage: Stage) -> list[dict[str, Any]]:
    """The lines of ``sessions.jsonl`` for one stage, by job id."""
    texts = []
    for s in _observed(stage):
        attempt = s.attempt_dir
        original = _sources(attempt / "session" / "original_tests")
        returned = _sources(attempt / "returned" / "tests")
        if not original:
            raise AnalysisRefused(f"{s.job_id}: no original test")
        try:
            files = changed_files(original, returned)
        except AnalysisRefused as exc:
            raise AnalysisRefused(f"{s.job_id}: {exc}") from exc
        texts.append({
            "session": s.job_id,
            "final": final_message(attempt / "out" / "stream.jsonl"),
            "comments": added_comments(original, returned),
            "skip_reasons": added_skip_reasons(original, returned)[0],
            "files": files,
        })
    return texts


def codes_of(stage: Stage, coding: Path) -> dict[str, dict[str, str]]:
    """The adjudicated codes of a reviewed stage by job id; none for another stage."""
    if stage.name not in analysis.REVIEWED:
        return {}
    folder = Path(coding) / stage.name
    gate = analysis.defect_codes(folder)
    if gate["state"] != "ready":
        raise AnalysisRefused(f"stage {stage.name}: its codes are not ready ({gate['why']})")
    return analysis.load_codes(folder, stage)


def read_provenance(path: Path, frozen: str = FROZEN_PROVENANCE) -> dict[str, Any]:
    """The frozen analysis's provenance file; another file refuses the export."""
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != frozen:
        raise AnalysisRefused(f"{path} is not the frozen analysis's provenance file")
    return json.loads(data)


def check_inputs(stages: Iterable[Stage], recorded: Mapping[str, Any], repo: Path) -> None:
    """The coding files of each reviewed stage and the selection are the ones the frozen
    analysis read."""
    for stage in stages:
        if stage.name not in analysis.REVIEWED:
            continue
        want = (recorded.get("coding") or {}).get(stage.name) or {}
        folder = Path(repo) / "docs" / "coding" / stage.name
        for name in ("adjudication.json", "adjudicated.csv", "key.json", "agreement.json"):
            if want.get(name) is None or analysis._sha256_of(folder / name) != want[name]:
                raise AnalysisRefused(f"stage {stage.name}: {name} is not the file the frozen "
                                      "analysis read")
    path = "scenarios/selection.json"
    want = ((recorded.get("inputs") or {}).get(path) or {}).get("object")
    if analysis.git_object(Path(repo) / path) != want:
        raise AnalysisRefused(f"{path} is not the file the frozen analysis read")


def check_provenance(stages: Iterable[Stage], recorded: Mapping[str, Any]) -> None:
    """Each stage's job records are the ones the frozen analysis read."""
    for stage in stages:
        want = ((recorded.get("stages") or {}).get(stage.name) or {}).get("records_sha256")
        if want != analysis._records_sha256(stage):
            raise AnalysisRefused(f"stage {stage.name}: its job records are not the ones the "
                                  "provenance file records")


def write(out: Path, stages: Sequence[Stage], codes: Mapping[str, Mapping[str, Any]],
          scenarios: Scenarios, *, results: Path | None = None) -> None:
    """Write ``sessions.csv`` and ``sessions.jsonl`` under ``out``."""
    out = Path(out).resolve()
    if results is not None:
        results = Path(results).resolve()
        if out == results or results in out.parents:
            raise AnalysisRefused(f"the output {out} is under the results root {results}")
    rows = [row for stage in stages for row in session_rows(stage, codes[stage.name], scenarios)]
    texts = [text for stage in stages for text in session_texts(stage)]
    out.mkdir(parents=True, exist_ok=True)
    with (out / "sessions.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    (out / "sessions.jsonl").write_text(
        "".join(json.dumps(text, ensure_ascii=False) + "\n" for text in texts),
        encoding="utf-8")


def main(argv: Sequence[str] | None = None, *, repo: Path = analysis.REPO,
         frozen: str = FROZEN_PROVENANCE) -> int:
    parser = argparse.ArgumentParser(prog="python -m hoh.artifact", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--stages", nargs="+", required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if len(set(args.stages)) != len(args.stages):
            raise AnalysisRefused(f"a stage named twice: {' '.join(args.stages)}")
        recorded = read_provenance(args.provenance, frozen)
        stages = [analysis.load_stage(args.results, name) for name in args.stages]
        check_provenance(stages, recorded)
        check_inputs(stages, recorded, repo)
        scenarios = analysis.load_scenarios(
            repo / "scenarios" / "selection.json",
            {name: repo / "scenarios" / name / "rules.yaml" for name in ("rename", "assert")})
        codes = {stage.name: codes_of(stage, repo / "docs" / "coding") for stage in stages}
        write(args.out, stages, codes, scenarios, results=args.results)
    except AnalysisRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.out / 'sessions.csv'} and {args.out / 'sessions.jsonl'}: "
          f"{sum(len(s.sessions) for s in stages)} sessions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
