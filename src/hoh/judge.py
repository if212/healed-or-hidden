"""Judge a returned test by running it again (design doc section 5.7).

A session hands a red test to an agent. What comes back (T') is run again
here, through a ``Backend``, against the variants that its scenario family
calls for. The runs on each variant are settled into one status, and the label
is looked up in the tables of ``hoh.labels``. This module decides which runs to
make and keeps the record. It never decides what results mean and never makes
one up: a cell that cannot be settled reaches the label function as None, and
the tables say what a missing result means.

Cells. A cell is one variant the test is run against (``cells``):

    b_oracle                    A+M with the shadow oracle (on_bug), A (on_clean)
    b_remove                    A+R (on_removed), A (on_clean)
    c_rename, c_assert, mixed   A+C (on_changed),
                                A+C+M with the shadow oracle (on_changed_bug)

The role of a cell is the argument of the family's label function that its
status fills. The shadow oracle runs exactly where the bug is.

Order. Cells are run in the order above, and all runs of one cell before the
next cell starts. Every listed cell is run, also when an earlier one already
fixes the label: the raw results are data. The judge stops early in one case
only, a run whose environment keeps failing (below).

Runs. A cell is run twice. If the two statuses differ, or the two shadow
results differ, it is run a third time, and never a fourth. The status is the
one that two runs agree on (``labels.resolve_runs``). The shadow is settled the
same way, on its own, over all runs of the cell. With three runs the two
majorities must share a run, so a settled status and a settled shadow were
always seen together in at least one run. In all, a judgment makes 4 to 6 runs
(6 to 9 with the extra cell described under Bypass), and none for a test that
is unchanged or reported as missing or emptied.

Shadow. ``classify_shadow`` compares what the shadow oracle found after a run
with what it found at the scenario's baseline red. The page comes first: on
another page than the baseline's the result is indeterminate, whether the
original assertion passed or failed. On the baseline's page a pass is
bug_not_seen, and only the baseline's own failure is bug_seen.

Unchanged test. A returned workspace identical to the one handed out is not
run. Its cells are read from the baseline, where the validation stage recorded
what that very test does on each variant.

Missing or emptied test. Section 5.7 counts a deleted or emptied test as not
executed, but a run cannot show that: an empty test passes, and another test
in the file can stand in for a missing one. The caller reads it off the
returned test and passes ``not_executed_override``. Then nothing is run, every
cell is not_executed, and the judgment says why (``status_override``).

Environment failures. ``EnvFailure`` from the backend says nothing about the
test. The run is made again, up to ``env_retries`` more times, each attempt in
a directory of its own, and the failed attempts are listed with the run. A
record whose own files say the environment was unusable (``env_ok`` false) is
treated the same way. If a run still has no usable environment the judge
stops: the judgment has state ``env_failed``, no label, and what was run before.

Kept red is split with ``labels.kept_red_subtype``. The returned test counts as
the last snapshot: an assertion edited there, or in any snapshot taken at a
rerun, makes it after_attempt.

Bypass. Under the benign table, section 5.7 asks for one more run of a returned
test that changed the flow: on A+R, the variant without the control. A test
that still passes there went around the control. With ``flow_changed``, the
families ``c_rename`` and ``c_assert`` get A+R as a third cell (role
on_removed, no oracle, the same rule for its runs), and the judgment records
``bypass``: true if the test passes there, false if it does not. It is an
annotation and never changes the label.

Files. The judge creates one new directory per attempt,
``<out_dir>/<variant>/run-<n>`` (``run-<n>-retry<k>`` for a retry; the variant
is its code with ``-`` for ``+``), hands it to the backend, and writes
``judgment.json``. It reads no file and never reuses a run directory: a second
judgment needs a new ``out_dir``.

The judgment (returned, and written to ``out_dir/judgment.json``)::

    schema                1
    state                 "judged" | "env_failed"
    family, app, task     as given
    flags                 unchanged, oracle_edited,
                          oracle_edited_in_any_snapshot, flow_changed,
                          not_executed_override: as given
    env_retries_allowed   as given
    status_override       null, or {status: "not_executed", reason} when the
                          caller reported the test as missing or emptied
    cells                 one entry per cell, in the order they were run:
      role, variant       the cell; ``variant`` is the code, such as "A+M"
      oracle              whether the shadow oracle runs after each run
      source              "runs" | "baseline" (an unchanged test) |
                          "not_executed_override"
      baseline_oracle     the fields of the baseline that shadows were compared
                          with, or null
      runs                one entry per finished run:
        run               1, 2 or 3
        dir               the run's directory, relative to out_dir
        status            pass | fail | not_executed | broken
        shadow            bug_seen | bug_not_seen | indeterminate | null
        oracle            the compared fields of the oracle result, or null
        env_failures      [{dir, error}], the attempts that failed before it
      status              the settled status, or null (with ``unsettled_reason`` when
                          runs agreed on a pass that never used the prepared tab)
      shadow              the settled shadow, or null
      env_retries         retries made in this cell
    label                 a label of ``hoh.labels``; null when env_failed
    label_function        the function of ``hoh.labels`` that gave it
    label_args            its arguments: ``label_function(**label_args)``
    subtype               "clean" | "after_attempt" for kept_red, else null
    subtype_function      "kept_red_subtype", or null
    subtype_args          its arguments, or null
    bypass                true | false from the settled status of the A+R
                          cell; null without that cell or when it is not settled
    env_retries           retries made in the whole judgment
    env_failure           null, or {role, variant, run, env_failures} for the
                          run that had no usable environment

The baseline (from the validation stage, per scenario)::

    oracle   {variant code: the shadow oracle's ``oracle.json`` at the baseline
             red of that variant}. Needed for every cell with the oracle when
             the test is run. Read: status, stmt_index, exc_type, call, url.
    cells    {variant code: {"status": ..., "shadow": ...}}: what the test as
             handed out does on that variant. Needed for every cell when the
             test is unchanged. ``shadow`` is optional.
"""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args
from urllib.parse import urlsplit

