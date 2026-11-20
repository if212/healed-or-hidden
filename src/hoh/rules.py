"""The rename and remove rules of the families that change the page, and the reference fixes
made from them.

    python -m hoh.rules fixes --rules FILE [--tests-root build/tests] [--out build/reference_fixes]

One target per test (design doc 5.3): the control C-rename renames (A+C, and A+C+M for Mixed)
is the one B-remove removes (A+R). The rules file is committed; it is YAML::

    schema: 1
    apps:
      <app>:
        <task>: {role: <role>, name: <label now>, new_name: <label after the rename>,
                 exact: true, assertion: false}

``role`` is one of ``hoh.pagescripts.ROLES``; ``exact`` (default true) matches the whole name,
case-sensitive, as ``get_by_role(role, name=..., exact=True)`` does. Every rule must be one
``hoh.pagescripts.rule_script`` accepts, for the rename and for the removal: in particular the
new name may not contain the old one and may not name the study. One label gets one
replacement across the tests of an app (rule 4). Unknown keys are refused.

The reference fix of a test is the generated test with every locator of the control renamed:
each ``get_by_role(<role>, name=<label>)`` whose name is the rule's label (whitespace
collapsed, case ignored, as the locator itself matches) gets the new name, written with the
same quotes. Nothing else is touched: a text locator or a heading with the same words may be
another element, so each string that still holds the old label is named in a note for the
person who reviews the fix. A test with no such locator has no reference fix. Fixes are
build outputs (``build/reference_fixes/<app>/test_<task>.py``) and are never committed.

The shadow oracle of a renamed variant checks the reference fix's final assertion: the
benchmark's final assertion with the same locators renamed (``RuleSet.oracle_snippet_for``).

A target with ``assertion: true`` (C-assert, design doc 5.3 and 5.7) is interface text that an
assertion's expected value checks, not a control the test acts through. The page rule is the
same rename; the reference fix and the shadow oracle change the expected value instead of a
locator: every string of a ``to_have_text`` or ``to_contain_text`` expected value (the argument,
or a string in a list argument) that is the old text, whitespace collapsed and case-sensitive as
the assertion compares, becomes the new text. Each other string that still holds the old text
is named in a note, as above. Such a target is an ``AssertionTarget``, the only one whose record
(``asdict``) has an ``assertion`` key: every other target is recorded as before C-assert.

This module makes a target's page rules, oracle check and reference fix, so a run of a rule
records its digest (``rules_source_digest``), a 1b baseline names it, and a stage refuses a
baseline made with other code. It is kept out of ``hoh.envs.ENVIRONMENT_FILES``, whose digest
every baseline names, 1a's too: a change to it concerns only the families with rules.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import sys
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import yaml

from hoh.backend import Variant
from hoh.benchmark import REPO_ROOT, load_case
from hoh.generator import write_tests
from hoh.pagescripts import ROLES, RuleSpec, rule_script

SCHEMA = 1
#: The families whose variants change a control by a rule, and the rule each hands out.
RULE_FAMILIES: Mapping[str, str] = {
    "b_remove": "remove", "c_rename": "rename", "c_assert": "rename", "mixed": "rename",
}
DEFAULT_TESTS_ROOT = "build/tests"
DEFAULT_FIXES_ROOT = "build/reference_fixes"
_TARGET_KEYS = {"role", "name", "new_name", "exact", "assertion"}
#: The assertions whose expected text the reference fix of an assertion target changes.
TEXT_MATCHERS = ("to_have_text", "to_contain_text")
#: This module, as the records and baselines that name its digest call it.
RULES_SOURCE = "src/hoh/rules.py"


def rules_source_digest() -> str:
    """SHA-256 of this module's source (``RULES_SOURCE``)."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _norm(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True)
