"""Blind folders for coding "reported an application defect" and "fallback".

Analysis plan sections 1 and 9; the coders' instructions are
``docs/coding/defect-report-coder-prompt.md``.

    python -m hoh.coding_export export --results <stage dir> [--results ...] \
        --out <dir> --key <file> [--seed N]

``export`` writes one folder per judged session under ``--out``, named by an opaque id, holding
up to three files, all taken from the attempt that counts (``job.json``'s ``attempt_dir``):

- ``final.txt``: the ``result`` text of the last line of ``out/stream.jsonl``. Not written when
  that line is not a result event or carries no text; such sessions are listed in the summary.
- ``comments.txt``: the ``#`` comments of the Python files under ``returned/tests/`` that are
  not in ``session/original_tests/`` (a multiset difference over the whole directory, so a
  comment the agent left alone is not added), one per line.
- ``skip-reasons.txt``: the reason of every skip, skipif or xfail mark and every
  ``pytest.skip``/``pytest.xfail`` call that is in the returned files and not in the original
  ones, one per line. A mark or call without a reason has no text; the summary counts them.

A missing file means there is no such text. ``--out`` also gets ``codes-template.csv``. Nothing
under ``--out`` names a job, an arm, a label or a test: the coders receive that directory.

A session is exported when its job is ``done`` and its counting record has a label. A record
with ``dry_run: true`` is never exported. Every job left out is counted with the reason, also a
job directory that has attempts and no ``job.json``; a job that never started has no directory,
so ``--expect N`` stops the export when the jobs found are not the N the stage has.

An id is a keyed hash of the job id under the seed: the same seed gives a job the same id in
every export, whatever other jobs are exported with it, and without the seed an id says nothing
of the job. A file that does not parse keeps the comment lines after the point where tokenizing
stops and the skip reasons of marks and calls written on one line; the summary lists the
session.

The key file maps each id to the job (job id, arm, family, app, task, rep, label, subtype) and
records the seed, the tool version and which files the session has. It is refused inside
``--out``.

    python -m hoh.coding_export agree --codes A.csv B.csv --key <file> --out <dir> \
        [--names A B] [--texts <dir>]

``agree`` takes the two coders' ``codes.csv``. Each must cover exactly the exported ids, hold
only allowed values, and have a code exactly where the session has the file (``fallback`` goes
with ``final.txt``). It reports, per column, n (the sessions that have the file), raw agreement
and Cohen's kappa, and writes ``agreement.json``, ``disagreements.csv`` (id, column, both codes,
both coders' notes of the row, the text) and ``adjudicated.csv``, which holds the agreed codes
and a blank where the coders disagree. The texts are read from the export directory the key
names, or from ``--texts``. ``--names A B`` records the two models in ``agreement.json``, from
which ``adjudication.json`` takes them.

    python -m hoh.coding_export adjudicated --codes A.csv B.csv \
        --adjudicated <dir>/adjudicated.csv --key <file> --out <dir>

``adjudicated`` checks the filled ``adjudicated.csv`` like a coder's file, refuses a code that
differs from the one both coders gave, and writes ``adjudication.json``: per column and in
total, ``decided`` (cells where the coders differ), ``changed_a`` and ``changed_b`` (decided
cells whose adjudicated code is not coder A's, not coder B's) and ``neither`` (not either's).
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import hmac
import io
import json
import re
import secrets
import subprocess
import sys
import tokenize
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

TEXT_FILES = {"final": "final.txt", "comments": "comments.txt", "skip_reasons": "skip-reasons.txt"}
CODE_COLUMNS = ("final", "comments", "skip_reasons", "fallback")
CSV_COLUMNS = ("session", *CODE_COLUMNS, "note")
TEMPLATE_NAME = "codes-template.csv"
KEY_SCHEMA = 1
TOOL_VERSION = 2  # raised whenever an export of the same results and seed would differ
DRY_RUN = "dry run"

_MARKS = {"skip", "skipif", "xfail"}
# constructs whose first positional argument is the reason (for skipif and the xfail mark it
# is the condition)
_POSITIONAL_REASON = {"mark.skip", "call.skip", "call.xfail"}


# ---- the three texts ------------------------------------------------------------------------


def final_message(stream: Path) -> str | None:
    """The ``result`` text of the last line of a stream-json file, or None when that line is
    not a result event or has no text."""
    try:
        lines = Path(stream).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    lines = [line for line in lines if line.strip()]
    if not lines:
        return None
    try:
        event = json.loads(lines[-1])
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("type") != "result":
        return None
    text = event.get("result")
    return text if isinstance(text, str) and text.strip() else None


def _comments(source: str) -> list[str]:
    out: list[str] = []
    reached = 0  # the last line tokenized
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            reached = tok.end[0]
            if tok.type == tokenize.COMMENT:
                out.append(tok.string.rstrip())
    except (tokenize.TokenError, SyntaxError):
        # where tokenizing stops, the lines that hold only a comment are still read
        for line in source.splitlines()[reached:]:
            if line.lstrip().startswith("#"):
                out.append(line.strip())
    return out


def added_comments(original: Mapping[str, str], returned: Mapping[str, str]) -> list[str]:
    """Comments of the returned files that the original files do not have, in file and line
    order. Both arguments map a file name to its source."""
    had = Counter(c for name in sorted(original) for c in _comments(original[name]))
    added = []
    for name in sorted(returned):
        for comment in _comments(returned[name]):
            if had[comment] > 0:
                had[comment] -= 1
            else:
                added.append(comment)
    return added


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _construct(node: ast.AST) -> str | None:
    """``mark.skip``, ``mark.skipif``, ``mark.xfail``, ``call.skip`` or ``call.xfail`` for the
    callee of a skip or xfail mark or call, else None."""
    parts = _dotted(node).split(".")
    if parts[-1] == "Exception" and len(parts) > 1:  # raise pytest.skip.Exception("...")
        parts = parts[:-1]
    last = parts[-1]
    if last not in _MARKS:
        return None
    before = parts[-2] if len(parts) > 1 else None
    if before == "mark" or last == "skipif":
        return f"mark.{last}"
    if before == "unittest":
        return "mark.skip"
    if before in {None, "pytest"}:
        return f"call.{last}"
    return None


def _reason_text(node: ast.expr, source: str) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ast.get_source_segment(source, node) or ast.unparse(node)


def _skip_reasons(source: str) -> list[tuple[str, str | None]]:
    """(construct, reason) for every skip or xfail mark or call of a file, in source order;
    the reason is None when none is given. A file that does not parse is read line by line,
    which finds the marks and calls written on one line."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        found_by_line: list[tuple[str, str | None]] = []
        for line in source.splitlines():
            statement = line.strip().removeprefix("@")
            try:
                ast.parse(statement)
            except (SyntaxError, ValueError):
                continue
            found_by_line.extend(_skip_reasons(statement))
        return found_by_line
    found: list[tuple[int, int, str, str | None]] = []
    callees: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callees.add(id(node.func))
        kind = _construct(node.func)
        if kind is None:
            continue
        reason = next((k.value for k in node.keywords if k.arg in {"reason", "msg"}), None)
        if reason is None and kind in _POSITIONAL_REASON and node.args:
            reason = node.args[0]
        text = _reason_text(reason, source) if reason is not None else None
        found.append((node.lineno, node.col_offset, kind, text))
    for node in ast.walk(tree):  # a bare mark: @pytest.mark.skip, pytestmark = pytest.mark.xfail
        if isinstance(node, ast.Attribute) and id(node) not in callees:
            kind = _construct(node)
            if kind is not None and kind.startswith("mark."):
                found.append((node.lineno, node.col_offset, kind, None))
    return [(kind, text) for _line, _col, kind, text in sorted(found, key=lambda f: f[:2])]


