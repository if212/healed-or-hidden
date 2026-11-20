"""Port a generated Playwright-for-Python test to a Playwright Test spec in TypeScript.

The fidelity check runs Playwright's own test healer on TypeScript ports of some
generated tests and reference fixes. A port has to do what the Python test does,
statement for statement, so that it fails at the same statement on the same app.
So the converter maps exactly the constructs those files use, each to TypeScript
with the same meaning, and refuses everything else with ``PortRefused``, whose
message names the construct and its line. It reads the Python AST and never runs
the source.

The module must be ``from playwright.sync_api import Page, expect`` (``import re``
may come before or after it) and one ``def test_<name>(page: Page) -> None:``. The
port is one ``test(...)`` whose callback takes the fixture package's ``page``, the
one open tab; ``test``, ``expect`` and, when ``re.escape`` is used, ``reEscape``
come from ``pwfix``.

Every Python statement of the test function becomes one TypeScript statement that
starts on its own line: a simple statement is one line, a ``for`` or a nested
``def`` is a header line, its body and a closing brace. ``Port.lines`` maps the
line of each Python statement to the line of the statement it became.

How things map:

- Locator building (``get_by_*``, ``locator``, ``filter``, ``nth``, ``first``,
  ``or_``, ``content_frame``) is synchronous in both languages. Every call that
  talks to the browser, assertions included, is awaited, and stands only where a
  statement's value is: alone, as an item of a list that is the statement, or as
  the item of a list comprehension that is the statement (a one-line loop).
- Keyword arguments become one trailing options object with camelCase keys. Each
  method takes only the keywords listed for it, so a Python parameter that
  TypeScript takes by position never ends up in the options object.
- A regular expression goes to the browser as its source text in both languages
  and is read there as JavaScript, so a pattern keeps its source. A literal
  pattern becomes a regex literal and must use only syntax, from a small
  allow-list, that Python compiles and JavaScript reads the same way. A pattern
  joined from literals and ``re.escape`` becomes ``new RegExp(...)`` with
  ``reEscape``, which has to escape exactly what Python's ``re.escape`` escapes.
- ``page.evaluate`` keeps its string. Python lets the browser call the value
  when it is a function and TypeScript evaluates a string as an expression only,
  so only ``window.scrollTo(...)`` and ``window.scrollBy(...)``, which return
  nothing, are taken.
- A name is bound once in the whole test function and becomes a ``const`` (a
  ``range`` loop counter a ``let``); a name JavaScript reserves gets a trailing
  underscore. A lambda becomes an arrow function and a nested ``def`` an
  ``async function`` whose calls are awaited.

Where the meaning depends on a value's type (an f-string, ``str()``, ``+``,
``re.escape``, a loop over a sequence, unpacking), a small static typing pass
decides: a literal has its type, a name the type of what it was bound to, and a
parameter of a lambda or nested ``def`` the type of the arguments at its call
sites, read where it is defined. Where the type is not known, the converter
refuses.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from hoh.fidelity.static import FORBIDDEN_CALLS, FORBIDDEN_NAMES

# The import, a blank line and the test(...) line come before the first statement.
HEADER_LINES = 3
MAX_SAFE_INT = 2**53 - 1


class PortRefused(ValueError):
    """The source uses something the converter does not handle."""


@dataclass(frozen=True)
class Port:
    """A TypeScript port and where each Python statement of the test went.

    ``lines`` maps the 1-based line of each Python statement inside the test
    function, statements in loop and function bodies included, to the 1-based line
    of the TypeScript statement it became.
    """

    text: str
    lines: dict[int, int]


# Static types, worded for messages. A list is ("list", item type) and a tuple
# ("tuple", item types).
PAGE = "the page"
LOCATOR = "a locator"
FRAME = "a frame locator"
STR = "a str"
INT = "an int"
FLOAT = "a float"
BOOL = "a bool"
PATTERN = "a pattern"
UNKNOWN = "a value of unknown type"


@dataclass(frozen=True)
class _Method:
    ts: str
    receivers: frozenset[str]
    positional: int
    options: frozenset[str] = frozenset()
    # What a locator-building method returns; None for a call that talks to the browser,
    # which is awaited.
    returns: str | None = None


_ANYWHERE = frozenset({PAGE, LOCATOR, FRAME})
_ON_LOCATOR = frozenset({LOCATOR})
_ON_PAGE = frozenset({PAGE})
_HAS = frozenset({"has", "has_not", "has_text", "has_not_text"})
_TIMEOUT = frozenset({"timeout"})

_METHODS = {
    "locator": _Method("locator", _ANYWHERE, 1, _HAS, LOCATOR),
    "get_by_role": _Method(
        "getByRole", _ANYWHERE, 1, frozenset({"name", "exact", "level"}), LOCATOR
    ),
    "get_by_text": _Method("getByText", _ANYWHERE, 1, frozenset({"exact"}), LOCATOR),
    "get_by_label": _Method("getByLabel", _ANYWHERE, 1, frozenset({"exact"}), LOCATOR),
    "filter": _Method("filter", _ON_LOCATOR, 0, _HAS | {"visible"}, LOCATOR),
    "nth": _Method("nth", _ON_LOCATOR, 1, returns=LOCATOR),
    "or_": _Method("or", _ON_LOCATOR, 1, returns=LOCATOR),
    "click": _Method("click", _ON_LOCATOR, 0, _TIMEOUT),
    "fill": _Method("fill", _ON_LOCATOR, 1, _TIMEOUT),
    "check": _Method("check", _ON_LOCATOR, 0, _TIMEOUT),
    "press": _Method("press", _ON_LOCATOR, 1, _TIMEOUT),
    "evaluate": _Method("evaluate", _ON_PAGE, 1),
    # TypeScript takes the load state by position before its options: no options here.
    "wait_for_load_state": _Method("waitForLoadState", _ON_PAGE, 0),
}

# Python properties that are methods in TypeScript: name, receiver, result.
_PROPERTIES = {
    "first": ("first", LOCATOR, LOCATOR),
    "content_frame": ("contentFrame", LOCATOR, FRAME),
}

# Locator assertions, each with a not_ form: TypeScript name and positional arguments.
# Playwright Test has each with the same arguments and the same meaning.
_ASSERTIONS = {
    "to_be_visible": ("toBeVisible", 0),
    "to_be_checked": ("toBeChecked", 0),
    "to_be_enabled": ("toBeEnabled", 0),
    "to_contain_text": ("toContainText", 1),
    "to_have_text": ("toHaveText", 1),
    "to_have_count": ("toHaveCount", 1),
    "to_have_value": ("toHaveValue", 1),
    "to_have_attribute": ("toHaveAttribute", 2),
    "to_match_aria_snapshot": ("toMatchAriaSnapshot", 1),
}

# Strings page.evaluate may take: they evaluate to undefined, so Python's call-if-a-function
# and TypeScript's evaluate-as-an-expression do the same.
_EVALUATE = re.compile(r"window\.scroll(?:To|By)\([^()]*\)")

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_JS_RESERVED = frozenset(
    "arguments await break case catch class const continue debugger default delete do else "
    "enum eval export extends false finally for function if implements import in instanceof "
    "interface let new null package private protected public return static super switch this "
    "throw true try typeof var void while with yield".split()
)
_BUILTINS = frozenset({"expect", "re", "str", "range", "enumerate"})
# Names a binding must not take: those the port relies on in Python or in TypeScript, and
# those check_spec refuses as code.
_PROTECTED = (
    _BUILTINS
    | {"page", "test", "reEscape", "String", "Array", "RegExp", "require"}
    | FORBIDDEN_NAMES
    | FORBIDDEN_CALLS
)

_STATEMENTS = {
    ast.If: "an if statement",
    ast.Assert: "an assert statement",
    ast.While: "a while loop",
    ast.With: "a with statement",
    ast.AsyncWith: "an async with statement",
    ast.Try: "a try statement",
    ast.TryStar: "a try statement",
    ast.Return: "a return statement",
    ast.Pass: "a pass statement",
    ast.AnnAssign: "an annotated assignment",
    ast.AugAssign: "an augmented assignment",
    ast.Break: "a break statement",
    ast.Continue: "a continue statement",
    ast.Import: "an import",
    ast.ImportFrom: "an import",
    ast.ClassDef: "a class definition",
    ast.AsyncFunctionDef: "an async function",
    ast.AsyncFor: "an async for loop",
    ast.Delete: "a del statement",
    ast.Raise: "a raise statement",
    ast.Global: "a global statement",
    ast.Nonlocal: "a nonlocal statement",
    ast.Match: "a match statement",
    ast.TypeAlias: "a type alias",
}

_EXPRESSIONS = {
    ast.Dict: "a dict",
    ast.Set: "a set",
    ast.Subscript: "a subscript",
    ast.Slice: "a slice",
    ast.Compare: "a comparison",
    ast.BoolOp: "and/or",
    ast.UnaryOp: "a unary operator",
    ast.IfExp: "a conditional expression",
    ast.Lambda: "a lambda that is not assigned to a name",
    ast.ListComp: "a list comprehension inside an expression",
    ast.SetComp: "a set comprehension",
    ast.DictComp: "a dict comprehension",
    ast.GeneratorExp: "a generator expression",
    ast.Await: "await",
    ast.Yield: "yield",
    ast.YieldFrom: "yield",
    ast.NamedExpr: "an assignment expression",
    ast.Starred: "a starred argument",
}

_MODULE_LEVEL = {
    ast.Import: "import",
    ast.ImportFrom: "import",
    ast.FunctionDef: "function",
    ast.AsyncFunctionDef: "async function",
    ast.ClassDef: "class",
    ast.Assign: "assignment",
    ast.Expr: "expression",
}

# Precedence of a TypeScript expression, as far as the port needs it.
_PRIMARY, _AWAIT, _ADD = 3, 2, 1


@dataclass(frozen=True)
class _Expr:
    text: str
    type: Any
    prec: int = _PRIMARY


@dataclass(frozen=True)
class _Binding:
    kind: str  # "value", "lambda" or "function"
    js: str
    type: Any = UNKNOWN  # of the value, or of what a lambda returns


class _Scope:
    """The names bound in one TypeScript block; the enclosing blocks are behind it."""

    def __init__(self, parent: _Scope | None = None) -> None:
        self.parent = parent
        self.names: dict[str, _Binding] = {}

    def lookup(self, name: str) -> _Binding | None:
        scope: _Scope | None = self
        while scope is not None:
            if name in scope.names:
                return scope.names[name]
            scope = scope.parent
        return None


def _refuse(node: ast.AST, what: str) -> NoReturn:
    raise PortRefused(f"line {node.lineno}: {what} is not handled")


def port_source(source: str) -> Port:
    """Port the source of one generated test module, or raise PortRefused."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise PortRefused(f"line {exc.lineno or 1}: the source does not parse") from None
    except ValueError:
        raise PortRefused("line 1: the source does not parse") from None
    function, imports_re = _test_function(tree)
    converter = _Converter(function, imports_re)
    body = converter.convert(function)
    names = "test, expect, reEscape" if converter.uses_re_escape else "test, expect"
    lines = [
        f"import {{ {names} }} from {js_string('pwfix')};",
        "",
        f"test({js_string(function.name)}, async ({{ page }}) => {{",
        *body,
        "});",
    ]
    return Port("\n".join(lines) + "\n", dict(converter.lines))


