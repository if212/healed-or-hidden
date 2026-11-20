"""The canned files of the fidelity check's checkpoint, from the port of
bookstack/update_book.

    uv run python -m hoh.fidelity.canned --ports build/fidelity/ports.json \\
        --validation results/validation --out build/fidelity/canned

The benchmark's final assertion for update_book is three statements, and on A+M the test turns
red at the first: the grid's book headings (Python line 21 in stage 1a's validation, port line
20). Each canned file makes that check, or the click on the header's Books link before it,
through one route a test writer or a healer could take (``CANNED``). The race runs each one 10
times on A+M, where it must be red at that line or indeterminate every time, and 3 times on A,
where it must be green: red on A+M then means that the fixture's wait covered the route and the
change was seen. Two files are there for the static check, which must refuse them; they are not
raced.

The files are build outputs (CLAUDE.md rules 2 and 3). This module holds the edits: each is a
small replacement of text in the port that must apply exactly once, changes only the lines its
text is on, and keeps every line where it was. The line that turns red comes from the
validation's A-M signature through the converter's line map, never from the text, and the
routes must sit on it. ``canned.json`` names that line and lists each file with its SHA-256, the
line and its expected result, ``raced`` or ``refused``; the go rule (``hoh.fidelity.verdict``)
reads it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hoh.fidelity import validations

APP, TASK = "bookstack", "update_book"
SCENARIO, CHECK = f"b_oracle/{APP}/{TASK}", "A-M"
"""Stage 1a's scenario and check whose red line the routes sit on."""
RACED, REFUSED = "raced", "refused"

# The text of the port that the edits replace.
IMPORT = 'import { test, expect } from "pwfix";'
TOP = 'from "pwfix";\n\ntest('
BOOKS = 'await page.locator("#header").getByRole("link", { name: "Books", exact: true }).click();'
_LINK = 'page.locator("#header").getByRole("link", { name: "Books", exact: true })'
_HEADINGS = '"main .grid-card h2"'
_LIST = '["Book1", "Book2", "Updated Book"]'
FINAL = f"await expect(page.locator({_HEADINGS})).toHaveText({_LIST});"
"""The statement that turns red on A+M."""
# Reads compared as toHaveText compares strings: whitespace collapsed, ends trimmed.
_PLAIN = '.map((text) => (text ?? "").replace(/\\s+/g, " ").trim())).toEqual(' + _LIST + ");"
_THREE = ("[await headings.nth(0).textContent(), await headings.nth(1).textContent(), "
          "await headings.nth(2).textContent()]")


@dataclass(frozen=True)
class Canned:
    name: str
    route: str
    expected: str
    edits: tuple[tuple[str, str], ...] = ()


CANNED: tuple[Canned, ...] = (
    Canned("01-port", "the port as converted", RACED),
    Canned("02-import-expect", "expect imported from @playwright/test", REFUSED, (
        (IMPORT, 'import { test } from "pwfix"; import { expect } from "@playwright/test";'),
    )),
    Canned("03-keyboard", "the Books link pressed with page.keyboard: focus, then Enter", RACED, (
        (BOOKS, f'await {_LINK}.focus(); await page.keyboard.press("Enter");'),
    )),
    Canned("04-mouse", "the Books link clicked with page.mouse at the middle of its box, once "
           "the link takes events (the click's checks as a trial)", RACED, (
        (BOOKS, f"await {_LINK}.click({{ trial: true }}); "
                f"const box = await {_LINK}.boundingBox(); "
                "await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);"),
    )),
    Canned("05-context-pages", "the headings checked on page.context().pages()[0]", RACED, (
        (FINAL, f"await expect(page.context().pages()[0].locator({_HEADINGS}))"
                f".toHaveText({_LIST});"),
    )),
    Canned("06-locator-page", "the headings checked on a locator's page()", RACED, (
        (FINAL, f'await expect(page.locator("main").page().locator({_HEADINGS}))'
                f".toHaveText({_LIST});"),
    )),
    Canned("07-element-handle",
           "each heading read through an element handle's textContent(), checked plainly",
           RACED, (
        (FINAL, "const texts = []; for (let i = 0; i < 3; i++) { const handle = await "
                f"page.locator({_HEADINGS}).nth(i).elementHandle(); "
                "texts.push(await handle.textContent()); } expect(texts" + _PLAIN),
    )),
    Canned("08-text-content",
           "each heading read with its locator's textContent(), checked plainly", RACED, (
        (FINAL, f"const headings = page.locator({_HEADINGS}); expect({_THREE}" + _PLAIN),
    )),
    Canned("09-test-expect", "the headings checked through test.expect", RACED, (
        (FINAL, f"await test.expect(page.locator({_HEADINGS})).toHaveText({_LIST});"),
    )),
    Canned("10-console-handler",
           "the headings read in a page.once('console') handler, from the page the handler "
           "gets, after a console.log the test triggers", RACED, (
        (FINAL, 'const read = new Promise((resolve) => page.once("console", (message) => '
                "resolve((async () => { const headings = "
                f"message.page().locator({_HEADINGS}); return {_THREE}; }})()))); "
                "await page.evaluate(\"console.log('read')\"); "
                "expect((await read)" + _PLAIN),
    )),
    Canned("11-process-env", "a top-level read of process.env", REFUSED, (
        (TOP, 'from "pwfix";\nconst settings = process.env;\ntest('),
    )),
)


