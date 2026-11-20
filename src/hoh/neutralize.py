"""Remove the hints that mark a benchmark bug as synthetic, without changing the bug.

A bug script is rewritten in a fixed order: comments are dropped, console calls are
replaced, the audited literal rules in scenarios/neutralize/<app>.yaml are applied, the
injector's global names are renamed, and the key passed to transition() is replaced
(design doc section 5.3). What the bug adds, removes or changes stays the same.

The rewritten scripts are build outputs and are never committed. The rules are.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from hoh.benchmark import REPO_ROOT, load_case

RULES_DIR = REPO_ROOT / "scenarios" / "neutralize"

# Names the injector leaves on `window` and in sessionStorage, where a test can read them.
# The first four are the plan's. The pinned injector also builds three more storage keys
# from the same prefix; they are renamed too, or they would stay readable at run time.
IDENT_MAP: dict[str, str] = {
    "__BUG_INJECTOR_TRIGGERED__": "__as_t",
    "__BUG_INJECTOR_API__": "__appState",
    "__BUG_INJECTOR__": "__as",
    "__flash_ui_active__": "__ui_ready",
    "__BUG_INJECTOR_TRIGGERED_URL__": "__as_u",
    "__BUG_INJECTOR_TRANSITION_PREV__": "__as_p",
    "__BUG_INJECTOR_TRANSITION_COUNT__": "__as_n",
}

_MARKERS = (
    "// BEGIN isConditionMet",
    "// END isConditionMet",
    "// BEGIN onConditionMet",
    "// END onConditionMet",
)
_MARKER = re.compile(r"//\s*(?:BEGIN|END)\s+(?:isConditionMet|onConditionMet)\s*")


class RuleMismatch(ValueError):
    """A rewrite rule does not fit the script, or a rule file is malformed.

    Raised instead of skipping: a rule that silently stops matching puts the hint back.
    """


@dataclass(frozen=True)
class Rule:
    find: str
    replace: str
    count: int = 1


# --- a small JavaScript scanner ----------------------------------------------
#
# Everything below works on segments: code, string, template text, regular expression
# literal, line comment, block comment. That is what lets a rewrite skip `//` inside a
# URL, a parenthesis inside a string, or the word console inside a message.


@dataclass(frozen=True)
class _Seg:
    kind: str  # "code", "str", "tpl", "regex", "line" or "block"
    start: int
    end: int


# A slash starts a regular expression after one of these, and divides otherwise.
_REGEX_AFTER_PUNCT = frozenset("(,=:[!&|?{};+-*%<>~^")
_REGEX_AFTER_WORD = frozenset(
    "return typeof instanceof in of new delete void throw case do else yield await".split()
)


def _segments(js: str) -> list[_Seg]:
    """Split a script into contiguous segments that cover it exactly."""
    segs: list[_Seg] = []
    n = len(js)
    i = 0
    code_start = 0
    interpolations: list[int] = []  # brace depth inside each open `${ ... }`
    prev = ""  # last token seen in code: a word, a punctuation mark, or "x" after a literal

    def close_code(end: int) -> None:
        if end > code_start:
            segs.append(_Seg("code", code_start, end))

    def template_text(start: int) -> tuple[int, bool]:
        """Scan template text; return where it stops and whether it stopped at `${`."""
        j = start
        while j < n:
            if js[j] == "\\":
                j += 2
            elif js[j] == "`":
                return j + 1, False
            elif js.startswith("${", j):
                return j + 2, True
            else:
                j += 1
        return n, False

    def regex_end(start: int) -> int:
        """End of the regular expression literal that starts here, or 0 if it is not one."""
        j = start + 1
        in_class = False
        while j < n and js[j] != "\n":
            ch = js[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "[":
                in_class = True
            elif ch == "]":
                in_class = False
            elif ch == "/" and not in_class:
                j += 1
                while j < n and js[j].isalpha():
                    j += 1
                return j
            j += 1
        return 0

    while i < n:
        c = js[i]
        end = 0
        kind = ""
        if js.startswith("//", i):
            end = js.find("\n", i)
            end = n if end < 0 else end
            kind = "line"
        elif js.startswith("/*", i):
            end = js.find("*/", i + 2)
            end = n if end < 0 else end + 2
            kind = "block"
        elif c in "'\"":
            j = i + 1
            while j < n and js[j] != c and js[j] != "\n":
                j += 2 if js[j] == "\\" else 1
            end = min(j + 1, n) if j < n and js[j] == c else min(j, n)
            kind = "str"
        elif c == "`" or (c == "}" and interpolations and interpolations[-1] == 0):
            if c == "}":
                interpolations.pop()
            end, opened = template_text(i + 1)
            if opened:
                interpolations.append(0)
            kind = "tpl"
        elif c == "/" and (not prev or prev in _REGEX_AFTER_PUNCT or prev in _REGEX_AFTER_WORD):
            end = regex_end(i)
            kind = "regex"

        if end:
            close_code(i)
            segs.append(_Seg(kind, i, end))
            i = code_start = end
            if kind == "tpl" and js.startswith("${", end - 2):
                prev = "("  # an expression starts here
            elif kind not in ("line", "block"):
                prev = "x"
            continue

        if c == "{" and interpolations:
            interpolations[-1] += 1
        elif c == "}" and interpolations:
            interpolations[-1] -= 1
        if c.isalnum() or c in "_$":
            j = i
            while j < n and (js[j].isalnum() or js[j] in "_$"):
                j += 1
            prev = js[i:j]
            i = j
        else:
            if not c.isspace():
                prev = c
            i += 1
    close_code(n)
    return segs


def _blank(js: str, segs: list[_Seg], kinds: tuple[str, ...], fill: str) -> str:
    """The script with some kinds of segment overwritten, offsets and line breaks kept."""
    parts = []
    for seg in segs:
        text = js[seg.start : seg.end]
        parts.append(re.sub(r"[^\n]", fill, text) if seg.kind in kinds else text)
    return "".join(parts)


def _code_view(js: str, segs: list[_Seg]) -> str:
    """Code only: comments read as spaces, literals as NUL characters."""
    return _blank(_blank(js, segs, ("line", "block"), " "), segs, ("str", "tpl", "regex"), "\0")


# --- comments ----------------------------------------------------------------


def strip_comments(js: str) -> str:
    """Drop every comment except the four marker lines.

    The comments describe the bug. The markers stay because the benchmark's loader cuts
    the two functions out of the script by looking for them; they never reach the page.
    """
    out = ""
    pos = 0
    for seg in _segments(js):
        text = js[seg.start : seg.end]
        if seg.kind not in ("line", "block") or _MARKER.fullmatch(text):
            continue
        out += js[pos : seg.start]
        line_start = out.rfind("\n") + 1
        kept = out[line_start:]  # what stays on this line, before the comment
        line_end = js.find("\n", seg.end)
        line_end = len(js) if line_end < 0 else line_end
        rest = js[seg.end : line_end]
        if rest.strip():  # code follows on the same line
            pos = seg.end + len(rest) - len(rest.lstrip(" \t"))
            if "\n" in text:
                out += "\n"  # a comment with a line break in it ends a statement
            elif kept.strip() and not kept.endswith((" ", "\t")):
                out += " "
        elif kept.strip():  # a comment after code: the code and the line break stay
            out = out.rstrip(" \t")
            pos = line_end
        else:  # a comment alone on its line or lines: the whole line goes
            out = out[:line_start]
            pos = min(line_end + 1, len(js))
    return out + js[pos:]


# --- console -----------------------------------------------------------------

_CONSOLE_USE = re.compile(
    r"(?<![\w$.])(?:(?:window|globalThis|self)\s*\.\s*)?console\s*\.\s*[A-Za-z_$][\w$]*"
)


def _console_uses(js: str) -> list[tuple[int, int, bool]]:
    """Every use of console.<name> in code, as (start, end, is a call)."""
    view = _code_view(js, _segments(js))
    uses: list[tuple[int, int, bool]] = []
    done = 0
    for m in _CONSOLE_USE.finditer(view):
        if m.start() < done:
            continue  # inside the arguments of a call that is already being replaced
        j = m.end()
        while j < len(view) and view[j].isspace():
            j += 1
        if j < len(view) and view[j] == "(":
            depth = 0
            k = j
            while k < len(view):
                depth += (view[k] == "(") - (view[k] == ")")
                if depth == 0:
                    break
                k += 1
            else:
                raise ValueError(f"unbalanced parentheses after {js[m.start() : m.end()]}")
            uses.append((m.start(), k + 1, True))
        else:
            uses.append((m.start(), m.end(), False))
        done = uses[-1][1]
    return uses


def strip_console(js: str) -> str:
    """Replace each console call with `void 0` and each bare reference with a no-op.

    A statement is never deleted, so a brace-less `else`, a ternary or a callback
    argument stays valid. `void 0` is left without parentheses on purpose: at the start
    of a line a parenthesis would turn the line above into a function call.
    """
    out = []
    pos = 0
    for start, end, is_call in _console_uses(js):
        out.append(js[pos:start])
        out.append("void 0" if is_call else "(() => {})")
        pos = end
    out.append(js[pos:])
    return "".join(out)


# --- literal rules -----------------------------------------------------------


def apply_literal_rules(js: str, rules: list[Rule]) -> str:
    """Apply the rules in order. Each must match exactly `count` times."""
    for rule in rules:
        if not rule.find:
            raise RuleMismatch("a rule has an empty `find`")
        found = js.count(rule.find)
        if found != rule.count:
            raise RuleMismatch(
                f"rule {rule.find!r} -> {rule.replace!r} expects {rule.count} "
                f"occurrence(s), found {found}"
            )
        js = js.replace(rule.find, rule.replace)
    return js


def _rule_file(app: str, rules_dir: Path | None = None) -> dict:
    path = (rules_dir or RULES_DIR) / f"{app}.yaml"
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuleMismatch(f"{path}: expected a mapping of task name to a list of rules")
    return data


def _rule_entries(app: str, task: str, rules_dir: Path | None = None) -> list[dict]:
    """The raw rule entries of one task, checked. `why` is kept here for the report."""
    entries = _rule_file(app, rules_dir).get(task, [])
    where = f"{app}/{task}"
    if not isinstance(entries, list):
        raise RuleMismatch(f"{where}: expected a list of rules")
    for entry in entries:
        if not isinstance(entry, dict) or not set(entry) <= {"find", "replace", "count", "why"}:
            raise RuleMismatch(f"{where}: a rule may only have find, replace, count, why: {entry}")
        find, replace, count = entry.get("find"), entry.get("replace"), entry.get("count", 1)
        if not isinstance(find, str) or not find or not isinstance(replace, str):
            raise RuleMismatch(f"{where}: `find` and `replace` must be strings: {entry}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise RuleMismatch(f"{where}: `count` must be a positive integer: {entry}")
    return entries


def load_rules(app: str, task: str, rules_dir: Path | None = None) -> list[Rule]:
    """The literal rules of one task, from scenarios/neutralize/<app>.yaml."""
    return [
        Rule(find=e["find"], replace=e["replace"], count=e.get("count", 1))
        for e in _rule_entries(app, task, rules_dir)
    ]


# --- injector names ----------------------------------------------------------


def neutralize_identifiers(text: str) -> str:
    """Rename the injector's global names. Also applied to the runtime's own two files,
    bug_injector.js and init.py, so that all three agree."""
    for name in sorted(IDENT_MAP, key=len, reverse=True):
        text = text.replace(name, IDENT_MAP[name])
    return text


_NEUTRAL_KIND = "st"
_TRANSITION_CALL = re.compile(r"(?<![\w$])transition\s*\(\s*")


def _rename_kind(js: str, neutral: str) -> tuple[str, int]:
    segs = _segments(js)
    view = _code_view(js, segs)
    literal_at = {
        s.start: s
        for s in segs
        if s.kind == "str" or (s.kind == "tpl" and js[s.start] == "`" and js[s.end - 1] == "`")
    }
    out = []
    pos = 0
    renamed = 0
    for m in _TRANSITION_CALL.finditer(view):
        seg = literal_at.get(m.end())
        if seg is None or seg.end - seg.start < 2:
            continue
        out.append(js[pos : seg.start])
        out.append(json.dumps(neutral))
        pos = seg.end
        renamed += 1
    out.append(js[pos:])
    return "".join(out), renamed


def rename_kind(js: str, neutral: str = _NEUTRAL_KIND) -> str:
    """Replace the first string argument of every transition("...") call.

    The benchmark passes "<app>_<task>" there, and the injector uses it in the names of
    two sessionStorage keys.
    """
    return _rename_kind(js, neutral)[0]


# --- one script --------------------------------------------------------------


@dataclass(frozen=True)
class _Changes:
    console_calls: int
    rules: int
    names: int
    keys: int


def _neutralize(app: str, task: str, bug_js: str) -> tuple[str, _Changes]:
    js = strip_comments(bug_js)
    console_calls = len(_console_uses(js))
    js = strip_console(js)
    rules = load_rules(app, task)
    try:
        js = apply_literal_rules(js, rules)
    except RuleMismatch as exc:
        raise RuleMismatch(f"{app}/{task}: {exc}") from None
    names = sum(js.count(name) for name in IDENT_MAP)
    js = neutralize_identifiers(js)
    js, keys = _rename_kind(js, _NEUTRAL_KIND)
    comments = [js[s.start : s.end].strip() for s in _segments(js) if s.kind in ("line", "block")]
    if comments != list(_MARKERS):
        raise ValueError(f"{app}/{task}: the four BEGIN/END marker lines are not intact")
    return js, _Changes(console_calls, len(rules), names, keys)


def neutralize_bug(app: str, task: str, bug_js: str) -> str:
    """Rewrite one bug script. Order: comments, console, literal rules, injector names,
    transition key. The literal rules therefore see code without comments or console
    calls."""
    return _neutralize(app, task, bug_js)[0]


# --- the scan ----------------------------------------------------------------

_HINT_WORDS = frozenset({"fake", "custom", "dummy", "duplicate", "bad"})
_WORD_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_PHRASE = re.compile(r"\bsome\s+(?:page|book)\b", re.I)
_ANYWHERE = re.compile(r"console|__BUG_INJECTOR|__flash_ui_active__", re.I)
_DEAD_LINK = re.compile(r"""href\s*=\s*\\?(["'])#\\?\1|(["'])href\2\s*,\s*(["'])#\3""", re.I)
_STYLE = re.compile(r"\.\s*style\s*\.\s*(color|fontWeight)(?![\w$])", re.I)
_PLAIN_NAME_BEFORE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*$")
_MEMBER = re.compile(r"\.\s*([A-Za-z_$][\w$]*)")
_ESCAPE = re.compile(r"\\.", re.S)