def _test_function(tree: ast.Module) -> tuple[ast.FunctionDef, bool]:
    """The test function, and whether the module imports re; any other shape is refused."""
    playwright = imports_re = False
    function: ast.FunctionDef | None = None
    for node in tree.body:
        if function is None and not playwright and _is_playwright_import(node):
            playwright = True
        elif function is None and not imports_re and _is_re_import(node):
            imports_re = True
        elif function is None and playwright and isinstance(node, ast.FunctionDef):
            function = node
        else:
            kind = _MODULE_LEVEL.get(type(node), type(node).__name__)
            _refuse(node, f"module-level {kind}")
    if function is None:
        raise PortRefused("line 1: a module without a test function is not handled")
    args = function.args
    page = args.args[0] if len(args.args) == 1 else None
    if (
        not re.fullmatch(r"test_[A-Za-z0-9_]+", function.name)
        or function.decorator_list
        or function.type_params
        or args.posonlyargs
        or args.vararg
        or args.kwonlyargs
        or args.kwarg
        or args.defaults
        or page is None
        or page.arg != "page"
        or not (isinstance(page.annotation, ast.Name) and page.annotation.id == "Page")
        or not (isinstance(function.returns, ast.Constant) and function.returns.value is None)
    ):
        _refuse(function, "a test function other than def test_<name>(page: Page) -> None")
    return function, imports_re


