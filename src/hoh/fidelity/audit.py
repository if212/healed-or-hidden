"""Leak audit of the fidelity check's sessions (design doc 5.10, applied to the check).

``hoh.audit`` reads Python tests. The check hands the healer a TypeScript port, so this module
looks for the same things under their TypeScript names: a console listener (``.on("console"``),
``sessionStorage``, ``newPage``, ``newContext``, a raw DevTools session (``newCDPSession``,
``.send(``), absolute paths, URLs and ``file:`` URLs; and, for the harness, its settings (``HOH_``
and the fixture's ``PWFIX_``), its code (``hoh_runner``, and ``pwfix`` beyond what the port
already imports) and the places it reports through (the control directory, ``/control`` in the
server's container, ``/work/out`` and the study's report files). The kinds, and so their
categories, are ``hoh.audit``'s: a finding of category ``leak`` is a leak.

``scan_transcript`` checks every tool call as ``hoh.audit.scan_transcript`` does: a tool the
healer's agent file does not list, a path argument outside the workspace, a URL in any argument.
It scans the text a call writes, and also the ``function`` of every ``browser_evaluate`` call,
the code the healer runs in the page, with these patterns, URLs left to the check of every
argument. Paths in evaluated code are the app's routes, not files, so they are not findings.
Written text is cleared line by line: a match on a line the port holds with that match is the
port's, once for each such line of the port, so a Write of the whole test, which imports
``pwfix`` as the port does, finds nothing; a match on any other line is the healer's, so a second
import of ``pwfix``, which the returned test would show as a new use, is found in the call that
wrote it even if it is gone by the end. A module inside the fixture (``pwfix/lib/control``) is a
name of its own, which no port holds. Evaluated code has no allowance: the page's code has no
reason to name the harness.

``audit_attempt`` audits one attempt as the study's worker audits a session: the transcript, the
returned ``tests/`` and the test at each run, each against the port the session was given; the
workspace is the session's working directory. The tests at each run are the runs' snapshots
(``hoh.fidelity.runs``: ``snapshots/NN``, taken at each reset), and every reset in ``out/end.json``
must name a snapshot of its own that is there. What cannot be read cannot be cleared: a missing
transcript, returned ``tests/``, end record or named snapshot, a reset that names no snapshot, one
that names a snapshot an earlier reset names or a name the watcher does not give, and a file that
is not UTF-8 text, is a ``malformed`` finding, so a leak, as a malformed transcript is in the
study; so is a launch record with no absolute working directory, and then nothing else of the
attempt is read, since no path could be judged. It also counts what it read. Findings never change
a label.

Usage: ``python -m hoh.fidelity.audit <output root>``: every session's kept attempt, what was
read, its findings and its leaks; read only. Exit 1 with a leak; otherwise 2 with no session or
with a session that has no kept attempt (nothing of it was audited); otherwise 0.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from hoh import audit as study_audit
from hoh import session as study
from hoh.audit import Finding
from hoh.fidelity import runs
from hoh.fidelity import session as fid

EVALUATE = f"{fid.MCP_PREFIX}browser_evaluate"

_URL_KIND_NAMES = frozenset(kind for kind, _ in study_audit._URL_KINDS)

PATTERNS = (
    ("console_listener", re.compile(r"\.on\(\s*[\"'`]console[\"'`]")),
    ("session_storage", re.compile(r"sessionStorage")),
    ("new_page", re.compile(r"\bnewPage\b")),
    ("new_context", re.compile(r"\bnewContext\b")),
    ("cdp_session", re.compile(r"\bnewCDPSession\b")),
    ("cdp_send", re.compile(r"\.send\(")),
    *study_audit._URL_KINDS,
    ("harness_setting", re.compile(r"\b(?:HOH|PWFIX)_\w*")),
    # The whole module name, so the port's import of the package clears that name alone and
    # never a module inside the fixture ("pwfix/lib/control").
    ("harness_import", re.compile(r"\bpwfix\b(?:/[\w./-]*)?|\bhoh_runner\b")),
    (
        "harness_output",
        re.compile(
            r"(?<![\w.-])(?:pytest-report|result|oracle)\.json\b"
            r"|(?<![\w./-])/work/out\b|(?<![\w./-])/control(?![\w-])"
        ),
    ),
)
"""(kind, pattern): ``hoh.audit``'s kinds under the TypeScript names of the same APIs."""