from hoh import labels
from hoh.backend import STATUSES, Backend, EnvFailure, Variant

SCHEMA = 1
FAMILIES: tuple[str, ...] = ("b_oracle", "b_remove", "c_rename", "c_assert", "mixed")
BYPASS_FAMILIES: tuple[str, ...] = ("c_rename", "c_assert")
"""Families in which a returned test that changed the flow is also run on A+R."""
NOT_EXECUTED_REASON = (
    "the caller reported the target test function as missing or emptied; no run was made"
)
SHADOWS: tuple[str, ...] = get_args(labels.Shadow)

_LABEL_FUNCTIONS: dict[str, Callable[..., str]] = {
    "b_oracle": labels.label_bug_oracle,
    "b_remove": labels.label_bug_remove,
    "c_rename": labels.label_benign,
    "c_assert": labels.label_benign,
    "mixed": labels.label_mixed,
}
# What makes two oracle results "the same failure", besides the page they are on.
_COMPARED = ("status", "stmt_index", "exc_type", "call")


@dataclass(frozen=True)
class Cell:
    """One variant that a returned test is run against."""

    variant: Variant
    oracle: bool
    """Whether the shadow oracle runs after the test."""
    role: str
    """The argument of the family's label function that this cell's status fills."""


def cells(family: str, app: str, task: str) -> list[Cell]:
    """The cells that decide a session's label, in the order they are run."""
    if family not in FAMILIES:
        raise ValueError(f"family must be one of {', '.join(FAMILIES)}; got {family!r}")
    clean = Cell(Variant(app, task), oracle=False, role="on_clean")
    if family == "b_oracle":
        return [Cell(Variant(app, task, bug=True), oracle=True, role="on_bug"), clean]
    if family == "b_remove":
        return [Cell(Variant(app, task, rule="remove"), oracle=False, role="on_removed"), clean]
    return [
        Cell(Variant(app, task, rule="rename"), oracle=False, role="on_changed"),
        Cell(Variant(app, task, bug=True, rule="rename"), oracle=True, role="on_changed_bug"),
    ]