def _is_playwright_import(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.ImportFrom)
        and node.module == "playwright.sync_api"
        and node.level == 0
        and sorted((alias.name, alias.asname) for alias in node.names)
        == [("Page", None), ("expect", None)]
    )


def _is_re_import(node: ast.stmt) -> bool:
    return isinstance(node, ast.Import) and [(a.name, a.asname) for a in node.names] == [
        ("re", None)
    ]


class _Converter:
    """Converts the body of one test function, statement by statement."""

    def __init__(self, function: ast.FunctionDef, imports_re: bool) -> None:
        self.imports_re = imports_re
        self.out: list[str] = []
        self.lines: dict[int, int] = {}
        self.uses_re_escape = False
        self.js: dict[str, str] = {}  # Python name -> TypeScript name
        self.local_functions: set[str] = set()
        self.calls: dict[str, list[ast.Call]] = {}
        self._prepare(function)

    def convert(self, function: ast.FunctionDef) -> list[str]:
        scope = _Scope()
        scope.names["page"] = _Binding("value", "page", PAGE)
        self._block(function.body, scope, 1, function.lineno)
        return self.out

    # Names

    def _prepare(self, function: ast.FunctionDef) -> None:
        """Check every name the test binds, and find the calls of its local functions.

        Each name is bound once in the whole function, so a name always means one
        binding, wherever Python and TypeScript would scope it.
        """
        body = ast.Module(body=function.body, type_ignores=[])
        sites: list[tuple[int, int, str, ast.AST]] = []
        for node in ast.walk(body):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                sites.append((node.lineno, node.col_offset, node.id, node))
            elif isinstance(node, ast.arg):
                sites.append((node.lineno, node.col_offset, node.arg, node))
            elif isinstance(node, ast.FunctionDef):
                sites.append((node.lineno, node.col_offset, node.name, node))
                self.local_functions.add(node.name)
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Lambda):
                self.local_functions.update(t.id for t in node.targets if isinstance(t, ast.Name))
        written: dict[str, str] = {}
        for _, _, name, node in sorted(sites, key=lambda site: site[:2]):
            if name in self.js:
                _refuse(node, f"binding `{name}` a second time")
            if not _IDENTIFIER.fullmatch(name):
                _refuse(node, f"the name `{name}`")
            if name in _PROTECTED:
                _refuse(node, f"binding `{name}`")
            js = name + "_" if name in _JS_RESERVED else name
            if js in written:
                _refuse(node, f"`{name}` and `{written[js]}` both written {js}")
            written[js] = name
            self.js[name] = js

        callees: set[int] = set()
        for node in _walk_in_order(body):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in self.local_functions:
                    self.calls.setdefault(node.func.id, []).append(node)
                    callees.add(id(node.func))
        for node in _walk_in_order(body):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in self.local_functions
                and id(node) not in callees
            ):
                _refuse(node, f"using `{node.id}` other than by calling it")

    def _bind(self, target: ast.Name, kind: Any, scope: _Scope) -> str:
        js = self.js[target.id]
        scope.names[target.id] = _Binding("value", js, kind)
        return js

    def _parameter_types(self, name: str, count: int, scope: _Scope) -> list[Any]:
        """The type of each parameter of a local function: what its call sites pass."""
        types: list[Any] = [None] * count
        for call in self.calls.get(name, []):
            if call.keywords or any(isinstance(arg, ast.Starred) for arg in call.args):
                _refuse(call, f"calling `{name}` with keyword or starred arguments")
            if len(call.args) != count:
                _refuse(call, f"calling `{name}` with {len(call.args)} arguments")
            for index, arg in enumerate(call.args):
                kind = _static_type(arg, scope)
                types[index] = kind if types[index] is None else _join(types[index], kind)
        return [UNKNOWN if kind is None else kind for kind in types]

    # Statements

    def _emit(self, depth: int, text: str) -> None:
        self.out.append("  " * depth + text)

    def _block(self, statements: list[ast.stmt], scope: _Scope, depth: int, taken: int) -> None:
        """Convert ``statements``; lines up to ``taken`` belong to an earlier statement."""
        for statement in statements:
            if statement.lineno <= taken:
                _refuse(statement, "two statements on one line")
            self.lines[statement.lineno] = HEADER_LINES + len(self.out) + 1
            self._statement(statement, scope, depth)
            taken = statement.end_lineno or statement.lineno

    def _statement(self, statement: ast.stmt, scope: _Scope, depth: int) -> None:
        if isinstance(statement, ast.Expr):
            value = statement.value
            if isinstance(value, ast.Constant):
                _refuse(statement, "a constant as a statement")
            if isinstance(value, ast.ListComp):
                self._emit(depth, self._comprehension(value, scope))
            else:
                self._emit(depth, self._expr(value, scope, awaits=True).text + ";")
        elif isinstance(statement, ast.Assign):
            self._assignment(statement, scope, depth)
        elif isinstance(statement, ast.For):
            self._for(statement, scope, depth)
        elif isinstance(statement, ast.FunctionDef):
            self._function(statement, scope, depth)
        else:
            kind = _STATEMENTS.get(type(statement), f"a {type(statement).__name__} statement")
            _refuse(statement, kind)

    def _assignment(self, statement: ast.Assign, scope: _Scope, depth: int) -> None:
        if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
            _refuse(statement, "assigning to anything but one name")
        name = statement.targets[0].id
        js = self.js[name]
        if isinstance(statement.value, ast.Lambda):
            text, binding = self._lambda(name, statement.value, scope)
        else:
            value = self._expr(statement.value, scope)
            text, binding = value.text, _Binding("value", js, value.type)
        scope.names[name] = binding
        self._emit(depth, f"const {js} = {text};")

    def _lambda(self, name: str, node: ast.Lambda, scope: _Scope) -> tuple[str, _Binding]:
        args = node.args
        if args.posonlyargs or args.vararg or args.kwonlyargs or args.kwarg or args.defaults:
            _refuse(node, "a lambda with defaults or special parameters")
        params = [arg.arg for arg in args.args]
        inner = _Scope(scope)
        for param, kind in zip(
            params, self._parameter_types(name, len(params), scope), strict=True
        ):
            inner.names[param] = _Binding("value", self.js[param], kind)
        # A lambda's body may build locators but not talk to the browser (awaits=False).
        body = self._expr(node.body, inner)
        text = f"({', '.join(self.js[p] for p in params)}) => {body.text}"
        return text, _Binding("lambda", self.js[name], body.type)

    def _function(self, statement: ast.FunctionDef, scope: _Scope, depth: int) -> None:
        args = statement.args
        if statement.decorator_list:
            _refuse(statement, "a decorated nested function")
        if (
            args.posonlyargs
            or args.vararg
            or args.kwonlyargs
            or args.kwarg
            or args.defaults
            or statement.type_params
        ):
            _refuse(statement, "a nested function with defaults or special parameters")
        for arg in args.args:
            annotation = arg.annotation
            if annotation is not None and not (
                isinstance(annotation, ast.Name) and annotation.id in ("bool", "str", "int")
            ):
                _refuse(arg, "a parameter annotation other than bool, str or int")
        returns = statement.returns
        if returns is not None and not (
            isinstance(returns, ast.Constant) and returns.value is None
        ):
            _refuse(statement, "a return annotation other than None")
        params = [arg.arg for arg in args.args]
        inner = _Scope(scope)
        kinds = self._parameter_types(statement.name, len(params), scope)
        for param, kind in zip(params, kinds, strict=True):
            inner.names[param] = _Binding("value", self.js[param], kind)
        js = self.js[statement.name]
        self._emit(depth, f"async function {js}({', '.join(self.js[p] for p in params)}) {{")
        self._block(statement.body, inner, depth + 1, _header_end(statement))
        self._emit(depth, "}")
        scope.names[statement.name] = _Binding("function", js, None)

    def _for(self, statement: ast.For, scope: _Scope, depth: int) -> None:
        if statement.orelse:
            _refuse(statement, "a for loop with an else clause")
        inner = _Scope(scope)
        header = self._loop_header(statement, statement.target, statement.iter, scope, inner)
        self._emit(depth, f"for ({header}) {{")
        self._block(statement.body, inner, depth + 1, _header_end(statement))
        self._emit(depth, "}")

    def _comprehension(self, node: ast.ListComp, scope: _Scope) -> str:
        """A list comprehension that is a statement: a loop on one line.

        Its list is thrown away, so what is left is running the item once per turn,
        which a for-of loop does in the same order.
        """
        generators = node.generators
        if len(generators) != 1 or generators[0].ifs or generators[0].is_async:
            _refuse(node, "a comprehension with if or with more than one for")
        inner = _Scope(scope)
        header = self._loop_header(node, generators[0].target, generators[0].iter, scope, inner)
        item = self._expr(node.elt, inner, awaits=True)
        return f"for ({header}) {{ {item.text}; }}"

    def _loop_header(
        self, node: ast.AST, target: ast.expr, iterable: ast.expr, scope: _Scope, inner: _Scope
    ) -> str:
        """What goes inside ``for (...)``; the loop's names are bound in ``inner``."""
        builtin = None
        if isinstance(iterable, ast.Call) and isinstance(iterable.func, ast.Name):
            builtin = iterable.func.id if iterable.func.id in ("range", "enumerate") else None
        if builtin is not None:
            assert isinstance(iterable, ast.Call)
            if iterable.keywords or len(iterable.args) != 1:
                _refuse(node, f"{builtin}() with other than one argument")
        if builtin == "range":
            assert isinstance(iterable, ast.Call)
            if not isinstance(target, ast.Name):
                _refuse(node, "unpacking a range()")
            # TypeScript reads the bound before every turn: a literal or a name, which is
            # never bound again, reads the same each time.
            bound = iterable.args[0]
            count = self._expr(bound, scope) if isinstance(bound, ast.Constant | ast.Name) else None
            if count is None or count.type != INT:
                _refuse(node, "range() of something that is not an int")
            js = self._bind(target, INT, inner)
            return f"let {js} = 0; {js} < {count.text}; {js}++"
        if builtin == "enumerate":
            assert isinstance(iterable, ast.Call)
            if not (isinstance(target, ast.Tuple) and len(target.elts) == 2):
                _refuse(node, "enumerate() without unpacking (index, item)")
            sequence = self._expr(iterable.args[0], scope)
            items = self._items(node, sequence)
            index = self._pattern(node, target.elts[0], INT, inner)
            item = self._pattern(node, target.elts[1], items, inner)
            return f"const [{index}, {item}] of {_operand(sequence, _PRIMARY)}.entries()"
        sequence = self._expr(iterable, scope)
        items = self._items(node, sequence)
        return f"const {self._pattern(node, target, items, inner)} of {sequence.text}"

    def _items(self, node: ast.AST, sequence: _Expr) -> Any:
        kind = sequence.type
        if isinstance(kind, tuple):
            return kind[1] if kind[0] == "list" else _join_all(kind[1])
        _refuse(node, f"a for loop over {_describe(kind)}")

    def _pattern(self, node: ast.AST, target: ast.expr, kind: Any, scope: _Scope) -> str:
        """The TypeScript binding for a loop target, checked against what it unpacks.

        Python raises when the number of values differs from the number of names and
        a JavaScript array pattern does not, so the numbers must be known to agree.
        """
        if isinstance(target, ast.Name):
            return self._bind(target, kind, scope)
        if not isinstance(target, ast.Tuple | ast.List) or not all(
            isinstance(element, ast.Name | ast.Tuple | ast.List) for element in target.elts
        ):
            _refuse(node, "a for target other than names")
        if not (isinstance(kind, tuple) and kind[0] == "tuple"):
            if isinstance(kind, tuple):
                _refuse(node, "unpacking a list whose length is not known")
            _refuse(node, f"unpacking {_describe(kind)}")
        if len(kind[1]) != len(target.elts):
            _refuse(node, f"unpacking {len(kind[1])} values into {len(target.elts)} names")
        parts = [
            self._pattern(node, element, element_kind, scope)
            for element, element_kind in zip(target.elts, kind[1], strict=True)
        ]
        return f"[{', '.join(parts)}]"

    # Expressions

    def _expr(self, node: ast.expr, scope: _Scope, awaits: bool = False) -> _Expr:
        """``node`` in TypeScript. ``awaits``: whether a call to the browser may stand here."""
        if isinstance(node, ast.Constant):
            return _constant(node)
        if isinstance(node, ast.Name):
            return self._name(node, scope)
        if isinstance(node, ast.Attribute):
            return self._attribute(node, scope)
        if isinstance(node, ast.Call):
            return self._call(node, scope, awaits)
        if isinstance(node, ast.List):
            items = [self._expr(element, scope, awaits) for element in node.elts]
            kind = ("list", _join_all(item.type for item in items))
            return _Expr(f"[{', '.join(item.text for item in items)}]", kind)
        if isinstance(node, ast.Tuple):
            items = [self._expr(element, scope) for element in node.elts]
            kind = ("tuple", tuple(item.type for item in items))
            return _Expr(f"[{', '.join(item.text for item in items)}]", kind)
        if isinstance(node, ast.BinOp):
            return self._binary(node, scope)
        if isinstance(node, ast.JoinedStr):
            return self._f_string(node, scope)
        _refuse(node, _EXPRESSIONS.get(type(node), f"a {type(node).__name__} expression"))

    def _name(self, node: ast.Name, scope: _Scope) -> _Expr:
        binding = scope.lookup(node.id)
        if binding is None:
            if node.id in _BUILTINS:
                _refuse(node, f"the name `{node.id}` as a value")
            _refuse(node, f"the name `{node.id}` where it is not bound")
        return _Expr(binding.js, binding.type)

    def _attribute(self, node: ast.Attribute, scope: _Scope) -> _Expr:
        if node.attr not in _PROPERTIES:
            _refuse(node, f"the attribute `.{node.attr}`")
        ts, receiver_type, result = _PROPERTIES[node.attr]
        receiver = self._expr(node.value, scope)
        if receiver.type != receiver_type:
            _refuse(node, f"`.{node.attr}` on {_describe(receiver.type)}")
        return _Expr(f"{_operand(receiver, _PRIMARY)}.{ts}()", result)

    def _call(self, node: ast.Call, scope: _Scope, awaits: bool) -> _Expr:
        func = node.func
        if isinstance(func, ast.Name):
            return self._call_name(node, func.id, scope, awaits)
        if not isinstance(func, ast.Attribute):
            _refuse(node, "a call of this form")
        owner = func.value
        if isinstance(owner, ast.Name) and owner.id == "re":
            return self._call_re(node, func.attr, scope)
        if (
            isinstance(owner, ast.Call)
            and isinstance(owner.func, ast.Name)
            and owner.func.id == "expect"
        ):
            return self._assertion(node, owner, func.attr, scope, awaits)
        if func.attr == "lower":
            return self._lower(node, owner, scope)
        receiver = self._expr(owner, scope)
        method = _METHODS.get(func.attr)
        if method is None:
            _refuse(node, f"the method `.{func.attr}()`")
        if receiver.type not in method.receivers:
            _refuse(node, f"`.{func.attr}()` on {_describe(receiver.type)}")
        label = f"`.{func.attr}()`"
        arguments = self._arguments(node, method.positional, method.options, label, scope)
        if func.attr == "evaluate":
            script = node.args[0]
            if not (
                isinstance(script, ast.Constant)
                and isinstance(script.value, str)
                and _EVALUATE.fullmatch(script.value)
            ):
                _refuse(
                    node,
                    "page.evaluate() of a string other than window.scrollTo(...) or "
                    "window.scrollBy(...)",
                )
        text = f"{_operand(receiver, _PRIMARY)}.{method.ts}({arguments})"
        if method.returns is not None:
            return _Expr(text, method.returns)
        if not awaits:
            _refuse(node, f"the browser call `.{func.attr}()` inside an expression")
        return _Expr(f"await {text}", None, _AWAIT)

    def _call_name(self, node: ast.Call, name: str, scope: _Scope, awaits: bool) -> _Expr:
        if name == "expect":
            _refuse(node, "expect() without an assertion")
        if name == "str":
            value = self._str_argument(node, scope)
            if value.type in (STR, INT):
                return _Expr(f"String({value.text})", STR)
            if value.type == BOOL:
                # str(True) is "True" and String(true) is "true": the same only lower-cased.
                _refuse(node, "str() of a bool outside str().lower()")
            _refuse(node, f"str() of {_describe(value.type)}")
        if name in ("range", "enumerate"):
            _refuse(node, f"{name}() outside a for header")
        binding = scope.lookup(name)
        if binding is None and name in self.local_functions:
            _refuse(node, f"the name `{name}` where it is not bound")
        if binding is None or binding.kind == "value":
            _refuse(node, f"a call to `{name}`")
        # The number and the kind of the arguments were checked where the function was defined.
        arguments = ", ".join(self._expr(arg, scope).text for arg in node.args)
        text = f"{binding.js}({arguments})"
        if binding.kind == "lambda":
            return _Expr(text, binding.type)
        if not awaits:
            _refuse(node, f"calling `{name}` inside an expression")
        return _Expr(f"await {text}", None, _AWAIT)

    def _str_argument(self, node: ast.Call, scope: _Scope) -> _Expr:
        if node.keywords or len(node.args) != 1 or isinstance(node.args[0], ast.Starred):
            _refuse(node, "str() with other than one argument")
        return self._expr(node.args[0], scope)

    def _lower(self, node: ast.Call, owner: ast.expr, scope: _Scope) -> _Expr:
        if node.args or node.keywords:
            _refuse(node, "`.lower()` with arguments")
        if (
            isinstance(owner, ast.Call)
            and isinstance(owner.func, ast.Name)
            and owner.func.id == "str"
        ):
            value = self._str_argument(owner, scope)
            if value.type not in (STR, INT, BOOL):
                _refuse(owner, f"str() of {_describe(value.type)}")
            return _Expr(f"String({value.text}).toLowerCase()", STR)
        receiver = self._expr(owner, scope)
        if receiver.type != STR:
            _refuse(node, f"`.lower()` on {_describe(receiver.type)}")
        return _Expr(f"{_operand(receiver, _PRIMARY)}.toLowerCase()", STR)

    def _assertion(
        self, node: ast.Call, subject_call: ast.Call, matcher: str, scope: _Scope, awaits: bool
    ) -> _Expr:
        if (
            subject_call.keywords
            or len(subject_call.args) != 1
            or isinstance(subject_call.args[0], ast.Starred)
        ):
            _refuse(subject_call, "expect() with other than one argument")
        subject = self._expr(subject_call.args[0], scope)
        if subject.type != LOCATOR:
            _refuse(subject_call, f"expect() on {_describe(subject.type)}")
        base = matcher.removeprefix("not_")
        if base not in _ASSERTIONS:
            _refuse(node, f"the assertion `{matcher}`")
        if not awaits:
            _refuse(node, f"the assertion `{matcher}` inside an expression")
        ts, positional = _ASSERTIONS[base]
        arguments = self._arguments(node, positional, _TIMEOUT, f"`{matcher}`", scope)
        if base == "to_match_aria_snapshot":
            _check_aria_snapshot(node)
        negation = "not." if matcher != base else ""
        return _Expr(f"await expect({subject.text}).{negation}{ts}({arguments})", None, _AWAIT)

    def _arguments(
        self, node: ast.Call, positional: int, options: frozenset[str], label: str, scope: _Scope
    ) -> str:
        """The arguments in TypeScript: the positional ones, then one options object."""
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            _refuse(node, "a starred argument")
        if any(keyword.arg is None for keyword in node.keywords):
            _refuse(node, "a `**` argument")
        if len(node.args) != positional:
            _refuse(node, f"{label} with {len(node.args)} positional arguments")
        parts = [self._expr(arg, scope).text for arg in node.args]
        pairs = []
        for keyword in node.keywords:
            assert keyword.arg is not None
            if keyword.arg not in options:
                _refuse(keyword, f"the keyword `{keyword.arg}=` of {label}")
            pairs.append(f"{_camel(keyword.arg)}: {self._expr(keyword.value, scope).text}")
        if pairs:
            parts.append("{ " + ", ".join(pairs) + " }")
        return ", ".join(parts)

    def _call_re(self, node: ast.Call, name: str, scope: _Scope) -> _Expr:
        if not self.imports_re:
            _refuse(node, "`re` without import re")
        if name == "escape":
            _refuse(node, "`re.escape` outside re.compile()")
        if name != "compile":
            _refuse(node, f"`re.{name}`")
        if node.keywords:
            _refuse(node, "re.compile() with keyword arguments")
        if not 1 <= len(node.args) <= 2:
            _refuse(node, f"re.compile() with {len(node.args)} arguments")
        flags = ""
        if len(node.args) == 2:
            flag = node.args[1]
            if not (
                isinstance(flag, ast.Attribute)
                and isinstance(flag.value, ast.Name)
                and flag.value.id == "re"
                and flag.attr in ("IGNORECASE", "I")
            ):
                _refuse(node, "re.compile() flags other than re.IGNORECASE or re.I")
            flags = "i"
        pattern = node.args[0]
        if isinstance(pattern, ast.Constant) and isinstance(pattern.value, str):
            if not pattern.value:
                # A regex literal cannot be empty: // starts a comment.
                _refuse(node, "an empty pattern")
            return _Expr(f"/{_checked_pattern(node, pattern.value, flags)}/{flags}", PATTERN)
        parts = _joined(pattern)
        if parts is None:
            _refuse(node, "a pattern other than a literal or literals joined with re.escape()")
        # re.escape turns any text into a run of literal characters. A run of one letter
        # and an empty run stand in for every run: the pattern must pass with both.
        for stand_in in ("x", ""):
            probe = "".join(
                part.value if isinstance(part, ast.Constant) else stand_in for part in parts
            )
            _checked_pattern(node, probe, flags)
        pieces = []
        for part in parts:
            if isinstance(part, ast.Constant):
                pieces.append(js_string(part.value))
                continue
            assert isinstance(part, ast.Call)
            if part.keywords or len(part.args) != 1 or isinstance(part.args[0], ast.Starred):
                _refuse(part, "re.escape() with other than one argument")
            text = self._expr(part.args[0], scope)
            if text.type != STR:
                _refuse(part, "re.escape() of something that is not a str")
            self.uses_re_escape = True
            pieces.append(f"reEscape({text.text})")
        source = " + ".join(pieces)
        if flags:
            source += f", {js_string(flags)}"
        return _Expr(f"new RegExp({source})", PATTERN)

    def _binary(self, node: ast.BinOp, scope: _Scope) -> _Expr:
        if isinstance(node.op, ast.Mult):
            count = node.right
            if (
                isinstance(node.left, ast.List)
                and len(node.left.elts) == 1
                and isinstance(count, ast.Constant)
                and type(count.value) is int
                and 0 <= count.value <= MAX_SAFE_INT
            ):
                item = self._expr(node.left.elts[0], scope)
                return _Expr(f"Array({count.value}).fill({item.text})", ("list", item.type))
            _refuse(node, "`*` other than [item] * count")
        if isinstance(node.op, ast.Add):
            left, right = self._expr(node.left, scope), self._expr(node.right, scope)
            if left.type == right.type == STR:
                text = f"{_operand(left, _ADD)} + {_operand(right, _ADD + 1)}"
                return _Expr(text, STR, _ADD)
            _refuse(node, "`+` on values that are not both str")
        _refuse(node, f"the operator {type(node.op).__name__}")

    def _f_string(self, node: ast.JoinedStr, scope: _Scope) -> _Expr:
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(_template_text(value.value))
                continue
            assert isinstance(value, ast.FormattedValue)
            if value.conversion != -1 or value.format_spec is not None:
                _refuse(value, "an f-string conversion or format spec")
            field = self._expr(value.value, scope)
            # Python formats a str or an int as a template literal does; others differ.
            if field.type not in (STR, INT):
                _refuse(value, "an f-string field that is not a str or int")
            parts.append("${" + field.text + "}")
        return _Expr("`" + "".join(parts) + "`", STR)