def _matches(
    source: str, workspace: Path | None, *, paths: bool = True, urls: bool = True
) -> list[tuple[str, str, int]]:
    """(kind, text, line) of every match in ``source``; ``hoh.audit``'s path rules when
    ``paths``, inside ``workspace`` not a finding."""
    found: list[tuple[str, str, int]] = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        for kind, rx in PATTERNS:
            if urls or kind not in _URL_KIND_NAMES:
                found += [(kind, m.group(0), lineno) for m in rx.finditer(line)]
        if not paths:
            continue
        spans: list[tuple[int, int]] = []
        for rx in (study_audit._ABS, study_audit._HOME, study_audit._DOTDOT):
            for m in rx.finditer(line):
                if any(a <= m.start() and m.end() <= b for a, b in spans):
                    continue
                spans.append(m.span())
                text = m.group(0).rstrip(".")
                if workspace is not None and not study_audit._outside(text, workspace):
                    continue
                found.append(("outside_path", text, lineno))
    return found


def _new(
    sources: Iterable[tuple[str, str]], original: str, workspace: Path | None, **rules: bool
) -> list[Finding]:
    """The matches in the ``(where, text)`` sources that ``original`` does not already hold,
    each at ``where:line``; one allowance for all the sources, as ``hoh.audit.scan_test``."""
    allowance = Counter((k, t) for k, t, _ in _matches(original, workspace, **rules))
    out: list[Finding] = []
    for where, source in sources:
        for kind, text, line in _matches(source, workspace, **rules):
            if allowance[(kind, text)] > 0:
                allowance[(kind, text)] -= 1
                continue
            out.append(Finding(kind, text, f"{where}:{line}"))
    return out


def _by_line(source: str, workspace: Path | None) -> list[tuple[str, str, int, str]]:
    """(kind, text, line, that line stripped) of every match in ``source``, URLs aside."""
    lines = source.splitlines()
    return [(kind, text, n, lines[n - 1].strip())
            for kind, text, n in _matches(source, workspace, urls=False)]


def _written(where: str, text: str, original: str, workspace: Path) -> list[Finding]:
    """The matches in a text a tool call writes, each at ``where:line``, cleared only on a line
    the port holds with that match, once for each such line (module docstring)."""
    allowance = Counter((kind, found, line) for kind, found, _, line
                        in _by_line(original, workspace))
    out: list[Finding] = []
    for kind, found, n, line in _by_line(text, workspace):
        if allowance[(kind, found, line)] > 0:
            allowance[(kind, found, line)] -= 1
            continue
        out.append(Finding(kind, found, f"{where}:{n}"))
    return out


def scan_test(
    returned: Mapping[str, str], original: str, *, workspace: Path | None = None
) -> list[Finding]:
    """Findings in the returned files, ignoring what the port already holds (``hoh.audit``'s
    ``scan_test``, with this module's patterns)."""
    return _new(((path, returned[path]) for path in sorted(returned)), original, workspace)


def scan_transcript(
    events: Iterable[Any], workspace: Path, allowed_tools: Collection[str], *, original: str = ""
) -> list[Finding]:
    """Findings in a Claude Code stream-json transcript (module docstring)."""
    out: list[Finding] = []
    for i, event in enumerate(events):
        where = f"event[{i}]"
        if not isinstance(event, dict):
            out.append(Finding("malformed", f"event is a {type(event).__name__}", where))
            continue
        etype = event.get("type")
        if not isinstance(etype, str):
            out.append(Finding("malformed", "event has no string type", where))
            continue
        if etype != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict) or "content" not in message:
            out.append(Finding("malformed", "assistant event without message content", where))
            continue
        content = message["content"]
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            out.append(Finding("malformed", "message content is not a list", where))
            continue
        for j, block in enumerate(content):
            bwhere = f"{where}.content[{j}]"
            if not isinstance(block, dict):
                out.append(
                    Finding("malformed", f"content block is a {type(block).__name__}", bwhere))
                continue
            if block.get("type") != "tool_use":
                continue
            name, args = block.get("name"), block.get("input")
            if not isinstance(name, str) or not isinstance(args, dict):
                out.append(
                    Finding("malformed", "tool_use without a name or an input object", bwhere))
                continue
            out += _scan_tool_use(name, args, bwhere, workspace, allowed_tools, original)
    return out