class Target:
    """The control one test's rename renames and its removal removes.

    ``assertion`` is a class attribute here, not a field: ``asdict`` of a target gives the four
    keys that the baselines validated before C-assert recorded, and a stage compares them."""

    role: str
    name: str
    new_name: str
    exact: bool = True
    assertion: ClassVar[bool] = False

    def spec(self, op: str) -> RuleSpec:
        """The rule of a rename (``op="rename"``) or a removal (``op="remove"``)."""
        return RuleSpec(op, self.role, self.name, self.new_name if op == "rename" else None,
                        self.exact)


@dataclass(frozen=True)
class AssertionTarget(Target):
    """A target with ``assertion: true`` (C-assert): text that the test checks only as an
    expected value. Here ``assertion`` is a field, so its baseline records it."""

    assertion: bool = field(default=True, init=False)


@dataclass(frozen=True)
class RuleSet:
    """The targets of a rules file: app -> task -> target."""

    targets: Mapping[str, Mapping[str, Target]]
    path: Path | None = None

    def target(self, app: str, task: str) -> Target | None:
        return self.targets.get(app, {}).get(task)

    def missing(self, scenarios: Iterable[tuple[str, str, str]]) -> list[str]:
        """The keys (family/app/task) of the scenarios of rule families without a target."""
        return [f"{family}/{app}/{task}" for family, app, task in scenarios
                if family in RULE_FAMILIES and self.target(app, task) is None]

    def specs_for(self, variant: Variant) -> tuple[RuleSpec, ...]:
        """The rules a variant applies: none without a rule, else the one of its target."""
        if variant.rule is None:
            return ()
        target = self.target(variant.app, variant.task)
        if target is None:
            raise ValueError(f"{variant.app}/{variant.task} ({variant.code}): the rules file "
                             f"has no target for this test")
        return (target.spec(variant.rule),)

    def oracle_snippet_for(self, variant: Variant) -> str | None:
        """The final assertion the shadow oracle checks on a renamed variant: the benchmark's,
        with the target's locators renamed, or for an assertion target its expected values
        rewritten. None for every other variant (the stack's own)."""
        if variant.rule != "rename":
            return None
        [spec] = self.specs_for(variant)
        target = self.target(variant.app, variant.task)
        assert target is not None and spec.op == "rename"
        text = textwrap.dedent(load_case(variant.app, variant.task).assertion_check or "").strip()
        if not text:
            return None
        rewrite = rewrite_expected if target.assertion else rename_in_source
        renamed, _, _ = rewrite(text + "\n", target)
        return renamed


def _target(value: Any, where: str) -> Target:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a mapping with role, name and new_name")
    unknown = sorted(str(k) for k in value if k not in _TARGET_KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}; the keys are "
                         f"{', '.join(sorted(_TARGET_KEYS))}")
    for key in ("role", "name", "new_name"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"{where}: {key} must be a non-empty string, got {value.get(key)!r}")
    exact = value.get("exact", True)
    if not isinstance(exact, bool):
        raise ValueError(f"{where}: exact must be true or false, got {exact!r}")
    assertion = value.get("assertion", False)
    if not isinstance(assertion, bool):
        raise ValueError(f"{where}: assertion must be true or false, got {assertion!r}")
    if value["role"] not in ROLES:
        raise ValueError(f"{where}: unknown role {value['role']!r}; one of {', '.join(ROLES)}")
    kind = AssertionTarget if assertion else Target
    target = kind(value["role"], value["name"], value["new_name"], exact)
    for op in ("rename", "remove"):
        try:
            rule_script([target.spec(op)], ready_key=None)  # the checks the page script makes
        except ValueError as exc:
            raise ValueError(f"{where}: {exc}") from None
    return target