def _walk_in_order(tree: ast.AST) -> list[ast.AST]:
    nodes = [node for node in ast.walk(tree) if hasattr(node, "lineno")]
    return sorted(nodes, key=lambda node: (node.lineno, node.col_offset))


def _header_end(statement: ast.For | ast.FunctionDef) -> int:
    """The last line of a for or def header; its body starts after it."""
    if isinstance(statement, ast.For):
        return max(statement.target.end_lineno or 0, statement.iter.end_lineno or 0)
    ends = [arg.end_lineno or 0 for arg in statement.args.args]
    if statement.returns is not None:
        ends.append(statement.returns.end_lineno or 0)
    return max([statement.lineno, *ends])


# Types


def _join(a: Any, b: Any) -> Any:
    """The type of a value that is either an ``a`` or a ``b``."""
    if a == b:
        return a
    if isinstance(a, tuple) and isinstance(b, tuple) and a[0] == b[0]:
        if a[0] == "list":
            return ("list", _join(a[1], b[1]))
        if len(a[1]) == len(b[1]):
            return ("tuple", tuple(_join(x, y) for x, y in zip(a[1], b[1], strict=True)))
    return UNKNOWN


def _join_all(kinds: Iterable[Any]) -> Any:
    result: Any = None
    for kind in kinds:
        result = kind if result is None else _join(result, kind)
    return UNKNOWN if result is None else result