def _scan_tool_use(
    name: str, args: dict, where: str, workspace: Path, allowed: Collection[str], original: str
) -> list[Finding]:
    out: list[Finding] = []
    if name not in allowed:
        out.append(Finding("disallowed_tool", name, f"{where} {name}"))
    for key in study_audit._PATH_KEYS:
        value = args.get(key)
        if not isinstance(value, str) or not value:
            continue
        if key == "pattern" and name == "Grep" and not study_audit._grep_pattern_is_path(value):
            continue
        if study_audit._outside(value, workspace):
            out.append(Finding("outside_path", value, f"{where} {name}.{key}"))
    for key, text in study_audit._written_texts(name, args):
        out += _written(f"{where} {name}.{key}", text, original, workspace)
    if name == EVALUATE and isinstance(args.get("function"), str):
        out += _new([(f"{where} {name}.function", args["function"])], "", workspace,
                    paths=False, urls=False)
    for key, value in args.items():
        for kind, rx in study_audit._URL_KINDS:
            for s in study_audit._strings(value):
                m = rx.search(s)
                if m:
                    out.append(Finding(kind, m.group(0), f"{where} {name}.{key}"))
                    break
    return out


# --- an attempt


def _files(root: Path) -> tuple[dict[str, str], list[Finding]]:
    """Every file under ``root``, keyed ``tests/<path under root>``, and a ``malformed`` finding
    for each that cannot be read as UTF-8 text."""
    files: dict[str, str] = {}
    unread: list[Finding] = []
    for p in sorted(root.rglob("*")) if root.is_dir() else ():
        if not p.is_file():
            continue
        try:
            files[f"tests/{p.relative_to(root).as_posix()}"] = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            unread.append(Finding("malformed", f"cannot be read as text: {error}", str(p)))
    return files, unread


def _events(stream: Path) -> list[Any]:
    """The transcript's lines as events; a line that is not JSON stays a string (malformed)."""
    events: list[Any] = []
    for raw in stream.read_bytes().splitlines():
        if not raw.strip():
            continue
        try:
            events.append(json.loads(raw))
        except ValueError:
            events.append(raw.decode("utf-8", errors="replace"))
    return events


def _as_dict(source: str, finding: Finding) -> dict[str, str]:
    return {"source": source, "kind": finding.kind, "category": finding.category,
            "detail": finding.detail, "where": finding.where}