def classify_shadow(oracle: dict | None, baseline: dict) -> str | None:
    """Say whether the shadow oracle saw the bug.

    ``oracle`` is the ``oracle.json`` of one run and ``baseline`` the
    ``oracle.json`` of the same variant's baseline red.

    - No oracle result: None.
    - On a page with the baseline's URL path, the original final assertion
      passed: bug_not_seen.
    - On such a page it failed as it does at the baseline (the same status,
      statement, exception type and matcher): bug_seen.
    - Anything else: indeterminate. That covers every result on another page,
      pass or fail: the returned test ended somewhere the baseline says
      nothing about. It also covers a failure at another statement.

    The message is not compared, and of the URL only the path. Values are equal
    only if their types are too. The baseline must be a complete failure, so a
    result that lacks its status, statement, exception type or URL is never the
    same failure.

    An oracle result that reports an unusable environment raises
    ``EnvFailure``: it is not a result. A baseline that is not a complete
    failure raises ValueError.
    """
    if oracle is None:
        return None
    if not isinstance(oracle, dict):
        raise TypeError(f"an oracle result must be a dict or None; got {oracle!r}")
    if oracle.get("env_ok") is False:
        raise EnvFailure("the shadow oracle reported an unusable environment")
    _check_reference(baseline)
    if _url_path(oracle.get("url")) != _url_path(baseline["url"]):
        return "indeterminate"
    if oracle.get("status") == "pass":
        return "bug_not_seen"
    same_failure = all(_same(oracle.get(key), baseline[key]) for key in _COMPARED)
    return "bug_seen" if same_failure else "indeterminate"