def added_skip_reasons(
    original: Mapping[str, str], returned: Mapping[str, str]
) -> tuple[list[str], int]:
    """The reasons of the skip and xfail marks and calls that the returned files have and the
    original files do not, and the number of such marks and calls that give no reason."""
    had = Counter(item for name in sorted(original) for item in _skip_reasons(original[name]))
    reasons: list[str] = []
    without = 0
    for name in sorted(returned):
        for item in _skip_reasons(returned[name]):
            if had[item] > 0:
                had[item] -= 1
            elif item[1] is None or not item[1].strip():
                without += 1
            else:
                reasons.append(item[1])
    return reasons, without


# ---- export ---------------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sources(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        return {}
    return {
        p.relative_to(directory).as_posix(): p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(directory.rglob("*.py")) if p.is_file()
    }


def _parses(source: str) -> bool:
    try:
        ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    return True


def _job_without_record(job_dir: Path, root: Path) -> str:
    for record_file in sorted(job_dir.glob("attempt-*/job_record.json")):
        record = _read_json(record_file)
        if isinstance(record, dict) and isinstance(record.get("job"), dict):
            if record["job"].get("job_id"):
                return str(record["job"]["job_id"])
    return (Path(root.name) / job_dir.relative_to(root)).as_posix()


def _judged_sessions(
    results: Sequence[Path],
) -> tuple[dict[str, tuple[Path, dict]], dict[str, list[str]]]:
    """{job id: (attempt directory, record)} of the sessions to code, and {reason: [job id]}
    of the jobs left out."""
    sessions: dict[str, tuple[Path, dict]] = {}
    skipped: dict[str, list[str]] = {}
    seen: set[str] = set()
    for root in results:
        root = Path(root)
        if not root.is_dir():
            raise ValueError(f"not a directory: {root}")
        started = {p.parent for p in root.rglob("attempt-*") if p.is_dir()}
        for job_dir in sorted(d for d in started if not (d / "job.json").exists()):
            skipped.setdefault("no job.json", []).append(_job_without_record(job_dir, root))
        for job_file in sorted(root.rglob("job.json")):
            job_dir = job_file.parent
            queued = _read_json(job_file)
            if not isinstance(queued, dict):
                skipped.setdefault("unreadable job.json", []).append(str(job_dir))
                continue
            job_state = queued.get("state")
            attempt_name = queued.get("attempt_dir")
            attempt = job_dir / attempt_name if isinstance(attempt_name, str) else None
            record = _read_json(attempt / "job_record.json") if attempt else None
            job_id = queued.get("job_id")
            if isinstance(record, dict) and isinstance(record.get("job"), dict):
                job_id = record["job"].get("job_id", job_id)
            job_id = str(job_id) if job_id else str(job_dir)
            if isinstance(record, dict) and record.get("dry_run") is not False:
                # a dry run repeats the job ids of its stage, so it is set aside first
                skipped.setdefault(DRY_RUN, []).append(job_id)
                continue
            if job_id in seen:
                raise ValueError(f"job {job_id} is given twice (the second time in {job_dir})")
            seen.add(job_id)
            if not isinstance(record, dict):
                reason = f"no attempt record (job state {job_state})"
            elif record.get("label") is None:
                reason = f"no label (record state {record.get('state')})"
            elif job_state != "done":
                reason = f"job not done (job state {job_state})"
            elif not isinstance(record.get("job"), dict):
                reason = "record names no job"
            else:
                sessions[job_id] = (attempt, record)
                continue
            skipped.setdefault(reason, []).append(job_id)
    return sessions, skipped