def _recorded_snapshots(end: Path) -> list[str | None] | None:
    """The snapshot each of the attempt's resets names in its end record, in their order, None
    for a reset that names none; None without a readable end record."""
    try:
        record = json.loads(end.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    resets = record.get("resets") if isinstance(record, dict) else None
    if not isinstance(resets, list):
        return None
    return [r["snapshot"] if isinstance(r, dict) and isinstance(r.get("snapshot"), str) else None
            for r in resets]


def audit_attempt(attempt: Path) -> dict[str, Any]:
    """One attempt's findings, by source, the leaks among them, and what was read: transcript
    events, returned files, snapshots scanned and snapshots the resets name (module docstring)."""
    launch_path = attempt / "out" / "launch.json"
    try:
        workspace: Path | None = Path(json.loads(launch_path.read_text(encoding="utf-8"))["cwd"])
    except (OSError, ValueError, TypeError, KeyError):
        workspace = None
    if workspace is None or not workspace.is_absolute():
        # Without the working directory no path can be judged: nothing of the attempt is cleared.
        bad = _as_dict("launch", Finding("malformed", "no launch record with an absolute cwd",
                                         str(launch_path)))
        return {"findings": [bad], "leaks": [bad],
                "read": {"events": 0, "returned": 0, "snapshots": 0, "resets": 0}}
    port, unread_port = _files(attempt / "session" / study.ORIGINAL_TESTS)
    original = "".join(port.values())
    stream = attempt / "out" / "stream.jsonl"
    events = _events(stream) if stream.is_file() else []
    if stream.is_file():
        transcript = scan_transcript(events, workspace, set(fid.vendored_agent()[0]),
                                     original=original)
    else:
        transcript = [Finding("malformed", "no transcript", str(stream))]
    returned_dir = attempt / "workspace" / "tests"
    returned, unread_returned = _files(returned_dir)
    groups: list[tuple[str, list[Finding]]] = [
        ("port", unread_port),
        ("transcript", transcript),
        ("returned", scan_test(returned, original, workspace=workspace) + unread_returned
         if returned_dir.is_dir()
         else [Finding("malformed", "no returned tests", str(returned_dir))]),
    ]
    end = attempt / "out" / "end.json"
    recorded = _recorded_snapshots(end)
    if recorded is None:
        groups.append(("resets", [Finding("malformed", "no end record with resets", str(end))]))
    root = attempt / runs.SNAPSHOTS
    taken = sorted((int(p.name), p) for p in (root.iterdir() if root.is_dir() else ())
                   if p.is_dir() and p.name.isdigit())
    named: set[str] = set()
    for number, name in enumerate(recorded or (), start=1):
        if name is None:
            groups.append((f"reset {number}", [Finding(
                "malformed", "a reset that names no snapshot", str(end))]))
        elif not re.fullmatch(r"\d{2,}", name):  # the watcher names them 01, 02 and on
            groups.append((f"reset {number}", [Finding(
                "malformed", "a reset that names a snapshot the watcher does not make",
                str(end))]))
        elif name in named:
            groups.append((f"reset {number}", [Finding(
                "malformed", "a reset that names the snapshot of an earlier reset", str(end))]))
        else:
            named.add(name)
            if not (root / name).is_dir():
                groups.append((f"snapshot {name}", [Finding(
                    "malformed", "a snapshot its reset names is missing", str(root / name))]))
    for _, run in taken:
        files, unread = _files(run)
        groups.append((f"snapshot {run.name}",
                       scan_test(files, original, workspace=workspace) + unread))
    findings = [_as_dict(source, f) for source, found in groups for f in found]
    return {"findings": findings, "leaks": [f for f in findings if f["category"] == "leak"],
            "read": {"events": len(events), "returned": len(returned),
                     "snapshots": len(taken), "resets": len(recorded or ())}}


# --- the command


def _kept(item: Path) -> Path | None:
    numbered = sorted((int(p.name.removeprefix("attempt-")), p) for p in item.glob("attempt-*")
                      if p.is_dir() and p.name.removeprefix("attempt-").isdigit())
    for _, path in reversed(numbered):
        try:
            record = json.loads((path / "attempt.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("kept"):
            return path
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hoh.fidelity.audit",
        description="Leak audit of the fidelity check's kept sessions (read only).")
    parser.add_argument("root", type=Path, help="the runs' output root (its sessions/)")
    args = parser.parse_args(argv)
    sessions_dir = args.root / "sessions"
    items = sorted(p for p in sessions_dir.iterdir() if p.is_dir()) if sessions_dir.is_dir() else []
    if not items:
        print(f"no sessions under {sessions_dir}", file=sys.stderr)
        return 2
    total: Counter[str] = Counter()
    audited = missing = 0
    for item in items:
        kept = _kept(item)
        if kept is None:
            print(f"{item.name}: no kept attempt")
            missing += 1
            continue
        result = audit_attempt(kept)
        audited += 1
        counts = {**result["read"], "findings": len(result["findings"]),
                  "leaks": len(result["leaks"])}
        total.update(counts)
        print(f"{item.name}/{kept.name}: {_summary(counts)}")
        for f in result["findings"]:
            print(f"  {f['category']}: {f['source']} {f['kind']} {f['detail']!r} at {f['where']}")
    print(f"{_n(audited, 'session')} audited, {missing} without a kept attempt: {_summary(total)}")
    if total["leaks"]:
        return 1
    return 2 if missing else 0


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _summary(c: Mapping[str, int]) -> str:
    return (f"{_n(c['events'], 'transcript event')}, {_n(c['returned'], 'returned file')}, "
            f"{_n(c['snapshots'], 'snapshot')} of {_n(c['resets'], 'reset')}; "
            f"{_n(c['findings'], 'finding')}, {_n(c['leaks'], 'leak')}")


if __name__ == "__main__":
    sys.exit(main())