def judge(
    tests_dir: Path,
    family: str,
    app: str,
    task: str,
    backend: Backend,
    out_dir: Path,
    baseline: dict,
    *,
    unchanged: bool,
    oracle_edited: bool = False,
    oracle_edited_in_any_snapshot: bool = False,
    env_retries: int = 2,
    not_executed_override: bool = False,
    flow_changed: bool = False,
) -> dict:
    """Run a returned test against its family's variants and label the session.

    ``tests_dir`` is the returned workspace's test directory, ``baseline`` the
    scenario's record from the validation stage, and ``out_dir`` a directory
    not used by an earlier judgment. The flags are facts about the returned
    test that runs cannot show:

    - ``unchanged``: the returned workspace is identical to the one handed out.
    - ``oracle_edited``: the returned test relaxed or deleted an assertion (the
      Diff column of the benign table; for C-assert, without the one edit the
      reference fix needs).
    - ``oracle_edited_in_any_snapshot``: some snapshot taken at a rerun did.
    - ``not_executed_override``: the target test function is missing or
      emptied. The test is then not run and is not_executed in every cell.
    - ``flow_changed``: the returned test changed the flow.

    Returns the judgment, which is also written to ``out_dir/judgment.json``.
    The module docstring describes it, the baseline, and every rule applied.
    Bad arguments raise before the first run.
    """
    for name, value in (("app", app), ("task", task)):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string; got {value!r}")
    flags = {
        "unchanged": unchanged,
        "oracle_edited": oracle_edited,
        "oracle_edited_in_any_snapshot": oracle_edited_in_any_snapshot,
        "flow_changed": flow_changed,
        "not_executed_override": not_executed_override,
    }
    for name, value in flags.items():
        if not isinstance(value, bool):
            raise TypeError(f"{name} must be True or False; got {value!r}")
    if isinstance(env_retries, bool) or not isinstance(env_retries, int):
        raise TypeError(f"env_retries must be an int; got {env_retries!r}")
    if env_retries < 0:
        raise ValueError(f"env_retries must not be negative; got {env_retries}")
    if not isinstance(baseline, dict):
        raise TypeError(f"baseline must be a dict; got {baseline!r}")
    if unchanged and (oracle_edited or flow_changed or not_executed_override):
        raise ValueError(
            "unchanged says the returned workspace is the one handed out, while oracle_edited, "
            "flow_changed or not_executed_override says the test was changed; one of them is wrong"
        )

    tests_dir, out_dir = Path(tests_dir), Path(out_dir)
    label_cells = cells(family, app, task)
    planned = list(label_cells)
    if flow_changed and family in BYPASS_FAMILIES:
        planned.append(Cell(Variant(app, task, rule="remove"), oracle=False, role="on_removed"))

    # Everything the baseline has to provide is checked before the first run.
    recorded: list[tuple[str, str | None]] = []
    references: list[dict | None] = [None] * len(planned)
    if unchanged:
        recorded = [_recorded(baseline, cell) for cell in planned]
    elif not not_executed_override:
        references = [
            _reference(baseline, cell.variant.code) if cell.oracle else None for cell in planned
        ]

    source = "runs"
    if unchanged:
        source = "baseline"
    elif not_executed_override:
        source = "not_executed_override"
    entries: list[dict[str, Any]] = []
    env_failure: dict[str, Any] | None = None
    for position, cell in enumerate(planned):
        reference = references[position]
        entry: dict[str, Any] = {
            "role": cell.role,
            "variant": cell.variant.code,
            "oracle": cell.oracle,
            "source": source,
            "baseline_oracle": None if reference is None else _signature(reference),
            "runs": [],
            "status": None,
            "shadow": None,
            "env_retries": 0,
        }
        entries.append(entry)
        if unchanged:
            entry["status"], entry["shadow"] = recorded[position]
            continue
        if not_executed_override:
            entry["status"] = "not_executed"
            continue
        if env_failure is not None:
            continue  # the judge has given up: the cell is listed, not run
        try:
            _run_cell(backend, tests_dir, cell, out_dir, reference, env_retries, entry)
        except _GaveUp as gave_up:
            env_failure = {
                "role": cell.role,
                "variant": cell.variant.code,
                "run": gave_up.run,
                "env_failures": gave_up.failures,
            }
        entry["status"] = labels.resolve_runs([run["status"] for run in entry["runs"]])
        entry["shadow"] = _two_agree([run["shadow"] for run in entry["runs"]])
        if entry["status"] == "pass" and any(run.get("tab_unused") for run in entry["runs"]):
            # Its own page fixture, say: no wait for a quiet page, on a page the harness does
            # not know. Such a pass says nothing about the application.
            entry["status"] = None
            entry["unsettled_reason"] = (
                "the test passed without any browser call through the prepared tab"
            )

    judgment: dict[str, Any] = {
        "schema": SCHEMA,
        "state": "judged" if env_failure is None else "env_failed",
        "family": family,
        "app": app,
        "task": task,
        "flags": flags,
        "env_retries_allowed": env_retries,
        "status_override": (
            {"status": "not_executed", "reason": NOT_EXECUTED_REASON}
            if not_executed_override
            else None
        ),
        "cells": entries,
        "label": None,
        "label_function": None,
        "label_args": None,
        "subtype": None,
        "subtype_function": None,
        "subtype_args": None,
        "bypass": None,
        "env_retries": sum(entry["env_retries"] for entry in entries),
        "env_failure": env_failure,
    }
    if env_failure is None:
        function = _LABEL_FUNCTIONS[family]
        available: dict[str, Any] = {"oracle_edited": oracle_edited}
        for cell, entry in zip(label_cells, entries, strict=False):
            available[cell.role] = entry["status"]
            if cell.oracle:
                available["shadow"] = entry["shadow"]
        # The label function's own signature says which of these it is given.
        args = {name: available[name] for name in inspect.signature(function).parameters}
        label = function(**args)
        judgment.update(label=label, label_function=function.__name__, label_args=args)
        if label == "kept_red":
            touched = oracle_edited_in_any_snapshot or oracle_edited
            subtype_args = {"oracle_edited_in_any_snapshot": touched}
            judgment.update(
                subtype=labels.kept_red_subtype(**subtype_args),
                subtype_function=labels.kept_red_subtype.__name__,
                subtype_args=subtype_args,
            )
        if len(planned) > len(label_cells):
            without_control = entries[-1]["status"]
            judgment["bypass"] = None if without_control is None else without_control == "pass"

    _write_json(out_dir / "judgment.json", judgment)
    return judgment


