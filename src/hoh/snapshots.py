"""Per-step accessibility snapshots of each chosen test on the clean app, and a check of targets.

Before a rename or removal scenario is run, the proposed target has to be seen on a real page:
it must exist with the expected role, be unique, and the new wording must confuse no locator
(``scenarios/rename/notes.md``, section 7). This module collects the evidence.

1. ``render_snapshot_test`` renders a test like ``hoh.generator.render_test`` and adds a dump of
   the page after the start and after every step: ``step-00`` is the page before the first
   step, ``step-NN`` the page after step NN (its action and its check), ``step-final`` the page
   just before the final assertion. A dump is the URL on a first line ``# url: <url>`` and then
   ``page.locator("body").aria_snapshot()``. A dump cannot fail the test: it is wrapped in
   try/except and a failed dump leaves no file. Before dumping the helper waits up to three
   seconds for the page's load event (also inside the try), so a snapshot is not taken in the
   middle of a navigation.
2. ``collect`` (``python -m hoh.snapshots --apps ...``) runs each snapshot test once on the
   clean app through a backend, in parallel and resumably (``hoh.queue.run_queue``), and keeps
   ``<out>/<app>/<task>/step-*.yml`` and ``index.json``.
3. ``check_targets`` (``python -m hoh.snapshots check ...``) answers the checklist for each row of
   ``assignment.csv`` that has an ``old`` label.

Parsing rule for an aria snapshot (``parse_snapshot``). Every line that starts with ``- `` after
its indentation is one node; other lines (the ``# url:`` header, blank lines) are ignored. The
text after ``- `` is a YAML key and, after a colon, an optional inline value::

    - link "Name" [attr] [attr]: inline text      role, quoted name, attributes, text
    - button "Say \\"hi\\""                         the name is a JSON string
    - 'link "Say: hi"':                            a key YAML needs to quote is unquoted first
    - text: Some words                             a text node: role "text", no name
    - paragraph: Some words                        a role with only its text

A name is read as a JSON string (backslash escapes), so it is the accessible name as the
browser computed it; ``text`` is the inline value (unquoted when quoted). Lines whose role
starts with ``/`` (``/url``, ``/placeholder``) are properties of the node above and are not
nodes. Nesting is not used: a match is a node, wherever it sits.

Matching, for a target with role R and old label L. *exact*: a node with role R and name equal
to L (same case). *substring*: a node with role R whose name contains L, ignoring case, which
is what ``get_by_role(name=L)`` selects without ``exact``. For ``get_by_text`` and
``get_by_label`` targets there is no role, and a node of any role whose name or text matches
counts. A collision is a node, in any snapshot of the task, whose name or text contains the new
wording, ignoring case.

Output is data derived from the benchmark's pages: it goes under ``results/`` or ``build/``,
which are ignored by git, and is never committed.
"""

from __future__ import annotations

import argparse
import csv
import json
import queue
import re
import shutil
import sys
import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from hoh.backend import Backend, EnvFailure, Variant
from hoh.benchmark import REPO_ROOT, BenchCase, load_case
from hoh.fake_backend import load_backend
from hoh.generator import GenerationError, _indent, render_test, strip_comments
from hoh.jobs import Job
from hoh.queue import Outcome, run_queue

RESULTS_DIR = "/work/test-results"
STEP_RE = re.compile(r"step-(\d\d|final)\.yml")
_HELPER = """\
def _snap_dump(page, name):
    try:
        import pathlib

        url = page.url
        try:
            page.wait_for_load_state("load", timeout=3000)
        except Exception:
            pass
        text = page.locator("body").aria_snapshot(timeout=10000)
        out = pathlib.Path({results_dir!r})
        out.mkdir(parents=True, exist_ok=True)
        (out / (name + ".yml")).write_text(
            "# url: " + str(url) + "\\n" + text + "\\n", encoding="utf-8"
        )
    except Exception:
        pass
"""


# --- the snapshot test ------------------------------------------------------------------


def _groups(tc: BenchCase) -> list[list[str]]:
    """The test's snippets, one list per step (action, then check), in running order."""
    return [[s for s in (step.solution, step.action_check) if s] for step in tc.steps]