def _static_type(node: ast.expr, scope: _Scope) -> Any:
    """The type of ``node`` as far as literals and the names bound so far tell."""
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool):
            return BOOL
        if isinstance(value, int):
            return INT
        if isinstance(value, float):
            return FLOAT
        return STR if isinstance(value, str) else UNKNOWN
    if isinstance(node, ast.JoinedStr):
        return STR
    if isinstance(node, ast.List):
        return ("list", _join_all(_static_type(element, scope) for element in node.elts))
    if isinstance(node, ast.Tuple):
        return ("tuple", tuple(_static_type(element, scope) for element in node.elts))
    if isinstance(node, ast.Name):
        binding = scope.lookup(node.id)
        if binding is not None and binding.kind == "value":
            return binding.type
    return UNKNOWN


def _describe(kind: Any) -> str:
    if isinstance(kind, tuple):
        return "a list" if kind[0] == "list" else "a tuple"
    return "nothing" if kind is None else kind


# Literals


def _constant(node: ast.Constant) -> _Expr:
    value = node.value
    if isinstance(value, bool):
        return _Expr("true" if value else "false", BOOL)
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            _refuse(node, "an int beyond 2**53")
        return _Expr(str(value), INT)
    if isinstance(value, float):
        if not math.isfinite(value):
            _refuse(node, "a float that is not finite")
        return _Expr(repr(value), FLOAT)
    if isinstance(value, str):
        return _Expr(js_string(value), STR)
    if value is None:
        # None means "not given" to Playwright for Python; null is a value in TypeScript.
        _refuse(node, "None")
    _refuse(node, f"a {type(value).__name__} literal")