def _excerpt(js: str, start: int, end: int, margin: int = 30) -> str:
    """The line a match is on, or the text around the match when the line is long."""
    line_start = js.rfind("\n", 0, start) + 1
    line_end = js.find("\n", end)
    line = js[line_start : len(js) if line_end < 0 else line_end].strip()
    if len(line) > 100:
        line = "..." + js[max(line_start, start - margin) : end + margin].strip() + "..."
    return " ".join(line.split())


def find_telltales(js: str) -> list[str]:
    """Hints left in a script that a test could see or read at run time.

    Looks at what reaches the page: text and attributes built from string and template
    literals, property names, inline styles, console use and the injector's names.
    Comments do not count, and neither do the names of local variables, which nothing
    outside the script can read. All matching ignores case. The input must be JavaScript:
    in plain text, such as a page snapshot, the words below would not be found.

    - `console`, `__BUG_INJECTOR`, `__flash_ui_active__`: anywhere outside comments.
    - `fake`, `custom`, `dummy`, `duplicate`, `bad` as a whole word, and the phrases
      "Some Page" and "Some Book": inside string and template literals, where a word also
      ends at a capital letter or a digit, so "FakeInvoice123" counts and "badge" and
      "customer" do not. The words are also looked for in property names.
    - A link to "#", as `href = '#'`, `setAttribute('href', '#')` or `href="#"` in HTML.
    - `style.color` and `style.fontWeight` set on an element that is already on the page.
      Styling an element the script itself creates is part of building it, like the
      inline styles in HTML it inserts, and is not reported.
    """
    segs = _segments(js)
    view = _code_view(js, segs)
    live = _blank(js, segs, ("line", "block"), " ")
    hits: list[tuple[int, str]] = []

    for m in _ANYWHERE.finditer(live):
        hits.append((m.start(), f"{m.group()}: {_excerpt(js, m.start(), m.end())}"))
    for m in _DEAD_LINK.finditer(live):
        hits.append((m.start(), f"link to #: {_excerpt(js, m.start(), m.end())}"))

    for m in _STYLE.finditer(view):
        owner = _PLAIN_NAME_BEFORE.search(view, 0, m.start())
        if owner and re.search(
            rf"(?<![\w$.]){re.escape(owner.group(1))}\s*=\s*document\s*\.\s*createElement\s*\(",
            view,
        ):
            continue
        prop = "fontWeight" if m.group(1).lower() == "fontweight" else "color"
        hits.append((m.start(), f"style.{prop}: {_excerpt(js, m.start(), m.end())}"))

    def words(text: str, offset: int) -> None:
        for m in _WORD_PART.finditer(text):
            if m.group().lower() in _HINT_WORDS:
                at = offset + m.start()
                hits.append((at, f"{m.group().lower()}: {_excerpt(js, at, at + len(m.group()))}"))

    for seg in segs:
        if seg.kind in ("str", "tpl"):
            text = _ESCAPE.sub(lambda e: " " * len(e.group()), js[seg.start : seg.end])
            words(text, seg.start)
            for m in _PHRASE.finditer(text):
                at = seg.start + m.start()
                hits.append((at, f"{m.group().lower()}: {_excerpt(js, at, at + len(m.group()))}"))
    for m in _MEMBER.finditer(view):
        words(m.group(1), m.start(1))

    return list(dict.fromkeys(text for _, text in sorted(hits)))