def render_snapshot_test(tc: BenchCase, results_dir: str = RESULTS_DIR) -> str:
    """The source of ``render_test(tc)`` with page dumps added (module docstring).

    The statements of the original test are unchanged and in the same order. ``step-00`` is
    dumped first, ``step-NN`` after each step, and ``step-final`` before the final assertion
    (at the end when there is none). ``results_dir`` is where the dumps go; the runner's
    ``/work/test-results`` by default.
    """
    base = render_test(tc)  # validates the original test and gives its import lines
    head, sep, _ = base.partition(f"def test_{tc.task}(")
    parts = ['    _snap_dump(page, "step-00")\n']
    for number, group in enumerate(_groups(tc), start=1):
        parts += [_indent(strip_comments(textwrap.dedent(snippet))) for snippet in group]
        parts.append(f'    _snap_dump(page, "step-{number:02d}")\n')
    parts.append('    _snap_dump(page, "step-final")\n')
    if tc.assertion_check:
        parts.append(_indent(strip_comments(textwrap.dedent(tc.assertion_check))))
    function = f"def test_{tc.task}(page: Page) -> None:\n{''.join(parts)}"
    source = f"{head}{_HELPER.format(results_dir=results_dir)}\n\n{function}"
    try:
        compile(source, f"test_{tc.task}.py", "exec", dont_inherit=True)
    except SyntaxError as err:  # pragma: no cover - render_test accepted the same snippets
        raise GenerationError(f"{tc.app}/{tc.task} does not make a snapshot test: {err}") from err
    assert sep
    return source


# --- collecting -------------------------------------------------------------------------


def selected_tasks(
    apps: Sequence[str], only: Sequence[str] | None, selection: Path | None = None
) -> list[tuple[str, str]]:
    """The chosen (app, task) pairs of ``apps`` in selection order, narrowed by ``app/task``s."""
    path = selection or REPO_ROOT / "scenarios" / "selection.json"
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    pairs = [(app, c["chosen"]) for app in apps for c in data[app]["clusters"]]
    if only:
        wanted = [tuple(item.split("/", 1)) for item in only]
        unknown = [w for w in wanted if w not in pairs]
        if unknown:
            raise ValueError(f"not chosen tasks of {list(apps)}: {unknown}")
        pairs = [p for p in pairs if p in wanted]
    return pairs


def _read_url(path: Path) -> str:
    with path.open(encoding="utf-8") as f:
        first = f.readline().strip()
    return first[len("# url:") :].strip() if first.startswith("# url:") else ""


def _step_names(n_steps: int) -> list[str]:
    return [f"step-{i:02d}" for i in range(n_steps + 1)] + ["step-final"]


def _task_index(task_dir: Path, status: str, n_steps: int, detail: str = "") -> dict:
    expected = _step_names(n_steps)
    urls = {
        p.stem: _read_url(p)
        for p in sorted(task_dir.glob("step-*.yml"))
        if STEP_RE.fullmatch(p.name)
    }
    return {
        "status": status,
        "detail": detail,
        "steps_dumped": len(urls),
        "expected": len(expected),
        "missing": [n for n in expected if n not in urls],
        "urls": urls,
    }