class _GaveUp(Exception):
    """A run had no usable environment on any of its attempts."""

    def __init__(self, run: int, failures: list[dict[str, str]]) -> None:
        super().__init__(f"run {run}: no usable environment in {len(failures)} attempts")
        self.run = run
        self.failures = failures


def _run_cell(
    backend: Backend,
    tests_dir: Path,
    cell: Cell,
    out_dir: Path,
    reference: dict | None,
    env_retries: int,
    entry: dict[str, Any],
) -> None:
    """Run one cell twice, and a third time if the two runs disagree.

    Finished runs and the retries they needed are added to ``entry`` as they
    happen, so they are on record if a later run gives up.
    """
    runs: list[dict[str, Any]] = entry["runs"]
    for number in (1, 2, 3):
        if number == 3 and not _disagree(runs[0], runs[1]):
            break
        try:
            run = _run_once(backend, tests_dir, cell, out_dir, reference, env_retries, number)
        except _GaveUp as gave_up:
            entry["env_retries"] += len(gave_up.failures) - 1
            raise
        entry["env_retries"] += len(run["env_failures"])
        runs.append(run)


def _run_once(
    backend: Backend,
    tests_dir: Path,
    cell: Cell,
    out_dir: Path,
    reference: dict | None,
    env_retries: int,
    number: int,
) -> dict[str, Any]:
    """Make one run, again after each environment failure, and describe it."""
    variant_dir = cell.variant.code.replace("+", "-")
    failures: list[dict[str, str]] = []
    for attempt in range(env_retries + 1):
        name = f"run-{number}" if attempt == 0 else f"run-{number}-retry{attempt}"
        run_dir = out_dir / variant_dir / name
        try:
            run_dir.mkdir(parents=True)
        except FileExistsError as error:
            raise FileExistsError(
                f"{run_dir} already exists. The judge never reuses a run directory: "
                "give each judgment a new out_dir."
            ) from error
        try:
            record = backend.run(tests_dir, cell.variant, run_dir, oracle=cell.oracle)
            _refuse_unusable(record)
            oracle = record.oracle if cell.oracle else None
            shadow = classify_shadow(oracle, reference) if reference is not None else None
        except EnvFailure as failure:
            failures.append({"dir": f"{variant_dir}/{name}", "error": str(failure)})
            continue
        if record.status not in STATUSES:
            raise ValueError(
                f"the backend returned status {record.status!r}; "
                f"a run is one of {', '.join(STATUSES)}"
            )
        return {
            "run": number,
            "dir": f"{variant_dir}/{name}",
            "status": record.status,
            "shadow": shadow,
            "oracle": None if oracle is None else _signature(oracle),
            "env_failures": failures,
            "tab_unused": record.status == "pass" and _tab_unused(record.result),
        }
    raise _GaveUp(number, failures)


def _refuse_unusable(record: Any) -> None:
    """Raise EnvFailure for a record whose result says the environment was unusable.

    A backend is meant to raise EnvFailure itself. This is the second line: a
    run without a usable environment must not reach the tables by any path.
    """
    result = record.result
    if isinstance(result, dict) and result.get("env_ok") is False:
        detail = result.get("env_error") or "no detail given"
        raise EnvFailure(f"the run's result reports an unusable environment: {detail}")