def _span(text: str, old: str) -> set[int]:
    """The lines ``old`` is on in ``text``, where it is exactly once."""
    first = text[:text.index(old)].count("\n") + 1
    return set(range(first, first + old.count("\n") + 1))


def make(port: str, final: int) -> list[tuple[Canned, str]]:
    """Each canned file's text, made from ``port``, whose line ``final`` is the statement that
    turns red (``FINAL``). Raises ValueError when that line is not ``FINAL``, or when an edit
    does not apply exactly once, moves a line, or changes a line its text is not on."""
    lines = port.splitlines()
    if not 0 < final <= len(lines) or FINAL not in lines[final - 1]:
        raise ValueError(f"the port's line {final} is not {FINAL!r}; the routes must sit on "
                         f"the line that turns red")
    made = []
    for item in CANNED:
        text, spans = port, set()
        for old, new in item.edits:
            if text.count(old) != 1:
                raise ValueError(f"{item.name}: {old!r} is in the port {text.count(old)} "
                                 f"times; an edit applies exactly once")
            spans |= _span(text, old)
            text = text.replace(old, new)
        after = text.splitlines()
        if text.count("\n") != port.count("\n") or len(after) != len(lines):
            raise ValueError(f"{item.name}: an edit moved a line")
        changed = {n for n, (a, b) in enumerate(zip(lines, after, strict=True), start=1)
                   if a != b}
        if not changed <= spans:
            raise ValueError(f"{item.name}: an edit changed lines {sorted(changed - spans)}")
        made.append((item, text))
    return made


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hoh.fidelity.canned", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ports", required=True, type=Path, help="the converter's ports.json")
    parser.add_argument("--validation", required=True, type=Path,
                        help="stage 1a's validation (results/validation): its A-M signature "
                             "gives the line that turns red")
    parser.add_argument("--out", required=True, type=Path, help="where the files go")
    args = parser.parse_args(argv)

    ports = json.loads(args.ports.read_text(encoding="utf-8"))["ports"]
    entry = next((e for e in ports if e.get("kind") == "tests" and e.get("app") == APP
                  and e.get("task") == TASK and e.get("output")), None)
    if entry is None:
        print(f"{args.ports} has no port of {APP}/{TASK}", file=sys.stderr)
        return 2
    port_path = args.ports.parent / entry["output"]
    data = port_path.read_bytes()
    if _sha256(data) != entry["output_sha256"]:
        print(f"{port_path} has changed since {args.ports} was written; convert again",
              file=sys.stderr)
        return 2
    try:
        red = validations.red_line([validations.load(args.validation)], SCENARIO, CHECK)
    except (OSError, KeyError, ValueError) as exc:
        print(f"{args.validation}: {exc}", file=sys.stderr)
        return 2
    final = entry.get("lines", {}).get(str(red))
    if final is None:
        print(f"{args.ports} maps no line of the port to the Python test's line {red}",
              file=sys.stderr)
        return 2
    try:
        made = make(data.decode("utf-8"), final)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    names = {f"{item.name}.spec.ts" for item, _ in made}
    for old in args.out.glob("*.spec.ts"):
        if old.name not in names:
            old.unlink()
    files: list[dict[str, Any]] = []
    for item, text in made:
        payload = text.encode("utf-8")
        (args.out / f"{item.name}.spec.ts").write_bytes(payload)
        files.append({"name": item.name, "route": item.route, "path": f"{item.name}.spec.ts",
                      "sha256": _sha256(payload), "final_line": final,
                      "expected": item.expected})
    summary = {"schema": 1, "app": APP, "task": TASK,
               "port": {"path": str(port_path), "sha256": _sha256(data)},
               "red_line": {"validation": str(args.validation), "scenario": SCENARIO,
                            "check": CHECK, "python_line": red, "port_line": final},
               "files": files}
    (args.out / "canned.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    refused = sum(f["expected"] == REFUSED for f in files)
    print(f"{len(files)} canned files in {args.out} ({len(files) - refused} to race, {refused} "
          f"for the static check to refuse); map: {args.out / 'canned.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
