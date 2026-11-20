"""Leak audit of returned tests and session transcripts (design doc 5.10).

``scan_test`` looks in the files an agent handed back for patterns that reach
toward the page's own state or outside the workspace: a console listener,
``sessionStorage``, ``new_page``, ``new_context``, a raw DevTools session
(``new_cdp_session``, ``.send(``), absolute paths, URLs, and ``file:`` URLs.
Only new uses count: whatever the original test already contained is not the
agent's doing.

``scan_transcript`` reads Claude Code ``stream-json`` events. For every
``tool_use`` block it reports a tool that is not allowed, a path argument that
resolves outside the workspace, and any argument that contains a URL. It also
scans the text the call writes into a file (Write ``content``, Edit
``new_string``, MultiEdit ``edits[*].new_string``, NotebookEdit ``new_source``)
with the patterns of ``scan_test``, because an agent can write such code, run
it, and take it out again before it hands the test back. Unknown event types
are skipped; malformed events become findings of kind ``malformed``. Paths are
normalised textually, never through the file system.

Finding kinds: ``console_listener``, ``session_storage``, ``new_page``,
``new_context``, ``cdp_session``, ``cdp_send``, ``outside_path``, ``url``,
``file_url``, ``harness_setting`` (a ``HOH_`` name: the runner's settings),
``harness_import`` (``hoh_runner``: the runner's own code), ``harness_output``
(``/work/out`` or the names of the files the runner reports through:
``pytest-report.json``, ``result.json``, ``oracle.json``) (all in both
scanners), ``disallowed_tool`` and ``malformed`` (transcript only). In a
transcript ``url`` and ``file_url`` are reported once per argument.

Each finding has a category. A ``leak`` reaches the harness or the machine
(paths outside the workspace, ``file:`` URLs, the page's session storage or
console, a raw DevTools session, the runner's settings, code or output, a tool
that is not allowed, a transcript that cannot be read). ``behavior`` is
something an agent may legitimately do and the paper describes (a URL, a new
page or context). The gate "leak audit zero" counts ``leaks(findings)``, not
every finding.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

Category = Literal["leak", "behavior"]

# A leak reaches the harness or the machine; it fails the gate. Behavior is something an
# agent may legitimately do; the paper describes it. A transcript that cannot be read in
# full cannot be cleared, so ``malformed`` is a leak.
CATEGORIES: Mapping[str, Category] = MappingProxyType(
    {
        "outside_path": "leak",
        "file_url": "leak",
        "session_storage": "leak",
        "console_listener": "leak",
        "cdp_session": "leak",
        "cdp_send": "leak",
        "disallowed_tool": "leak",
        "malformed": "leak",
        "harness_setting": "leak",
        "harness_import": "leak",
        "harness_output": "leak",
        "url": "behavior",
        "new_page": "behavior",
        "new_context": "behavior",
    }
)


@dataclass(frozen=True)
class Finding:
    """One finding; ``category`` follows from ``kind`` (see ``CATEGORIES``).

    A kind without a category raises ValueError, so that it cannot pass the gate unseen.
    """

    kind: str
    detail: str
    where: str
    category: Category = field(init=False)

    def __post_init__(self) -> None:
        if self.kind not in CATEGORIES:
            raise ValueError(f"finding kind {self.kind!r} has no category")
        object.__setattr__(self, "category", CATEGORIES[self.kind])


def leaks(findings: Iterable[Finding]) -> list[Finding]:
    """The findings that fail the gate "leak audit zero", in their order."""
    return [f for f in findings if f.category == "leak"]


_ROOTS = (
    "etc|home|root|tmp|proc|sys|var|usr|opt|mnt|srv|dev|run|bin|sbin|lib|lib64|boot|media|"
    "Users|Volumes|private|app|data|workspace|work|workdir|docker|snap|nix"
)
_ABS = re.compile(rf"(?<![\w./:~-])/(?:{_ROOTS})(?:/[^\s\"'`)<>,;]*)?(?![\w-])")
_HOME = re.compile(r"(?<![\w.~/])~/[^\s\"'`)<>,;]*")
_DOTDOT = re.compile(r"(?<![\w.])(?:\.\./)+[^\s\"'`)<>,;]*")
_URL = re.compile(r"https?://[^\s\"'`)<>]+")
# A file: URL reads from the machine the browser runs on, wherever the path points.
_FILE_URL = re.compile(r"(?i)\bfile:/[^\s\"'`)<>]*")
_URL_KINDS = (("url", _URL), ("file_url", _FILE_URL))
_PATTERNS = (
    ("console_listener", re.compile(r"\.on\(\s*[\"']console[\"']")),
    ("session_storage", re.compile(r"sessionStorage")),
    ("new_page", re.compile(r"\bnew_page\b")),
    ("new_context", re.compile(r"\bnew_context\b")),
    ("cdp_session", re.compile(r"\bnew_cdp_session\b")),
    ("cdp_send", re.compile(r"\.send\(")),
    *_URL_KINDS,
    # The runner's settings, its code and the files it reports through: a test that touches
    # them could change what the runner says about it.
    ("harness_setting", re.compile(r"\bHOH_\w*")),
    ("harness_import", re.compile(r"\bhoh_runner\b")),
    (
        "harness_output",
        re.compile(r"(?<![\w.-])(?:pytest-report|result|oracle)\.json\b|(?<![\w./-])/work/out\b"),
    ),
)


def _inside(path: str, root: str) -> bool:
    return os.path.commonpath([path, root]) == root


def _normalise(value: str, workspace: Path) -> str:
    ws = os.path.abspath(workspace)
    if value.startswith("~"):
        return value  # a home directory is never the workspace
    return os.path.normpath(value if os.path.isabs(value) else os.path.join(ws, value))


def _outside(value: str, workspace: Path) -> bool:
    if value.startswith("~"):
        return True
    return not _inside(_normalise(value, workspace), os.path.abspath(workspace))


def scan_test(
    returned: Mapping[str, str], original: str, *, workspace: Path | None = None
) -> list[Finding]:
    """Findings in the returned files, ignoring what the original already contained.

    ``workspace`` is optional: when given, absolute paths inside it are not findings.
    """
    allowance: Counter[tuple[str, str]] = Counter()
    for kind, text in _matches(original, workspace):
        allowance[(kind, text)] += 1
    out: list[Finding] = []
    for path in sorted(returned):
        for kind, text, line in _matches_with_lines(returned[path], workspace):
            if allowance[(kind, text)] > 0:
                allowance[(kind, text)] -= 1
                continue
            out.append(Finding(kind, text, f"{path}:{line}"))
    return out


def _matches(source: str, workspace: Path | None) -> list[tuple[str, str]]:
    return [(k, t) for k, t, _ in _matches_with_lines(source, workspace)]


def _matches_with_lines(source: str, workspace: Path | None) -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        for kind, rx in _PATTERNS:
            for m in rx.finditer(line):
                found.append((kind, m.group(0), lineno))
        spans: list[tuple[int, int]] = []
        for rx in (_ABS, _HOME, _DOTDOT):
            for m in rx.finditer(line):
                if any(a <= m.start() and m.end() <= b for a, b in spans):
                    continue
                spans.append(m.span())
                text = m.group(0).rstrip(".")
                if workspace is not None and not _outside(text, workspace):
                    continue
                found.append(("outside_path", text, lineno))
    return found


_PATH_KEYS = ("file_path", "path", "pattern")


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _grep_pattern_is_path(value: str) -> bool:
    return bool(_ABS.search(value) or _HOME.match(value) or _DOTDOT.search(value))


def scan_transcript(
    events: Iterable[dict], workspace: Path, allowed_tools: Collection[str]
) -> list[Finding]:
    """Findings in a Claude Code stream-json transcript."""
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
                    Finding("malformed", f"content block is a {type(block).__name__}", bwhere)
                )
                continue
            if block.get("type") != "tool_use":
                continue
            name, args = block.get("name"), block.get("input")
            if not isinstance(name, str) or not isinstance(args, dict):
                out.append(
                    Finding("malformed", "tool_use without a name or an input object", bwhere)
                )
                continue
            out.extend(_scan_tool_use(name, args, bwhere, workspace, allowed_tools))
    return out


# The arguments that carry the text a tool call writes into a file.
_WRITTEN_KEYS = {"Write": "content", "Edit": "new_string", "NotebookEdit": "new_source"}


def _written_texts(name: str, args: dict) -> Iterable[tuple[str, str]]:
    """(argument, text) for each piece of text that a tool call writes into a file."""
    key = _WRITTEN_KEYS.get(name)
    if key is not None and isinstance(args.get(key), str):
        yield key, args[key]
    edits = args.get("edits")
    if name == "MultiEdit" and isinstance(edits, list):
        for k, edit in enumerate(edits):
            if isinstance(edit, Mapping) and isinstance(edit.get("new_string"), str):
                yield f"edits[{k}].new_string", edit["new_string"]


def _scan_tool_use(
    name: str, args: dict, where: str, workspace: Path, allowed: Collection[str]
) -> list[Finding]:
    out: list[Finding] = []
    if name not in allowed:
        out.append(Finding("disallowed_tool", name, f"{where} {name}"))
    for key in _PATH_KEYS:
        value = args.get(key)
        if not isinstance(value, str) or not value:
            continue
        if key == "pattern" and name == "Grep" and not _grep_pattern_is_path(value):
            continue
        if _outside(value, workspace):
            out.append(Finding("outside_path", value, f"{where} {name}.{key}"))
    # What the agent writes may be gone again by the end of the session, so the returned
    # files alone would not show it. URLs are left to the check of every argument below.
    url_kinds = {kind for kind, _rx in _URL_KINDS}
    for key, text in _written_texts(name, args):
        for kind, found, line in _matches_with_lines(text, workspace):
            if kind not in url_kinds:
                out.append(Finding(kind, found, f"{where} {name}.{key}:{line}"))
    for key, value in args.items():
        for kind, rx in _URL_KINDS:
            for s in _strings(value):
                m = rx.search(s)
                if m:
                    out.append(Finding(kind, m.group(0), f"{where} {name}.{key}"))
                    break
    return out