def load_rules(path: Path | str) -> RuleSet:
    """Read and check a rules file (module docstring). Raises ValueError on any problem."""
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a rules file is a mapping with schema and apps")
    unknown = sorted(str(k) for k in data if k not in ("schema", "apps"))
    if unknown:
        raise ValueError(f"{path}: unknown key(s) {', '.join(unknown)}; the keys are apps, schema")
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{path}: schema must be {SCHEMA}, got {data.get('schema')!r}")
    apps = data.get("apps")
    if not isinstance(apps, dict) or not apps:
        raise ValueError(f"{path}: apps must map each app to its tasks' targets")
    targets: dict[str, dict[str, Target]] = {}
    for app, tasks in apps.items():
        if not isinstance(tasks, dict) or not tasks:
            raise ValueError(f"{path}: apps.{app} must map tasks to targets")
        targets[app] = {}
        seen: dict[tuple[str, str], tuple[str, Target]] = {}
        for task, value in tasks.items():
            target = _target(value, f"{path}: apps.{app}.{task}")
            label = (target.role, _norm(target.name))
            if label in seen:
                first_task, first = seen[label]
                if (_norm(first.new_name), first.exact) != (_norm(target.new_name), target.exact):
                    raise ValueError(
                        f"{path}: apps.{app}: the {target.role} {target.name!r} is renamed "
                        f"{first.new_name!r} for {first_task} and {target.new_name!r} for "
                        f"{task}; one label gets one replacement in every test of an app"
                    )
            seen.setdefault(label, (task, target))
            targets[app][task] = target
    return RuleSet(targets=targets, path=path)


# --- the rewrite


def _literal(value: str, original: str) -> str:
    """``value`` as a Python string written like ``original`` (its quote), when it can be."""
    quote = original[:1]
    simple = (quote in ("'", '"') and len(original) >= 2 and original[-1] == quote
              and not original.startswith(quote * 3))
    if simple and quote not in value and "\\" not in value and "\n" not in value:
        return f"{quote}{value}{quote}"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _locator_name(call: ast.Call, role: str) -> ast.Constant | None:
    """The name string of a ``get_by_role(role, name="...")`` call, when it is one."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "get_by_role"):
        return None
    given = call.args[0] if call.args else next(
        (k.value for k in call.keywords if k.arg == "role"), None)
    if not (isinstance(given, ast.Constant) and given.value == role):
        return None
    name = next((k.value for k in call.keywords if k.arg == "name"), None)
    if isinstance(name, ast.Constant) and isinstance(name.value, str):
        return name
    return None


def _expected_text(call: ast.Call) -> ast.expr | None:
    """The expected value of a ``to_have_text`` or ``to_contain_text`` call, when it is one."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr in TEXT_MATCHERS):
        return None
    if call.args:
        return call.args[0]
    return next((k.value for k in call.keywords if k.arg == "expected"), None)


def _rewrite(source: str, tree: ast.Module, found: list[ast.Constant],
             target: Target) -> tuple[str, int, list[str]]:
    """``source`` with the strings ``found`` written as the new text; how many; and a note for
    each other string that still holds the old text (line number and the string)."""
    data = source.encode("utf-8")
    starts = [0]
    for line in data.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))

    def span(node: ast.expr) -> tuple[int, int]:
        assert node.end_lineno is not None and node.end_col_offset is not None
        return (starts[node.lineno - 1] + node.col_offset,
                starts[node.end_lineno - 1] + node.end_col_offset)

    old = _norm(target.name).lower()
    notes = [
        f"line {node.lineno}: {node.value!r} still has the old label {target.name!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node not in found and old in _norm(node.value).lower()
    ]
    notes.sort(key=lambda note: int(note.split(":")[0].split()[1]))
    for node in sorted(found, key=lambda n: span(n)[0], reverse=True):
        start, end = span(node)
        written = _literal(_norm(target.new_name), data[start:end].decode("utf-8"))
        data = data[:start] + written.encode("utf-8") + data[end:]
    return data.decode("utf-8"), len(found), notes


def rename_in_source(source: str, target: Target) -> tuple[str, int, list[str]]:
    """``source`` with every locator of the target renamed; how many were renamed; and a note
    for each other string that still holds the old label (line number and the string)."""
    tree = ast.parse(source)
    old = _norm(target.name).lower()
    renamed: list[ast.Constant] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _locator_name(node, target.role)
            if name is not None and _norm(name.value).lower() == old:
                renamed.append(name)
    return _rewrite(source, tree, renamed, target)