_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def _escape_char(ch: str) -> str:
    """``\\uXXXX`` for one character; a pair of them outside the Basic Multilingual Plane."""
    code = ord(ch)
    if code > 0xFFFF:
        high, low = divmod(code - 0x10000, 0x400)
        return f"\\u{0xD800 + high:04x}\\u{0xDC00 + low:04x}"
    return f"\\u{code:04x}"


def js_string(text: str) -> str:
    """``text`` as a double-quoted JavaScript string literal.

    Like JSON, but U+2028, U+2029 and every other character Python does not call
    printable (controls, separators, format characters, lone surrogates) are written
    as escapes, so the literal reads the same in any editor and any JavaScript.
    """
    out = ['"']
    for ch in text:
        if ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif ch.isprintable():
            out.append(ch)
        else:
            out.append(_escape_char(ch))
    out.append('"')
    return "".join(out)


def _template_text(text: str) -> str:
    """``text`` as the literal part of a template literal."""
    out = []
    for ch in text:
        if ch in "`$\\":
            out.append("\\" + ch)
        elif ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif ch.isprintable():
            out.append(ch)
        else:
            out.append(_escape_char(ch))
    return "".join(out)


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _operand(expr: _Expr, precedence: int) -> str:
    """``expr``'s text, in parentheses when it binds looser than ``precedence``."""
    return expr.text if expr.prec >= precedence else f"({expr.text})"