def collect(
    tasks: Sequence[tuple[str, str]],
    out_dir: Path,
    *,
    slots: int,
    make_backend: Callable[[int], Backend],
    load: Callable[[str, str], BenchCase] = load_case,
) -> dict[str, dict]:
    """Run the snapshot test of each task once on the clean app; return and write the index.

    ``make_backend(slot)`` builds the backend of one slot; a worker holds one slot for the
    length of a job, so no two jobs share one. Each job runs through ``hoh.queue.run_queue``
    in ``<out_dir>/_queue/...``: a finished task is not run again, an environment failure is
    retried. The backend's output directory is kept next to the attempt (``backend-out``).
    The step files of a task go to ``<out_dir>/<app>/<task>/``, with its ``index.json``.
    """
    out_dir = Path(out_dir)
    pool: queue.Queue[int] = queue.Queue()
    for slot in range(slots):
        pool.put(slot)
    cases: dict[tuple[str, str], BenchCase] = {}

    def worker(job: Job, attempt_dir: Path) -> Outcome:
        slot = pool.get()
        try:
            tc = load(job.app, job.task)
            cases[(job.app, job.task)] = tc
            tests = attempt_dir / "tests"
            tests.mkdir()
            (tests / f"test_{job.task}.py").write_text(
                render_snapshot_test(tc), encoding="utf-8", newline="\n"
            )
            try:
                record = make_backend(slot).run(
                    tests, Variant(job.app, job.task), attempt_dir / "backend-out"
                )
            except EnvFailure as err:
                return Outcome("env_failed", str(err))
            task_dir = out_dir / job.app / job.task
            task_dir.mkdir(parents=True, exist_ok=True)
            for old in task_dir.glob("step-*.yml"):
                old.unlink()
            for src in sorted(Path(record.results_dir).glob("step-*.yml")):
                if src.is_file() and not src.is_symlink() and STEP_RE.fullmatch(src.name):
                    shutil.copyfile(src, task_dir / src.name)
            index = _task_index(task_dir, record.status, len(tc.steps))
            (task_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n", "utf-8")
            return Outcome("done", record.status)
        finally:
            pool.put(slot)

    jobs = [Job("snap", "clean", app, task, "A", 1) for app, task in tasks]
    run_queue(jobs, worker, out_dir / "_queue", slots=slots)

    index: dict[str, dict] = {}
    for job in jobs:
        record = json.loads((out_dir / "_queue" / job.path / "job.json").read_text("utf-8"))
        task_dir = out_dir / job.app / job.task
        if record["state"] == "done" and (task_dir / "index.json").is_file():
            entry = json.loads((task_dir / "index.json").read_text("utf-8"))
        else:
            entry = {
                "status": record["state"],
                "detail": record.get("detail", ""),
                "steps_dumped": 0,
                "expected": None,
                "missing": [],
                "urls": {},
            }
        index[f"{job.app}/{job.task}"] = entry
    (out_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    return index


# --- parsing an aria snapshot -----------------------------------------------------------


@dataclass(frozen=True)
class Node:
    role: str
    name: str | None = None
    text: str | None = None
    attrs: str = ""


_ROLE = re.compile(r"/?[A-Za-z][\w-]*")


def _scan_double(s: str, i: int) -> int:
    """Index just after the JSON string that starts at ``s[i]``; -1 if it does not end."""
    j = i + 1
    while j < len(s):
        if s[j] == "\\":
            j += 2
        elif s[j] == '"':
            return j + 1
        else:
            j += 1
    return -1


def _scan_single(s: str, i: int) -> int:
    j = i + 1
    while j < len(s):
        if s[j] == "'":
            if s[j + 1 : j + 2] == "'":
                j += 2
                continue
            return j + 1
        j += 1
    return -1


def _unquote(s: str) -> str:
    s = s.strip()
    if s.startswith('"'):
        end = _scan_double(s, 0)
        if end == len(s):
            try:
                return json.loads(s)
            except ValueError:
                return s[1:-1]
    if s.startswith("'"):
        end = _scan_single(s, 0)
        if end == len(s):
            return s[1:-1].replace("''", "'")
    return s


def _parse_key(key: str) -> Node | None:
    m = _ROLE.match(key)
    if not m:
        return None
    role, i = m.group(0), m.end()
    name = None
    if key[i : i + 2] == ' "':
        end = _scan_double(key, i + 1)
        if end < 0:
            return None
        try:
            name = json.loads(key[i + 1 : end])
        except ValueError:
            name = key[i + 2 : end - 1]
        i = end
    attrs = ""
    while (am := re.match(r" \[[^\]]*\]", key[i:])) is not None:
        attrs += am.group(0)
        i += am.end()
    rest = key[i:]
    text = None
    if rest.startswith(":"):
        text = _unquote(rest[1:]) or None
    elif rest.strip():
        return None
    return Node(role, name, text, attrs.strip())


def parse_snapshot(text: str) -> list[Node]:
    """The nodes of an aria snapshot, in order (module docstring for the rule)."""
    nodes: list[Node] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("- "):
            continue
        body = stripped[2:].rstrip()
        node: Node | None
        if body.startswith(("'", '"')):
            end = _scan_single(body, 0) if body[0] == "'" else _scan_double(body, 0)
            if end < 0:
                continue
            key = _unquote(body[:end])
            rest = body[end:]
            node = _parse_key(key)
            if node is not None and rest.startswith(":") and rest[1:].strip():
                node = Node(node.role, node.name, _unquote(rest[1:]), node.attrs)
        else:
            node = _parse_key(body)
        if node is None or node.role.startswith("/"):
            continue
        nodes.append(node)
    return nodes


# --- checking the targets ---------------------------------------------------------------


def _load_task(snapshot_dir: Path, app: str, task: str) -> dict[str, list[Node]]:
    task_dir = Path(snapshot_dir) / app / task
    if not task_dir.is_dir():
        return {}
    return {
        p.stem: parse_snapshot(p.read_text(encoding="utf-8"))
        for p in sorted(task_dir.glob("step-*.yml"))
        if STEP_RE.fullmatch(p.name)
    }


def _contains(haystack: str | None, needle: str) -> bool:
    return bool(haystack) and needle.lower() in haystack.lower()  # type: ignore[union-attr]


def _label(node: Node) -> str:
    return f"{node.role}: {node.name if node.name is not None else node.text}"


_FIRST_USE = re.compile(r"^(\d+)([cs])[#!~]*$")


def _page_of_use(step: int, where: str) -> str:
    """The snapshot that shows the page where the test first uses the label in step ``step``.

    Where that first use is the action of the step (``where`` starts with ``Ns``, or is empty),
    it is the page before the step, ``step-(N-1)``. Where it is the check after the step
    (``Nc``, as for a control inside a menu that the step opens), it is the page after it,
    ``step-N``.
    """
    first = _FIRST_USE.match((where.split() or [""])[0])
    return f"step-{step if first and first.group(2) == 'c' else step - 1:02d}"


def check_targets(snapshot_dir: Path, assignment_csv: Path) -> list[dict]:
    """Answer the snapshot checklist for each row of the assignment that has an ``old`` label.

    The page of use is the snapshot of step N-1 (``step-00`` for step 1) when the test first
    acts on the label in step N, and the snapshot of step N when its first use is the check
    after step N (``where`` starts with ``Nc``).

    Per row (keys): ``snapshot``, ``found`` (exact match exists), ``exact`` and ``substring``
    (counts), ``unique`` (exactly one substring match), ``present_in`` (every snapshot of the
    task with a substring match), ``text_elsewhere`` (other nodes whose text contains the old
    label), ``other_role_names`` (other nodes whose name contains it), ``collision``, ``collisions``
    (first ten nodes in any snapshot of the task whose name or text contains the new wording),
    ``n_collisions``, ``contained_in_new`` (names of four characters or more inside the new
    wording, which rule 4 of the notes also counts), ``roles`` (roles of the matches) and
    ``problem`` (``no snapshot`` or ``no step`` when nothing could be checked).
    """
    with Path(assignment_csv).open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("old") or "").strip()]
    cache: dict[tuple[str, str], dict[str, list[Node]]] = {}
    results = []
    for row in rows:
        app, task, old, new = row["app"], row["task"], row["old"], row.get("new") or ""
        role_spec = row.get("role") or ""
        role = role_spec.split("=", 1)[1] if role_spec.startswith("role=") else None
        result: dict = {
            "app": app,
            "task": task,
            "role": role_spec,
            "old": old,
            "new": new,
            "step": row.get("step", ""),
            "snapshot": None,
            "found": None,
            "exact": None,
            "substring": None,
            "unique": None,
            "present_in": [],
            "text_elsewhere": None,
            "other_role_names": [],
            "collision": None,
            "collisions": [],
            "n_collisions": None,
            "contained_in_new": [],
            "roles": [],
            "problem": "",
        }
        results.append(result)
        snaps = cache.setdefault((app, task), _load_task(snapshot_dir, app, task))
        try:
            used_on = _page_of_use(int(row["step"]), row.get("where") or "")
        except (TypeError, ValueError):
            result["problem"] = "no step"
            continue
        result["snapshot"] = used_on
        if used_on not in snaps:
            result["problem"] = "no snapshot"
            continue

        def is_match(n: Node, role=role, old=old) -> bool:
            if role is not None:
                return n.role == role and _contains(n.name, old)
            return _contains(n.name, old) or _contains(n.text, old)

        def is_exact(n: Node, role=role, old=old) -> bool:
            if role is not None:
                return n.role == role and n.name == old
            return n.name == old or n.text == old

        page = snaps[used_on]
        matches = [n for n in page if is_match(n)]
        result["exact"] = sum(1 for n in page if is_exact(n))
        result["substring"] = len(matches)
        result["found"] = result["exact"] >= 1
        result["unique"] = len(matches) == 1
        result["roles"] = sorted({n.role for n in matches})
        result["present_in"] = [name for name, nodes in snaps.items() if any(map(is_match, nodes))]
        others = [n for n in page if n not in matches]
        result["text_elsewhere"] = sum(1 for n in others if _contains(n.text, old))
        result["other_role_names"] = [_label(n) for n in others if _contains(n.name, old)]
        if new:
            hits = [
                {"snapshot": name, "role": n.role, "name": n.name, "text": n.text}
                for name, nodes in snaps.items()
                for n in nodes
                if _contains(n.name, new) or _contains(n.text, new)
            ]
            result["n_collisions"] = len(hits)
            result["collision"] = bool(hits)
            result["collisions"] = hits[:10]
            inside = {
                n.name
                for nodes in snaps.values()
                for n in nodes
                if n.name and len(n.name) >= 4 and n.name.lower() in new.lower()
            }
            result["contained_in_new"] = sorted(inside)[:10]
    return results