def rewrite_expected(source: str, target: Target) -> tuple[str, int, list[str]]:
    """``source`` with every expected value that is the target's text rewritten (module
    docstring: a string argument of ``to_have_text`` or ``to_contain_text``, or a string in a
    list argument; whitespace collapsed, case-sensitive); how many were rewritten; and a note for
    each other string that still holds the old text."""
    tree = ast.parse(source)
    old = _norm(target.name)
    found: list[ast.Constant] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            value = _expected_text(node)
            items = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
            found += [item for item in items if isinstance(item, ast.Constant)
                      and isinstance(item.value, str) and _norm(item.value) == old]
    return _rewrite(source, tree, found, target)


def reference_fix(test_text: str, target: Target) -> tuple[str, list[str]]:
    """The reference fix of a generated test, and the notes for its review: the target's
    locators renamed, or for an assertion target its expected values rewritten. Raises
    ValueError when there is nothing to change: the test would be its own fix."""
    if target.assertion:
        fixed, count, notes = rewrite_expected(test_text, target)
        if count == 0:
            raise ValueError(f"no expected value of {' or '.join(TEXT_MATCHERS)} in the test is "
                             f"{target.name!r}; there is nothing to rewrite")
        return fixed, notes
    fixed, count, notes = rename_in_source(test_text, target)
    if count == 0:
        raise ValueError(f"no get_by_role({target.role!r}, name={target.name!r}) locator in the "
                         f"test; there is nothing to rename")
    return fixed, notes


def fix_path(root: Path, app: str, task: str) -> Path:
    return Path(root) / app / f"test_{task}.py"


def write_fix(rules: RuleSet, tests_root: Path, fixes_root: Path, app: str,
              task: str) -> tuple[Path, list[str]]:
    """Make ``<fixes_root>/<app>/test_<task>.py`` from the generated test (generated first when
    missing) and the target of the test; leave a file that is already right untouched."""
    target = rules.target(app, task)
    if target is None:
        raise ValueError(f"{app}/{task}: the rules file has no target for this test")
    test = fix_path(tests_root, app, task)
    if not test.is_file():
        write_tests(app, [task], tests_root)
    fixed, notes = reference_fix(test.read_text(encoding="utf-8"), target)
    path = fix_path(fixes_root, app, task)
    if not path.is_file() or path.read_text(encoding="utf-8") != fixed:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f".{path.name}.tmp")
        partial.write_text(fixed, encoding="utf-8")
        partial.replace(path)
    return path, notes


# --- the command line


def _resolved(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hoh.rules", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    fixes = commands.add_parser("fixes", help="write the reference fix of every target")
    fixes.add_argument("--rules", required=True, help="the rules file (YAML)")
    fixes.add_argument("--tests-root", default=DEFAULT_TESTS_ROOT)
    fixes.add_argument("--out", default=DEFAULT_FIXES_ROOT)
    args = parser.parse_args(argv)
    try:
        rules = load_rules(_resolved(args.rules))
    except (OSError, ValueError) as exc:
        print(f"hoh.rules: {exc}", file=sys.stderr)
        return 2
    tests_root, out = _resolved(args.tests_root), _resolved(args.out)
    failed = 0
    for app, tasks in rules.targets.items():
        for task in tasks:
            try:
                path, notes = write_fix(rules, tests_root, out, app, task)
            except Exception as exc:  # noqa: BLE001 - every test is tried; each failure is shown
                failed += 1
                print(f"{app}/{task}: NO FIX: {exc}")
                continue
            print(f"{app}/{task}: {path}")
            for note in notes:
                print(f"    note: {note}")
    print(f"{sum(len(t) for t in rules.targets.values()) - failed} fixes written, "
          f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