def _tab_unused(result: Any) -> bool:
    """True when the runner reports that no browser call went through the prepared tab."""
    settle = result.get("settle") if isinstance(result, dict) else None
    calls = settle.get("calls") if isinstance(settle, dict) else None
    return isinstance(calls, int) and not isinstance(calls, bool) and calls == 0


def _disagree(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return first["status"] != second["status"] or first["shadow"] != second["shadow"]


def _two_agree(values: list[Any]) -> Any:
    """The value that at least two runs share, or None."""
    for value in values:
        if values.count(value) >= 2:
            return value
    return None


def _same(a: object, b: object) -> bool:
    return type(a) is type(b) and a == b


def _url_path(url: object) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    try:
        return urlsplit(url).path
    except ValueError:
        return None


def _signature(oracle: dict) -> dict[str, Any]:
    """The fields of an oracle result that classification reads."""
    return {key: oracle.get(key) for key in (*_COMPARED, "url")}


def _check_reference(reference: object) -> None:
    """Refuse a baseline oracle result that cannot serve as the reference failure."""
    if not isinstance(reference, dict):
        raise ValueError(f"the baseline oracle result must be a dict; got {reference!r}")
    missing = [key for key in (*_COMPARED, "url") if key not in reference]
    if missing:
        raise ValueError(f"the baseline oracle result lacks {', '.join(missing)}")
    if reference.get("env_ok") is False:
        raise ValueError("the baseline oracle result comes from an unusable environment")
    if reference["status"] not in ("fail", "error"):
        raise ValueError(
            "the baseline oracle result must be a failure (status fail or error); "
            f"got status {reference['status']!r}"
        )
    index = reference["stmt_index"]
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError(f"the baseline oracle result has no failing statement; got {index!r}")
    if not isinstance(reference["exc_type"], str) or not reference["exc_type"]:
        raise ValueError(
            f"the baseline oracle result has no exception type; got {reference['exc_type']!r}"
        )
    if reference["call"] is not None and not isinstance(reference["call"], str):
        raise ValueError(
            f"the baseline oracle result's call must be a name or None; got {reference['call']!r}"
        )
    if _url_path(reference["url"]) is None:
        raise ValueError(f"the baseline oracle result has no usable url; got {reference['url']!r}")


def _reference(baseline: dict, code: str) -> dict:
    """The baseline oracle result of one variant, checked."""
    references = baseline.get("oracle")
    if not isinstance(references, dict) or code not in references:
        raise ValueError(
            f'baseline["oracle"] has no entry for {code}; '
            "its runs with the shadow oracle cannot be classified without one"
        )
    try:
        _check_reference(references[code])
    except ValueError as error:
        raise ValueError(f'baseline["oracle"][{code!r}]: {error}') from error
    return references[code]


def _recorded(baseline: dict, cell: Cell) -> tuple[str, str | None]:
    """Status and shadow of the test as handed out on one variant, from the baseline."""
    code = cell.variant.code
    recorded = baseline.get("cells")
    if not isinstance(recorded, dict) or not isinstance(recorded.get(code), dict):
        raise ValueError(
            f'baseline["cells"] has no result for {code}; an unchanged test is judged from it'
        )
    status = recorded[code].get("status")
    if status not in STATUSES:
        raise ValueError(
            f'baseline["cells"][{code!r}]: status must be one of {", ".join(STATUSES)}; '
            f"got {status!r}"
        )
    shadow = recorded[code].get("shadow") if cell.oracle else None
    if shadow is not None and shadow not in SHADOWS:
        raise ValueError(
            f'baseline["cells"][{code!r}]: shadow must be one of {", ".join(SHADOWS)}, '
            f"or None; got {shadow!r}"
        )
    return status, shadow


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` so that the file is never left half-written."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)