def session_id_of(seed: int, job_id: str) -> str:
    """The opaque id of a job under a seed: a keyed hash, so it depends on nothing else."""
    digest = hmac.new(str(seed).encode(), job_id.encode(), hashlib.sha256).hexdigest()
    return f"s-{digest[:10]}"


def _tool_commit() -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = done.stdout.strip()
    return commit if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", commit) else None


def _inside(path: Path, directory: Path) -> bool:
    path, directory = path.resolve(), directory.resolve()
    return path == directory or directory in path.parents


def export(
    results: Sequence[Path], out: Path, key: Path, *, seed: int | None = None,
    expect: int | None = None,
) -> dict[str, Any]:
    """Write the blind folders, the codes template and the key; return the summary."""
    out, key = Path(out), Path(key)
    if _inside(key, out):
        raise ValueError(f"the key {key} is inside {out}, which the coders receive")
    if key.exists():
        raise ValueError(f"the key {key} exists; a key is never overwritten")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError(f"{out} is not empty")
    sessions, skipped = _judged_sessions(results)
    found = len(sessions) + sum(len(jobs) for jobs in skipped.values())
    if expect is not None and found != expect:
        raise ValueError(f"found {found} jobs ({len(sessions)} to export), expected {expect}; "
                         f"nothing was written")
    if seed is None:
        seed = secrets.randbits(64)
    ids = {job_id: session_id_of(seed, job_id) for job_id in sorted(sessions)}
    if len(set(ids.values())) != len(ids):
        raise ValueError(f"seed {seed} gives two jobs the same id; use another seed")

    out.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict[str, Any]] = {}
    counts = dict.fromkeys(TEXT_FILES, 0)
    no_final: list[str] = []
    unparsed: list[str] = []
    no_reason: list[str] = []
    marks_without_reason = 0
    for job_id, session_id in sorted(ids.items(), key=lambda kv: kv[1]):
        attempt, record = sessions[job_id]
        original = _sources(attempt / "session" / "original_tests")
        returned = _sources(attempt / "returned" / "tests")
        reasons, without = added_skip_reasons(original, returned)
        marks_without_reason += without
        if without:
            no_reason.append(session_id)
        texts = {
            "final": final_message(attempt / "out" / "stream.jsonl"),
            "comments": "\n".join(added_comments(original, returned)) or None,
            "skip_reasons": "\n".join(reasons) or None,
        }
        folder = out / session_id
        folder.mkdir()
        for name, text in texts.items():
            if text is not None:
                body = text if text.endswith("\n") else text + "\n"
                (folder / TEXT_FILES[name]).write_text(body, encoding="utf-8")
                counts[name] += 1
        if texts["final"] is None:
            no_final.append(session_id)
        if not all(_parses(src) for src in returned.values()):
            unparsed.append(session_id)
        job = record["job"]
        entries[session_id] = {
            "job_id": job_id,
            **{field: job.get(field) for field in ("arm", "family", "app", "task", "rep")},
            "label": record.get("label"),
            "subtype": record.get("subtype"),
            "files": [name for name, text in texts.items() if text is not None],
        }

    with (out / TEMPLATE_NAME).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for session_id in sorted(entries):
            writer.writerow([session_id] + [""] * (len(CSV_COLUMNS) - 1))

    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text(json.dumps({
        "schema": KEY_SCHEMA,
        "seed": seed,
        "tool": {"version": TOOL_VERSION, "commit": _tool_commit()},
        "out": str(out.resolve()),
        "results": [str(Path(r).resolve()) for r in results],
        "sessions": entries,
    }, indent=2) + "\n", encoding="utf-8")
    return {
        "found": found,
        "exported": len(entries),
        "files": counts,
        "skipped": skipped,
        "no_final": no_final,
        "unparsed": unparsed,
        "no_reason": no_reason,
        "marks_without_reason": marks_without_reason,
        "seed": seed,
    }


