"""Write the scripted test of each selected benchmark task as a pytest file.

A generated test is the benchmark's Playwright code and nothing else. The
benchmark's comments say what a test is meant to check, so they are removed:
the test an agent is handed does not explain itself (design doc section 5.4).
The files are build outputs and are never committed.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import textwrap
import tokenize
from collections.abc import Iterator
from pathlib import Path

from hoh.benchmark import BenchCase, load_case

_INDENT = "    "

# Token types that describe layout and hold no code of their own. NEWLINE is
# not one of them: alone on a line, it ends a statement that a backslash
# carried over from the line above, and that line has to stay.
_LAYOUT = frozenset({tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER})


class GenerationError(ValueError):
    """A test case's code cannot be turned into a valid test."""


def strip_comments(src: str) -> str:
    """Remove the comments from Python source, and every line with no code on it.

    Comments are found with the tokenizer, so a ``#`` inside a string literal
    is not taken for one. Blank lines go whether or not a comment stood on
    them, except inside a multi-line string, where they are part of the string.
    """
    comments: dict[int, tokenize.TokenInfo] = {}
    code_rows: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            comments[tok.start[0]] = tok
        elif tok.type not in _LAYOUT:
            code_rows.update(range(tok.start[0], tok.end[0] + 1))

    kept = []
    for row, line in enumerate(io.StringIO(src), start=1):
        if row in comments:
            tok = comments[row]
            line = line[: tok.start[1]].rstrip() + line[tok.end[1] :]
        if row in code_rows or line.strip():
            kept.append(line)
    return "".join(kept)


def _indent(code: str) -> str:
    """Indent code by one level and end it with a newline.

    A line that starts inside a multi-line string is part of the string's
    value and is left as it is.
    """
    in_string: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(code).readline):
        in_string.update(range(tok.start[0] + 1, tok.end[0] + 1))

    lines = [
        line if row in in_string else _INDENT + line
        for row, line in enumerate(io.StringIO(code), start=1)
    ]
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return "".join(lines)


def _snippets(tc: BenchCase) -> Iterator[str]:
    """The test's code in running order: each step's action, then its check; the assertion last."""
    for step in tc.steps:
        yield step.solution
        if step.action_check:
            yield step.action_check
    if tc.assertion_check:
        yield tc.assertion_check


def render_test(tc: BenchCase) -> str:
    """The source of one pytest module: the imports and a single test function.

    The function body is the test case's snippets one after another, with
    nothing in between that would show where one step ends and the next
    begins. The step's expected URL and the prose fields are not part of the
    test.
    """
    try:
        body = "".join(
            _indent(strip_comments(textwrap.dedent(snippet))) for snippet in _snippets(tc)
        )
        function = f"def test_{tc.task}(page: Page) -> None:\n{body}"
        tree = ast.parse(function)
        # Compile as well: an `await` in a plain function parses but does not compile.
        compile(tree, f"test_{tc.task}.py", "exec", dont_inherit=True)
    except (SyntaxError, tokenize.TokenError) as err:
        raise GenerationError(f"{tc.app}/{tc.task} does not make a valid test: {err}") from err

    imports = "from playwright.sync_api import Page, expect\n"
    if any(isinstance(node, ast.Name) and node.id == "re" for node in ast.walk(tree)):
        imports = "import re\n\n" + imports
    return f"{imports}\n\n{function}"


def write_tests(app: str, tasks: list[str], out_dir: Path, root: Path | None = None) -> list[Path]:
    """Write ``out_dir/<app>/test_<task>.py`` for each task; return the paths in task order."""
    app_dir = Path(out_dir) / app
    app_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for task in tasks:
        path = app_dir / f"test_{task}.py"
        path.write_text(render_test(load_case(app, task, root)), encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    for app, info in selection.items():
        paths = write_tests(app, [cluster["chosen"] for cluster in info["clusters"]], args.out)
        print(f"{app}: {len(paths)} tests in {args.out / app}")


if __name__ == "__main__":
    main()
