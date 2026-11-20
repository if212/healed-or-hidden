"""Turn what pytest reported into the status of a run.

A run has one of four statuses:

- ``pass``: the test ran and passed.
- ``fail``: the test ran and failed in its body.
- ``not_executed``: the test was skipped, marked xfail, deselected, or is gone.
- ``broken``: the run could not be carried out (collection or syntax error,
  an interrupted or crashed pytest, an error outside the test body).

The rules, in order:

1. No report from the pytest process, a process that had to be stopped at the
   time limit, a collection error, or pytest exit code 2, 3 or 4: ``broken``.
2. No test collected: ``not_executed``, and ``deleted`` is true.
3. The target test was collected: its outcome decides. ``passed`` is ``pass``,
   ``failed`` is ``fail``, ``error`` is ``broken``; ``skipped``, ``xfailed`` and
   ``xpassed`` are ``not_executed`` (a test marked xfail cannot turn a build
   red, so it counts as not executed even when it passes). A target that is an
   ``async def`` function pytest cannot run fails without running its body:
   ``broken``.
4. The target was not collected but other tests were: any ``failed`` is
   ``fail``; else any ``error`` is ``broken``; else at least one ``passed`` is
   ``pass``; else ``not_executed``.
"""

from __future__ import annotations

import ast
from typing import Any

BROKEN_EXIT_CODES = (2, 3, 4)

_BY_OUTCOME = {
    "passed": "pass",
    "failed": "fail",
    "error": "broken",
    "skipped": "not_executed",
    "xfailed": "not_executed",
    "xpassed": "not_executed",
}


# What pytest says when it fails an ``async def`` test it has no plugin to run.
_ASYNC_UNSUPPORTED = "async def functions are not natively supported"


def _result(status: str, reason: str, target_found: bool, deleted: bool = False) -> dict[str, Any]:
    return {"status": status, "reason": reason, "target_found": target_found, "deleted": deleted}


def run_status(report: dict[str, Any] | None, target: str | None) -> dict[str, Any]:
    """Status of one run: ``status``, ``reason``, ``target_found`` and ``deleted``.

    ``report`` is what the plugin wrote at the end of the pytest session plus
    the ``exit_code`` of the pytest process. When that process left no report
    it is None, or a dict without ``tests`` (``timed_out`` says it was stopped
    at the time limit). ``target`` is the node id of the test the run is about;
    without one, every collected test stands in for it (rule 4).
    """
    report = report or {}
    tests = list(report.get("tests") or ())
    ran = {t["nodeid"]: t for t in tests}
    items = set(report.get("items") or ()) | set(ran)
    found = target is not None and target in items

    if report.get("timed_out"):
        # Stopped at the time limit: whatever it had reported, the run did not end.
        return _result("broken", "timed out", found)
    if "tests" not in report:
        if report.get("exit_code") in BROKEN_EXIT_CODES:
            return _result("broken", f"pytest exit code {report['exit_code']}", False)
        return _result("broken", "no report", False)

    if report.get("collect_errors", 0) > 0:
        return _result("broken", "collection error", found)
    exit_code = report.get("exit_code")
    if exit_code in BROKEN_EXIT_CODES:
        return _result("broken", f"pytest exit code {exit_code}", found)

    if not items:
        hidden = report.get("deselected", 0) + report.get("skipped_collectors", 0)
        if hidden:
            return _result("not_executed", "no test ran", False)
        return _result("not_executed", "no test collected", False, deleted=True)

    if found:
        if target not in ran:
            return _result("not_executed", "target did not run", True)
        outcome = ran[target]["outcome"]
        if outcome == "failed" and _ASYNC_UNSUPPORTED in str(ran[target].get("message") or ""):
            return _result("broken", "target cannot run: async def", True)
        return _result(_BY_OUTCOME.get(outcome, "broken"), f"target {outcome}", True)

    outcomes = {t["outcome"] for t in tests}
    prefix = "target not found" if target is not None else "no target named"
    if "failed" in outcomes:
        return _result("fail", f"{prefix}: a test failed", False)
    if outcomes - set(_BY_OUTCOME) or "error" in outcomes:
        return _result("broken", f"{prefix}: a test had an error", False)
    if "passed" in outcomes:
        return _result("pass", f"{prefix}: a test passed", False)
    return _result("not_executed", f"{prefix}: no test passed or failed", False)


def _expression(stmt: ast.stmt) -> ast.AST | None:
    """The expression a statement evaluates first, if it has one."""
    if isinstance(stmt, ast.Expr | ast.Assign | ast.AugAssign | ast.AnnAssign | ast.Return):
        return stmt.value
    if isinstance(stmt, ast.Assert | ast.If | ast.While):
        return stmt.test
    if isinstance(stmt, ast.With):
        return stmt.items[0].context_expr
    if isinstance(stmt, ast.For):
        return stmt.iter
    if isinstance(stmt, ast.Raise):
        return stmt.exc
    return None


def call_name(source: str, lineno: int) -> str | None:
    """Name of the outermost call made by the statement on line ``lineno`` (1-based).

    ``expect(page.locator("#a")).to_have_text("b")`` gives ``to_have_text``;
    ``assert page.locator("li").count() == 3`` gives ``count``. The statement is
    the smallest one that covers the line, so any line of a call that spans
    several lines gives the same answer. None when the line makes no call or
    the source cannot be parsed.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    best: ast.stmt | None = None
    best_span = 0
    for node in ast.walk(tree):  # breadth first: of two equal spans the inner one is kept
        if not isinstance(node, ast.stmt):
            continue
        end = node.end_lineno or node.lineno
        if node.lineno <= lineno <= end and (best is None or end - node.lineno <= best_span):
            best, best_span = node, end - node.lineno
    expression = _expression(best) if best is not None else None
    if expression is None:
        return None
    for node in ast.walk(expression):  # breadth first: the outermost call comes first
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                return node.func.attr
            if isinstance(node.func, ast.Name):
                return node.func.id
            return None
    return None