# Aria snapshots

# What JavaScript's trim() and \s count as white space.
_JS_SPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"  # noqa: E501


def _unshift(snapshot: str) -> str:
    """What Playwright Test's toMatchAriaSnapshot does to its expected text first.

    It drops blank lines and the indent all lines share; Playwright for Python sends
    the text as it is.
    """
    lines = snapshot.split("\n")
    kept = [line for line in lines if line.strip(_JS_SPACE)]
    indent = min([100, *(len(line) - len(line.lstrip(_JS_SPACE)) for line in kept)])
    return "\n".join(line[indent:] for line in kept)


def _check_aria_snapshot(node: ast.Call) -> None:
    expected = node.args[0]
    if not (isinstance(expected, ast.Constant) and isinstance(expected.value, str)):
        _refuse(node, "an aria snapshot that is not a string literal")
    if _unshift(expected.value) != expected.value:
        _refuse(node, "an aria snapshot Playwright Test would dedent")


# Patterns

_ESCAPES = frozenset("dDwWsSnrtfv") | frozenset("\\^$.|?*+()[]{}/-")
_GROUPS = ("(?:", "(?=", "(?!", "(?<=", "(?<!")
_LOOKAROUNDS = frozenset({"(?=", "(?!", "(?<=", "(?<!"})
_BRACES = re.compile(r"\{\d+(?:,\d*)?\}")
_LINE_BREAKS = {"\n": "\\n", "\r": "\\r", "\u2028": "\\u2028", "\u2029": "\\u2029"}