# --- command line and report -------------------------------------------------


def _chosen(selection: dict) -> list[tuple[str, str]]:
    return [(app, c["chosen"]) for app, info in selection.items() for c in info["clusters"]]


def _check_rule_files(selection: dict, rules_dir: Path | None = None) -> None:
    """A rule filed under a task that is not selected would never be applied."""
    for app, info in selection.items():
        chosen = {c["chosen"] for c in info["clusters"]}
        unused = sorted(set(_rule_file(app, rules_dir)) - chosen)
        if unused:
            raise RuleMismatch(f"{app}: rules for tasks that are not selected: {', '.join(unused)}")


def _cell(text: str) -> str:
    """A string as a Markdown table cell in code style."""
    if not text:
        return "(removed)"
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}".replace("|", "\\|").replace("\n", " ")


def build_report(selection: dict) -> str:
    """The disclosure list: every literal rule applied, and what else changed per script."""
    _check_rule_files(selection)
    rule_rows = []
    script_rows = []
    console_calls = scripts_with_rules = scripts_renamed = 0
    for app, task in _chosen(selection):
        _, changes = _neutralize(app, task, load_case(app, task).bug_js)
        for entry in _rule_entries(app, task):
            times = f" x{entry['count']}" if entry.get("count", 1) != 1 else ""
            why = " ".join(str(entry.get("why", "")).split()).replace("|", "\\|")
            rule_rows.append(
                f"| {app} | {task} | {_cell(entry['find'])}{times} "
                f"| {_cell(entry['replace'])} | {why} |"
            )
        script_rows.append(
            f"| {app} | {task} | {changes.console_calls} | {changes.rules} "
            f"| {changes.names} | {changes.keys} |"
        )
        console_calls += changes.console_calls
        scripts_with_rules += bool(changes.rules)
        scripts_renamed += bool(changes.names or changes.keys)

    lines = [
        "# Bug-script neutralization report",
        "",
        "Written by `python -m hoh.neutralize --report` (`just neutralize`). Do not edit by hand:",
        "a test compares this file with a fresh build.",
        "",
        "Every selected bug script is rewritten before it is used (design doc section 5.3). The",
        "rewrite does four things. None of them changes what a bug adds, removes or changes on",
        "the page.",
        "",
        "1. It drops comments. The four `BEGIN`/`END` marker lines stay, because the benchmark's",
        "   loader needs them; they never reach the page.",
        "2. It replaces each `console.*` call with `void 0`.",
        "3. It applies the literal rules below, which live in `scenarios/neutralize/<app>.yaml`.",
        "4. It renames the injector's global names and the key passed to `transition()`.",
        "",
        "The rewritten scripts are written to `build/payloads/` and are not committed.",
        "",
        f"- Scripts: {len(script_rows)}",
        f"- Literal rules: {len(rule_rows)}, in {scripts_with_rules} scripts",
        f"- Console calls replaced: {console_calls}",
        f"- Scripts that call the injector and had its name and key renamed: {scripts_renamed}",
        "",
        "## Literal rules",
        "",
        "One line per rule. `xN` after a value means it is replaced in N places.",
        "",
        "| App | Task | Find | Replace | Why |",
        "|---|---|---|---|---|",
        *rule_rows,
        "",
        "## Renamed names",
        "",
        "The runtime's `bug_injector.js` and `init.py` use the same names. They must go through",
        "`neutralize_identifiers` too when an environment is built, so that the three files agree.",
        "",
        "| Name in the benchmark | Renamed to |",
        "|---|---|",
        *(f"| `{name}` | `{neutral}` |" for name, neutral in IDENT_MAP.items()),
        f"| first argument of `transition()`, which is `<app>_<task>` | `{_NEUTRAL_KIND}` |",
        "",
        "## Changes per script",
        "",
        "| App | Task | Console calls replaced | Literal rules | Injector names renamed "
        "| Transition keys renamed |",
        "|---|---|---|---|---|---|",
        *script_rows,
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Rewrite the selected bug scripts so that they carry no hint of being "
        "synthetic, and optionally write the list of rules applied."
    )
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path, help="write the disclosure list to this file")
    args = parser.parse_args(argv)

    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    _check_rule_files(selection)
    outputs = {}
    leftovers = []
    for app, task in _chosen(selection):
        outputs[app, task] = neutralize_bug(app, task, load_case(app, task).bug_js)
        leftovers += [f"{app}/{task}: {hit}" for hit in find_telltales(outputs[app, task])]
    if leftovers:
        raise SystemExit("Hints left in the rewritten scripts:\n" + "\n".join(leftovers))

    for (app, task), js in outputs.items():
        path = args.out / app / f"{task}.js"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(js, encoding="utf-8")
    print(f"{len(outputs)} scripts written to {args.out}")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(build_report(selection), encoding="utf-8")
        print(f"report written to {args.report}")


if __name__ == "__main__":
    main()