def _table(results: list[dict]) -> str:
    cols = [
        "app/task",
        "step",
        "role",
        "old",
        "snap",
        "exact",
        "sub",
        "uniq",
        "text+",
        "coll",
        "note",
    ]
    lines = []
    for r in results:

        def yn(v):
            return "-" if v is None else ("yes" if v else "NO")

        lines.append(
            [
                f"{r['app']}/{r['task']}",
                str(r["step"]),
                r["role"].replace("role=", ""),
                r["old"],
                r["snapshot"] or "-",
                "-" if r["exact"] is None else str(r["exact"]),
                "-" if r["substring"] is None else str(r["substring"]),
                yn(r["unique"]),
                "-" if r["text_elsewhere"] is None else str(r["text_elsewhere"]),
                "-" if r["collision"] is None else ("COLLIDES" if r["collision"] else "ok"),
                r["problem"] or ("not found" if r["found"] is False else ""),
            ]
        )
    widths = [
        max(len(c), *(len(row[i]) for row in lines)) if lines else len(c)
        for i, c in enumerate(cols)
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return "\n".join([fmt.format(*cols), *(fmt.format(*row) for row in lines)])


# --- command line -----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "check":
        parser = argparse.ArgumentParser(prog="hoh.snapshots check")
        parser.add_argument("--snapshots", type=Path, required=True)
        parser.add_argument("--assignment", type=Path, required=True)
        args = parser.parse_args(argv[1:])
        results = check_targets(args.snapshots, args.assignment)
        print(_table(results))
        path = args.snapshots / "targets_check.json"
        path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        print(f"\n{len(results)} rows; written to {path}")
        return

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apps", nargs="+", required=True)
    parser.add_argument("--only", nargs="+", metavar="APP/TASK")
    parser.add_argument("--out", type=Path, default=Path("results/snapshots"))
    parser.add_argument("--slots", type=int, default=1)
    parser.add_argument("--backend-factory", default="hoh.docker_backend:DockerBackend")
    parser.add_argument("--stacks-root", required=True)
    # Compose project names are global to the Docker daemon: never the sessions' (hoh) or the
    # validation's (hohv), so that snapshots can run beside them.
    parser.add_argument("--project-prefix", default="hohs")
    parser.add_argument("--selection", type=Path)
    args = parser.parse_args(argv)

    tasks = selected_tasks(args.apps, args.only, args.selection)

    def make_backend(slot: int) -> Backend:
        options = {"stacks_root": args.stacks_root, "slot": slot,
                   "project_prefix": args.project_prefix}
        return load_backend({"factory": args.backend_factory, "options": options})

    index = collect(tasks, args.out, slots=args.slots, make_backend=make_backend)
    for key, entry in index.items():
        print(f"{key}: {entry['status']}, {entry['steps_dumped']}/{entry['expected']} dumps")
    bad = [k for k, e in index.items() if e["status"] not in ("pass", "fail") or e["missing"]]
    print(
        f"{len(index)} tasks; {len(bad)} need a look: {bad}"
        if bad
        else f"{len(index)} tasks, all dumped"
    )


if __name__ == "__main__":
    main()