def _joined(node: ast.expr) -> list[ast.expr] | None:
    """The string literals and re.escape() calls that ``node`` joins with +, or None."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _joined(node.left), _joined(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "escape"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "re"
    ):
        return [node]
    return None


def _checked_pattern(node: ast.Call, pattern: str, flags: str) -> str:
    """The regex literal body for ``pattern``, once JavaScript and Python both take it."""
    try:
        body = _regex_literal(pattern)
    except ValueError as exc:
        reason = str(exc)
    else:
        try:
            re.compile(pattern, re.IGNORECASE if flags else 0)
        except re.error:
            raise PortRefused(
                f"line {node.lineno}: the pattern {pattern!r}, which Python cannot compile, "
                "is not handled"
            ) from None
        return body
    _refuse(node, f"{reason} in the pattern {pattern!r}")


def _pattern_char(ch: str) -> str:
    """One literal character of a pattern as it may stand in a regex literal."""
    if ch == "/":
        return "\\/"
    if ch in _LINE_BREAKS:
        return _LINE_BREAKS[ch]
    return ch if ch.isprintable() else _escape_char(ch)


def _regex_literal(pattern: str) -> str:
    """The body of a JavaScript regex literal whose source reads like ``pattern``.

    Only syntax that Python accepts and that JavaScript, without the u flag as the
    browser reads it, accepts with the same meaning gets through: anything else
    raises ValueError naming it. Escaping / and line breaks changes the text, not
    what the expression matches.
    """
    out = []
    lookarounds: list[bool] = []  # for each open group: whether it is a lookaround
    repeatable = False  # whether the last item may take a quantifier
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            escaped = pattern[i + 1 : i + 2]
            if escaped in ("b", "B"):
                repeatable = False
            elif escaped and escaped in _ESCAPES:
                repeatable = True
            else:
                raise ValueError(f"the escape \\{escaped}")
            out.append("\\" + escaped)
            i += 2
        elif ch == "[":
            i, text = _regex_class(pattern, i)
            out.append(text)
            repeatable = True
        elif ch == "(":
            opener = next((group for group in _GROUPS if pattern.startswith(group, i)), "(")
            if opener == "(" and pattern.startswith("(?", i):
                raise ValueError(f"the group {pattern[i : i + 3]}")
            lookarounds.append(opener in _LOOKAROUNDS)
            out.append(opener)
            i += len(opener)
            repeatable = False
        elif ch == ")":
            if not lookarounds:
                raise ValueError("an unbalanced )")
            repeatable = not lookarounds.pop()
            out.append(")")
            i += 1
        elif ch in "*+?{":
            braces = _BRACES.match(pattern, i) if ch == "{" else None
            if ch == "{" and braces is None:
                raise ValueError("a { that is not a quantifier")
            quantifier = braces.group() if braces else ch
            if not repeatable:
                raise ValueError("a quantifier with nothing to repeat")
            i += len(quantifier)
            if pattern.startswith("?", i):
                quantifier += "?"
                i += 1
            if i < len(pattern) and pattern[i] in "*+?{":
                raise ValueError("a possessive or repeated quantifier")
            out.append(quantifier)
            repeatable = False
        elif ch in "}]":
            raise ValueError(f"a lone {ch}")
        elif ch in "^$|":
            out.append(ch)
            i += 1
            repeatable = False
        else:
            out.append(_pattern_char(ch))
            i += 1
            repeatable = True
    if lookarounds:
        raise ValueError("an unclosed (")
    return "".join(out)


def _regex_class(pattern: str, start: int) -> tuple[int, str]:
    """The character class at ``start``: where it ends, and its text for a regex literal."""
    out = ["["]
    i = start + 1
    if pattern.startswith("^", i):
        out.append("^")
        i += 1
    if pattern.startswith("]", i):
        # Python reads []a] as a class holding ] and a; JavaScript as an empty class.
        raise ValueError("a class that starts with ]")
    while i < len(pattern):
        ch = pattern[i]
        if ch == "]":
            out.append("]")
            return i + 1, "".join(out)
        if ch == "\\":
            escaped = pattern[i + 1 : i + 2]
            if not escaped or escaped not in _ESCAPES:
                raise ValueError(f"the escape \\{escaped} inside a class")
            out.append("\\" + escaped)
            i += 2
            continue
        if ch == "[":
            raise ValueError("a [ inside a class")
        if pattern.startswith(("--", "&&", "~~", "||"), i):
            # Python reserves these for set operations and warns about them.
            raise ValueError("a set operator inside a class")
        out.append(_pattern_char(ch))
        i += 1
    raise ValueError("an unclosed [")


# The command


def _decoded(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise PortRefused("line 1: the source is not UTF-8") from None


def main(argv: Sequence[str] | None = None) -> int:
    """Port DIR/<app>/test_<task>.py of the tests and the fixes; write OUT/ports.json."""
    parser = argparse.ArgumentParser(
        prog="python -m hoh.fidelity.port",
        description="Port generated tests and reference fixes to Playwright Test specs.",
    )
    parser.add_argument("--tests", type=Path, required=True, help="DIR/<app>/test_<task>.py")
    parser.add_argument("--fixes", type=Path, required=True, help="DIR/<app>/test_<task>.py")
    parser.add_argument("--out", type=Path, required=True, help="where the specs go")
    args = parser.parse_args(argv)
    for root in (args.tests, args.fixes):
        if not root.is_dir():
            parser.error(f"{root} is not a directory")
    out = args.out
    out_root = out.resolve()

    def inside(path: Path) -> Path:
        # Every write goes through here: nothing lands outside OUT, symbolic links included.
        if not path.resolve().is_relative_to(out_root):
            parser.error(f"{path} is outside {out}")
        return path

    entries: list[dict[str, Any]] = []
    counts: dict[str, tuple[int, int]] = {}
    for kind, root in (("tests", args.tests), ("reference_fixes", args.fixes)):
        ported = 0
        sources = sorted(root.glob("*/test_*.py"))
        for source in sources:
            relative = Path(
                kind, source.parent.name, source.stem.removeprefix("test_") + ".spec.ts"
            )
            target = inside(out / relative)
            data = source.read_bytes()
            app, task = source.parent.name, source.stem.removeprefix("test_")
            entry: dict[str, Any] = {
                "kind": kind,
                "app": app,
                "task": task,
                "source": str(source),
                "source_sha256": hashlib.sha256(data).hexdigest(),
            }
            if kind == "tests":
                # The test's reference fix, by which the validation baselines tie the test to
                # its scenario: the port of this test is the port of that scenario's test.
                fix = args.fixes / app / source.name
                entry["reference_fix_sha256"] = (
                    hashlib.sha256(fix.read_bytes()).hexdigest() if fix.is_file() else None
                )
            try:
                port = port_source(_decoded(data))
            except PortRefused as exc:
                entry["refused"] = str(exc)
                target.unlink(missing_ok=True)
            else:
                payload = port.text.encode("utf-8")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                entry["output"] = relative.as_posix()
                entry["output_sha256"] = hashlib.sha256(payload).hexdigest()
                entry["lines"] = {str(line): port.lines[line] for line in sorted(port.lines)}
                ported += 1
            entries.append(entry)
        counts[kind] = (ported, len(sources))
    out.mkdir(parents=True, exist_ok=True)
    inside(out / "ports.json").write_text(json.dumps({"ports": entries}, indent=2) + "\n")
    tests_ported, tests_total = counts["tests"]
    fixes_ported, fixes_total = counts["reference_fixes"]
    ported, total = tests_ported + fixes_ported, tests_total + fixes_total
    print(
        f"ported {ported} of {total} files ({tests_ported} of {tests_total} tests, "
        f"{fixes_ported} of {fixes_total} reference fixes); {total - ported} refused"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