def _print_export(summary: Mapping[str, Any]) -> None:
    def listed(title: str, session_ids: Sequence[str]) -> None:
        print(f"{title}: {len(session_ids)}")
        for session_id in session_ids:
            print(f"  {session_id}")

    skipped = summary["skipped"]
    n_skipped = sum(len(v) for v in skipped.values())
    print(f"jobs found: {summary['found']} = {summary['exported']} exported + "
          f"{n_skipped} skipped")
    print(f"sessions exported: {summary['exported']} (seed {summary['seed']})")
    for name, file_name in TEXT_FILES.items():
        print(f"  {file_name}: {summary['files'][name]}")
    print(f"jobs skipped: {n_skipped}")
    for reason, jobs in skipped.items():
        print(f"  {reason}: {len(jobs)}")
        for job in jobs:
            print(f"    {job}")
    listed("sessions with no final message", summary["no_final"])
    listed("sessions with a returned file that does not parse (read them by hand)",
           summary["unparsed"])
    listed(f"sessions with a skip or xfail added without a reason "
           f"({summary['marks_without_reason']} marks and calls)", summary["no_reason"])
    not_ready = sum(len(jobs) for reason, jobs in skipped.items() if reason != DRY_RUN)
    if not_ready:
        print(f"WARNING: {not_ready} job(s) are left out for another reason than a dry run; "
              f"do not hand the folders out")


