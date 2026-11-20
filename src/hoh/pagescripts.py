"""Page scripts: the assembled change script and the rename/remove rules.

Three pieces run inside the application's pages:

- the benchmark's template, which applies one change once its condition holds and the
  page has settled. It refers to a settle helper it does not ship; ``prepare_template``
  inlines ours (``js/settle.js``) and renames every identifier that gives the study away;
- ``merge_bug``, which merges one change definition into the prepared template exactly the
  way the benchmark's browser start-up script does, but with a neutral kind;
- ``rule_script``, our own init script (``js/rules.js``) that renames one control's label
  (a harmless change) or removes the control (a real regression) for the life of the page. A
  rule finds its element by role (``ROLES``) and accessible name; a table cell (``cell``) is
  one of the roles C-assert uses for interface text that a test checks only as an expected
  value.

The template is read from the benchmark checkout at run time; nothing here copies it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from hoh.neutralize import IDENT_MAP, neutralize_identifiers, strip_comments

JS_DIR = Path(__file__).parent / "js"

SETTLE_PLACEHOLDER = "/* SETTLE_JS */ null"
RULES_PLACEHOLDER = "/* RULES */ []"
READY_PLACEHOLDER = "/* READY */ null"
_KIND_IDENT = "INJECTOR_KIND"  # the template's local name for the kind, and its placeholder
_KIND_NEUTRAL = "KIND"
KIND_PLACEHOLDER = "{{" + _KIND_NEUTRAL + "}}"
_IS_STUB = "const isConditionMet = () => {};"
_ON_STUB = "const onConditionMet = () => {};"

#: The session flag that switches the change script on, under its neutral name.
READY_KEY = IDENT_MAP["__flash_ui_active__"]

ROLES = ("link", "button", "tab", "menuitem", "heading", "cell")

_TELLTALE = re.compile(r"bug|inject|benign|scenario", re.I)
_KIND_OK = re.compile(r"[A-Za-z0-9_-]{1,32}")


def _norm(s: str) -> str:
    return " ".join(s.split())


def _check_neutral(text: str, what: str) -> None:
    for name in IDENT_MAP:
        if name in text:
            raise ValueError(f"{what} still contains the identifier {name!r}")
    m = _TELLTALE.search(text)
    if m:
        excerpt = text[max(0, m.start() - 30) : m.end() + 30]
        raise ValueError(f"{what} still contains {m.group()!r}: {excerpt!r}")


def settle_js() -> str:
    """The settle helper as one JavaScript expression (a function), comments removed."""
    expr = strip_comments((JS_DIR / "settle.js").read_text(encoding="utf-8")).strip()
    return expr.rstrip(";").strip()


_GUARD = "window[NAMESPACE] = { mutationObserver, conditionMet };"
_GUARD_NEUTRAL = "window[NAMESPACE] = { o: mutationObserver, s: conditionMet };"


def prepare_template(template: str) -> str:
    """Inline the settle helper and neutralize the template's identifiers and comments.

    The kind placeholder is renamed to ``{{KIND}}`` along with the local name it is
    assigned to, so that nothing in the result names the study; ``merge_bug`` fills it.
    """
    found = template.count(SETTLE_PLACEHOLDER)
    if found != 1:
        raise ValueError(f"expected one {SETTLE_PLACEHOLDER!r} in the template, found {found}")
    text = template.replace(SETTLE_PLACEHOLDER, f"({settle_js()})")
    text = strip_comments(text)
    text = neutralize_identifiers(text)
    text = re.sub(rf"\b{_KIND_IDENT}\b", _KIND_NEUTRAL, text)
    # The guard the template leaves on the page: its keys would describe a trigger and a
    # watcher to any test that reads it. Only its being there matters.
    if text.count(_GUARD) != 1:
        raise ValueError(f"expected one {_GUARD!r} in the template, found {text.count(_GUARD)}")
    text = text.replace(_GUARD, _GUARD_NEUTRAL)
    for stub in (KIND_PLACEHOLDER, _IS_STUB, _ON_STUB):
        if text.count(stub) != 1:
            raise ValueError(f"expected one {stub!r} in the template, found {text.count(stub)}")
    _check_neutral(text, "the prepared template")
    return text


def _block(bug_js: str, name: str) -> str:
    # The same cut as the benchmark's start-up script: the last marked block wins.
    blocks = re.findall(rf"// BEGIN {name}\s*(.*?)\s*// END {name}", bug_js, re.DOTALL)
    if not blocks:
        raise ValueError(f"the definition has no '// BEGIN {name}' ... '// END {name}' block")
    return neutralize_identifiers(strip_comments(blocks[-1])).strip()


def merge_bug(prepared_template: str, bug_js: str, kind: str = "st") -> str:
    """The full page script: one change definition merged into the prepared template."""
    if not _KIND_OK.fullmatch(kind) or _TELLTALE.search(kind):
        raise ValueError(f"kind must be a short neutral word, got {kind!r}")
    for stub in (KIND_PLACEHOLDER, _IS_STUB, _ON_STUB):
        if prepared_template.count(stub) != 1:
            raise ValueError("not a prepared template (run prepare_template first)")
    is_cond = _block(bug_js, "isConditionMet")
    on_cond = _block(bug_js, "onConditionMet")
    script = prepared_template.replace(_IS_STUB, is_cond)
    script = script.replace(_ON_STUB, on_cond)
    script = script.replace(KIND_PLACEHOLDER, kind)
    _check_neutral(script, "the merged script")
    return script


@dataclass(frozen=True)
class RuleSpec:
    """Rename a control's accessible name, or remove the control.

    ``exact`` matches like ``get_by_role(role, name=name, exact=True)``: the whole name,
    case-sensitive. Otherwise the name is a case-insensitive substring, like the default
    locator. Whitespace is collapsed either way.
    """

    op: Literal["rename", "remove"]
    role: str
    name: str
    new_name: str | None
    exact: bool = True


def _check_rule(rule: RuleSpec) -> None:
    if rule.op not in ("rename", "remove"):
        raise ValueError(f"unknown op {rule.op!r}")
    if rule.role not in ROLES:
        raise ValueError(f"unknown role {rule.role!r}; one of {', '.join(ROLES)}")
    if not _norm(rule.name):
        raise ValueError("a rule needs a name")
    if rule.op == "remove":
        if rule.new_name is not None:
            raise ValueError("a remove rule takes no new name")
        return
    if rule.new_name is None or not _norm(rule.new_name):
        raise ValueError("a rename rule needs a new name")
    if _norm(rule.name).lower() in _norm(rule.new_name).lower():
        # The default locator matches a case-insensitive substring: it would still find
        # the control under the old name.
        raise ValueError(f"new name {rule.new_name!r} contains the old one {rule.name!r}")


def rule_script(rules: Sequence[RuleSpec], ready_key: str | None = READY_KEY) -> str:
    """The init script that applies the rules in every page, from document start.

    In a document whose ``sessionStorage[ready_key]`` is not ``"true"`` the script does
    nothing at all, so a rule cannot interfere with the environment's login flow; the
    environment sets the key after login and reloads. ``ready_key=None``: always active.
    """
    if ready_key is not None and not _KIND_OK.fullmatch(ready_key):
        raise ValueError(f"ready_key must be a plain storage key, got {ready_key!r}")
    if not rules:
        raise ValueError("no rules")
    for rule in rules:
        _check_rule(rule)
    # A rename whose result another rule would match again chains or cycles.
    for a in rules:
        if a.op != "rename":
            continue
        for b in rules:
            if b.role == a.role and _norm(b.name).lower() in _norm(a.new_name or "").lower():
                raise ValueError(f"rule {b.name!r} would match the result of {a.name!r}")
    payload = [
        {
            "op": r.op,
            "role": r.role,
            "name": _norm(r.name),
            "newName": None if r.new_name is None else _norm(r.new_name),
            "exact": r.exact,
        }
        for r in rules
    ]
    template = (JS_DIR / "rules.js").read_text(encoding="utf-8")
    for placeholder in (RULES_PLACEHOLDER, READY_PLACEHOLDER):
        if template.count(placeholder) != 1:
            raise ValueError(f"rules.js must hold exactly one {placeholder!r}")
    script = template.replace(RULES_PLACEHOLDER, json.dumps(payload, ensure_ascii=True))
    script = script.replace(READY_PLACEHOLDER, json.dumps(ready_key))
    script = strip_comments(script)
    _check_neutral(script, "the rule script")
    return script