# ---- agreement ------------------------------------------------------------------------------

ALLOWED = {
    "final": ("asserted", "hedged", "none"),
    "comments": ("asserted", "hedged", "none"),
    "skip_reasons": ("asserted", "hedged", "none"),
    "fallback": ("yes", "no"),
}
# the text file a column codes
COLUMN_FILE = {"final": "final", "comments": "comments", "skip_reasons": "skip_reasons",
               "fallback": "final"}


def cohen_kappa(a: Sequence[str], b: Sequence[str]) -> float | None:
    """Cohen's kappa of two coders' codes of the same items: (po - pe) / (1 - pe), with pe from
    each coder's own marginal shares. None when there is no item or pe is 1 (both coders used
    one and the same category throughout), where kappa is not defined."""
    if len(a) != len(b):
        raise ValueError(f"the coders coded {len(a)} and {len(b)} items")
    n = len(a)
    if n == 0:
        return None
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    share_a, share_b = Counter(a), Counter(b)
    expected = sum(share_a[c] * share_b[c] for c in share_a) / (n * n)
    if expected == 1:
        return None
    return (observed - expected) / (1 - expected)


def _read_codes(path: Path, sessions: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """One coder's codes as {session id: {column: code, "note": the row's note}}; every
    problem of the file at once."""
    problems: list[str] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = [h.strip() for h in reader.fieldnames or []]
        if not {"session", *CODE_COLUMNS} <= set(header):
            raise ValueError(f"{path}: the columns must be {','.join(CSV_COLUMNS)}; "
                             f"found {','.join(header) or 'none'}")
        reader.fieldnames = header
        rows = list(reader)
    codes: dict[str, dict[str, str]] = {}
    for row in rows:
        session_id = (row.get("session") or "").strip()
        if session_id in codes:
            problems.append(f"{session_id} is coded twice")
            continue
        if session_id not in sessions:
            problems.append(f"{session_id!r} was not exported")
            continue
        files = sessions[session_id]["files"]
        codes[session_id] = {"note": (row.get("note") or "").strip()}
        for column in CODE_COLUMNS:
            value = (row.get(column) or "").strip().lower()
            file_name = TEXT_FILES[COLUMN_FILE[column]]
            if COLUMN_FILE[column] not in files:
                if value:
                    problems.append(f"{session_id} has no {file_name} but {column} is {value!r}")
            elif not value:
                problems.append(f"{session_id} has {file_name} but {column} is empty")
            elif value not in ALLOWED[column]:
                problems.append(f"{session_id}: {column} is {value!r}, not one of "
                                f"{', '.join(ALLOWED[column])}")
            codes[session_id][column] = value
    missing = sorted(set(sessions) - set(codes) - {
        p.split(" ", 1)[0] for p in problems if p.endswith("coded twice")})
    if missing:
        problems.append(f"{len(missing)} exported sessions are missing: {', '.join(missing)}")
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    return codes


Codes = Mapping[str, Mapping[str, str]]


def column_agreement(a: Codes, b: Codes, sessions: Mapping[str, Mapping[str, Any]]
                     ) -> dict[str, dict[str, Any]]:
    """Per column, the two coders' agreement over the sessions that have the column's text file
    (``COLUMN_FILE``): ``n``, ``agreed``, ``raw_agreement`` (None when n is 0) and ``kappa``
    (``cohen_kappa``). ``a`` and ``b`` are the coders' codes as ``_read_codes`` gives them."""
    columns: dict[str, dict[str, Any]] = {}
    for column in CODE_COLUMNS:
        ids = [s for s in sorted(sessions) if COLUMN_FILE[column] in sessions[s]["files"]]
        left, right = [a[s][column] for s in ids], [b[s][column] for s in ids]
        agreed = sum(x == y for x, y in zip(left, right, strict=True))
        columns[column] = {
            "n": len(ids),
            "agreed": agreed,
            "raw_agreement": agreed / len(ids) if ids else None,
            "kappa": cohen_kappa(left, right),
        }
    return columns


def agree(
    codes_a: Path, codes_b: Path, key: Path, out: Path, *, texts: Path | None = None,
    names: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Check both coders' files against the key, write ``agreement.json``,
    ``disagreements.csv`` and ``adjudicated.csv`` under ``out``, and return the report."""
    out = Path(out)
    key_data = json.loads(Path(key).read_text(encoding="utf-8"))
    sessions = key_data["sessions"]
    if (Path(codes_a).resolve() == Path(codes_b).resolve()
            or Path(codes_a).read_bytes() == Path(codes_b).read_bytes()):
        raise ValueError("both --codes files are the same; give one file per coder")
    a = _read_codes(codes_a, sessions)
    b = _read_codes(codes_b, sessions)
    if (out / "adjudicated.csv").exists():
        raise ValueError(f"{out / 'adjudicated.csv'} exists; an adjudication is never overwritten")
    text_dir = Path(texts) if texts is not None else Path(key_data["out"])

    columns = column_agreement(a, b, sessions)
    disagreements: list[list[str]] = []
    for column in CODE_COLUMNS:
        for session_id in sorted(sessions):
            if COLUMN_FILE[column] not in sessions[session_id]["files"]:
                continue
            x, y = a[session_id][column], b[session_id][column]
            if x == y:
                continue
            text_file = text_dir / session_id / TEXT_FILES[COLUMN_FILE[column]]
            try:
                text = text_file.read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"cannot read {text_file}; name the exported folders "
                                 f"with --texts") from exc
            disagreements.append([session_id, column, x, y, a[session_id]["note"],
                                  b[session_id]["note"], text])
    disagreements.sort(key=lambda row: (row[0], CODE_COLUMNS.index(row[1])))

    report = {
        "key": str(Path(key).resolve()),
        "codes_a": str(Path(codes_a).resolve()),
        "codes_b": str(Path(codes_b).resolve()),
        "names": {"a": names[0], "b": names[1]} if names else None,
        "sessions": len(sessions),
        "columns": columns,
        "disagreements": len(disagreements),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "agreement.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    with (out / "disagreements.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["session", "column", "code_a", "code_b", "note_a", "note_b", "text"])
        writer.writerows(disagreements)
    with (out / "adjudicated.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for session_id in sorted(sessions):
            agreed_codes = [a[session_id][c] if a[session_id][c] == b[session_id][c] else ""
                            for c in CODE_COLUMNS]
            writer.writerow([session_id, *agreed_codes, ""])
    return report


def _print_agree(report: Mapping[str, Any]) -> None:
    def number(value: float | None) -> str:
        return "-" if value is None else f"{value:.3f}"

    if report["names"]:
        print(f"A: {report['names']['a']}; B: {report['names']['b']}")
    print(f"sessions: {report['sessions']}")
    print(f"{'column':<14}{'n':>5}{'agreement':>11}{'kappa':>8}")
    for column, row in report["columns"].items():
        print(f"{column:<14}{row['n']:>5}{number(row['raw_agreement']):>11}"
              f"{number(row['kappa']):>8}")
    print(f"disagreements: {report['disagreements']}")


# ---- adjudication ---------------------------------------------------------------------------

COUNTS = ("decided", "changed_a", "changed_b", "neither")


def adjudication_counts(a: Codes, b: Codes, final: Codes,
                        sessions: Mapping[str, Mapping[str, Any]]
                        ) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Per column, the cells the adjudication decided and the codes it changed (``COUNTS``; see
    the coding README), and the problems: each cell where both coders gave one code and
    ``final`` another. ``a``, ``b`` and ``final`` are codes as ``_read_codes`` gives them."""
    columns = {column: dict.fromkeys(COUNTS, 0) for column in CODE_COLUMNS}
    problems: list[str] = []
    for session_id in sorted(sessions):
        for column in CODE_COLUMNS:
            x, y, j = a[session_id][column], b[session_id][column], final[session_id][column]
            if x == y:
                if j != x:
                    problems.append(f"{session_id}: {column} is {j!r} but both coders gave {x!r}")
                continue
            counts = columns[column]
            counts["decided"] += 1
            counts["changed_a"] += j != x
            counts["changed_b"] += j != y
            counts["neither"] += j != x and j != y
    return columns, problems


def adjudication(
    codes_a: Path, codes_b: Path, adjudicated: Path, key: Path, out: Path
) -> dict[str, Any]:
    """Check the filled ``adjudicated.csv`` against both coders' files and the key, write
    ``adjudication.json`` under ``out`` and return the report."""
    sessions = json.loads(Path(key).read_text(encoding="utf-8"))["sessions"]
    a = _read_codes(codes_a, sessions)
    b = _read_codes(codes_b, sessions)
    final = _read_codes(adjudicated, sessions)
    columns, problems = adjudication_counts(a, b, final, sessions)
    if problems:
        raise ValueError(f"{adjudicated}: " + "; ".join(problems))
    files = {"codes_a": codes_a, "codes_b": codes_b, "adjudicated": adjudicated}
    out = Path(out)
    agreement = _read_json(out / "agreement.json")
    report = {
        "key": str(Path(key).resolve()),
        "names": agreement.get("names") if isinstance(agreement, dict) else None,
        **{name: str(Path(path).resolve()) for name, path in files.items()},
        "sha256": {name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                   for name, path in files.items()},
        "sessions": len(sessions),
        "columns": columns,
        "total": {name: sum(c[name] for c in columns.values()) for name in COUNTS},
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "adjudication.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def _print_adjudication(report: Mapping[str, Any]) -> None:
    if report["names"]:
        print(f"A: {report['names']['a']}; B: {report['names']['b']}")
    print(f"sessions: {report['sessions']}")
    print(f"{'column':<14}" + "".join(f"{name:>11}" for name in COUNTS))
    for column, row in [*report["columns"].items(), ("total", report["total"])]:
        print(f"{column:<14}" + "".join(f"{row[name]:>11}" for name in COUNTS))


# ---- command line ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hoh.coding_export", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="write the blind folders and the key")
    exp.add_argument("--results", action="append", required=True, type=Path,
                     help="a stage directory of results; may be given several times")
    exp.add_argument("--out", required=True, type=Path, help="the directory the coders receive")
    exp.add_argument("--key", required=True, type=Path, help="the key file, outside --out")
    exp.add_argument("--seed", type=int, help="seed of the ids; drawn and recorded when absent")
    exp.add_argument("--expect", type=int, metavar="N",
                     help="the number of jobs the stage has; stop when another number is found")
    agr = sub.add_parser("agree", help="measure the two coders' agreement")
    agr.add_argument("--codes", nargs=2, required=True, type=Path, metavar=("A.csv", "B.csv"))
    agr.add_argument("--key", required=True, type=Path, help="the key file export wrote")
    agr.add_argument("--out", required=True, type=Path)
    agr.add_argument("--names", nargs=2, metavar=("A", "B"),
                     help="the two coders' models with their versions, in the order of --codes")
    agr.add_argument("--texts", type=Path,
                     help="the exported folders, when not where the key says they are")
    adj = sub.add_parser("adjudicated", help="check the adjudication and count what it changed")
    adj.add_argument("--codes", nargs=2, required=True, type=Path, metavar=("A.csv", "B.csv"))
    adj.add_argument("--adjudicated", required=True, type=Path, help="the filled adjudicated.csv")
    adj.add_argument("--key", required=True, type=Path, help="the key file export wrote")
    adj.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            _print_export(export(args.results, args.out, args.key, seed=args.seed,
                                 expect=args.expect))
        elif args.command == "adjudicated":
            _print_adjudication(adjudication(*args.codes, args.adjudicated, args.key, args.out))
        else:
            _print_agree(agree(*args.codes, args.key, args.out, texts=args.texts,
                               names=args.names))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
