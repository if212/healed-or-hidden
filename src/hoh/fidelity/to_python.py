"""Read a TypeScript spec as the Python test that ``hoh.mechanism`` compares.

The fidelity check judges T', the spec an agent handed back, by the study's own rules
(design doc 5.7, plan decision 8): whether T' edited the oracle of T, the port it was given,
is ``hoh.mechanism.oracle_edited``'s definition applied to their ``expect`` statements, and
§5.7's emptied, deleted, renamed and split rules are ``hoh.mechanism.classify``'s. Those read
Python. ``to_python`` writes a spec as the Python they read, construct for construct, so that
they run on it unchanged. A port read this way is the Python test it was made from, apart
from annotations, f-strings and the other places where the converter had to write TypeScript
differently (``hoh.fidelity.port``). It reads tokens (``hoh.fidelity.static.tokenize``) and
never runs the source.

What becomes what:

- Each ``test(title, callback)``, whatever ``describe`` blocks hold it, is a module-level
  ``def`` named by its own title when that title is a Python name starting with ``test`` and
  no other test has it, else ``test__ts_<k>`` (k counting those, in order): a target whose
  title two tests share is the target split. Its parameters are the fixtures the callback
  asks for. ``test.skip``, ``test.fixme`` and ``test.fail`` declarations, skipped groups and
  group-level ``test.skip(...)`` calls are ``pytest.mark`` decorators; inside a test, those
  calls are ``pytest.skip`` and ``pytest.xfail`` (under an ``if`` when they take a
  condition). Hooks are autouse fixtures ``ts_hook_<kind>``; other group-level code, and
  ``test.describe.configure``, is module-level code. A module-level function whose name
  starts with ``test`` gets ``ts_`` before it, so that it is not taken for a test.
- ``await`` goes, as Python has none. A statement that is an assertion with a web-first
  matcher (or ``expect.poll``) and is not awaited is ``nowait(...)``: nothing waits for it,
  so it is not the assertion it looks like. ``expect.soft``, ``test.expect`` and an alias of
  the fixture's ``expect`` are ``expect``.
- Member names are snake_case (``getByRole`` is ``get_by_role``; ``.not.toBeVisible`` on an
  assertion is ``not_to_be_visible``, elsewhere ``.not`` is ``.not_``); ``.first()``,
  ``.last()`` and ``.contentFrame()`` are the Python properties. Type arguments, type
  assertions, ``as`` and ``satisfies`` go. An object literal that is a call's last argument is
  its keyword arguments (keys in snake_case) when its keys allow; any other object literal is a
  dict with the keys as written. A regular expression, ``new RegExp(...)`` and ``RegExp(...)``
  are ``re.compile(source[, flags])`` (``\\/`` in a literal is ``/``) and ``reEscape`` is
  ``re.escape``. A template literal with substitutions is ``ts_template(text, value, ...)``.
- Statements map to their Python counterparts: ``if``, ``while``, ``try``/``catch``/``finally``
  (``except Exception``), ``for...of`` (``.entries()`` is ``enumerate``), a counting loop
  ``for (let i = 0; i < n; i++)`` is ``for i in range(n)``, nested and arrow functions with a
  block are ``def``. What has none is a call that keeps the construct apart:
  ``for ts_for in cfor("<header>")``, ``for key in ts_keys(...)``, ``while ts_do(...)``,
  a ``switch`` as ``for ts_switch in [value]`` around an ``if ts_case(...)`` chain, and
  ``with test.step(title):`` around a step's callback. A function with a block inside an
  expression is ``lambda <params>: ts_block("<its code>")``.
- What the mirror does not read (a class, a type declaration, an unusual statement) is kept
  as ``ts("<its code>")`` in its place, and ``Mirror.opaque`` lists its line. Code is written
  as its tokens joined by spaces, so formatting and comments never count.

Names Python reserves get a trailing underscore (``re`` too: the mechanism reads it as the
module); ``$`` is ``_dollar_``. A source that cannot be split into tokens, or whose brackets do
not match, raises ``Untranslatable``.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import keyword
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from hoh.fidelity.static import SpecSyntaxError, Token, tokenize

FIXTURE_MODULES = frozenset({"pwfix", "@playwright/test"})
"""Modules whose ``test`` and ``expect`` are Playwright Test's: ``playwright.sync_api``."""
PLAYWRIGHT = "playwright.sync_api"

#: The web-first matchers, which return a promise: ``ASYNC_MATCHERS`` of src/pwfix/lib/expect.js,
#: and ``toPass``.
ASYNC_MATCHERS = frozenset(
    {
        "toBeAttached", "toBeChecked", "toBeDisabled", "toBeEditable", "toBeEmpty",
        "toBeEnabled", "toBeFocused", "toBeHidden", "toBeInViewport", "toBeVisible",
        "toContainClass", "toContainText", "toHaveAccessibleDescription",
        "toHaveAccessibleErrorMessage", "toHaveAccessibleName", "toHaveAttribute", "toHaveCSS",
        "toHaveClass", "toHaveCount", "toHaveId", "toHaveJSProperty", "toHaveRole",
        "toHaveScreenshot", "toHaveText", "toHaveValue", "toHaveValues", "toMatchAriaSnapshot",
        "toHaveTitle", "toHaveURL", "toBeOK", "toPass",
    }
)

# Methods of a locator that are properties in Python, when called without arguments.
_PROPERTIES = {"first": "first", "last": "last", "contentFrame": "content_frame"}
_DECLARE = {None: None, "only": None, "skip": "skip", "fixme": "skip", "fail": "xfail"}
_GROUP = frozenset({"only", "skip", "fixme", "serial", "parallel"})
_HOOKS = frozenset({"before_each", "after_each", "before_all", "after_all"})
_SKIPS = {"skip": "skip", "fixme": "skip", "fail": "xfail"}

_BINARY = {
    "??": 1, "||": 2, "&&": 3, "|": 4, "^": 5, "&": 6,
    "==": 7, "!=": 7, "===": 7, "!==": 7,
    "<": 8, ">": 8, "<=": 8, ">=": 8, "instanceof": 8, "in": 8,
    "<<": 9, ">>": 9, ">>>": 9,
    "+": 10, "-": 10, "*": 11, "/": 11, "%": 11, "**": 12,
}
_ARITHMETIC = {
    "+": ast.Add, "-": ast.Sub, "*": ast.Mult, "/": ast.Div, "%": ast.Mod, "**": ast.Pow,
    "|": ast.BitOr, "^": ast.BitXor, "&": ast.BitAnd, "<<": ast.LShift, ">>": ast.RShift,
    ">>>": ast.RShift,
}
_COMPARE = {
    "==": ast.Eq, "===": ast.Eq, "!=": ast.NotEq, "!==": ast.NotEq, "<": ast.Lt, ">": ast.Gt,
    "<=": ast.LtE, ">=": ast.GtE, "in": ast.In,
}
_ASSIGN = frozenset(
    {"=", "+=", "-=", "*=", "/=", "%=", "**=", "<<=", ">>=", ">>>=", "&=", "|=", "^=", "&&=",
     "||=", "??="}
)
_TYPE_STOPS = frozenset(
    {",", ";", "=", ")", "]", "}", "?", ":", "=>", "&&", "||", "??", "+", "-", "*", "/", "%",
     "==", "===", "!=", "!==", "<=", ">=", "in", "instanceof", "as", "satisfies"}
)
# Tokens a statement can end with, and punctuators that continue an expression on a new line.
_ENDS = frozenset({")", "]", "}", "++", "--"})
_CONTINUES = frozenset(
    {".", "?.", ",", "=>", "?", ":", ")", "]", "}", "=", "*", "/", "%", "**", "<", ">", "<=", ">=",
     "==", "!=", "===", "!==", "&&", "||", "??", "&", "|", "^", "<<", ">>", ">>>"}
    | _ASSIGN
)
_NOT_STARTS = frozenset({"else", "catch", "finally", "instanceof", "in", "of", "as", "satisfies"})
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TEMPLATE_ESCAPE = re.compile(
    r"\\(?:u\{([0-9a-fA-F]+)\}|u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|(\r\n|[\n\r\u2028\u2029])|(.))",
    re.DOTALL,
)
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_PAIRS = {"(": ")", "[": "]", "{": "}"}


class Untranslatable(ValueError):
    """The source cannot be read: no tokens, or brackets that do not match."""


class _Unread(Exception):
    """The statement being read is not one the mirror reads: it is kept as text."""


@dataclass(frozen=True)
class TestDecl:
    """One test the spec declares."""

    __test__ = False  # not a pytest class

    title: str | None
    """Its own title, without the describe titles; None when it is not a literal."""
    path: tuple[str | None, ...]
    """The titles of the named describe blocks around it, outermost first."""
    name: str
    """The function it became."""
    line: int


@dataclass(frozen=True)
class Mirror:
    """A spec read as Python."""

    python: str
    tests: tuple[TestDecl, ...]
    opaque: tuple[int, ...]
    """The lines of statements kept as ``ts("<code>")``."""


def to_python(source: str) -> Mirror:
    """The Python ``hoh.mechanism`` reads for the spec ``source`` (module docstring)."""
    try:
        tokens = tokenize(source)
    except SpecSyntaxError as exc:
        raise Untranslatable(str(exc)) from None
    _check_brackets(tokens)
    reader = _Reader(tokens)
    try:
        body = reader.module()
    except RecursionError:
        raise Untranslatable("the source is nested too deeply") from None
    tests = reader.name_tests()
    module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    python = ast.unparse(module)
    try:
        ast.parse(python)
    except SyntaxError as exc:  # a statement the reader should have kept as text
        raise Untranslatable(f"the mirror is not Python: {exc}") from None
    return Mirror(python=python, tests=tests, opaque=tuple(reader.opaque))


# Tokens


def _opens(token: Token) -> bool:
    if token.kind == "template":
        return token.text.endswith("${")
    return token.kind == "punct" and token.text in _PAIRS


def _closes(token: Token) -> bool:
    if token.kind == "template":
        return token.text.startswith("}")
    return token.kind == "punct" and token.text in (")", "]", "}")


def _check_brackets(tokens: list[Token]) -> None:
    expected: list[str] = []
    for token in tokens:
        if _closes(token):
            want = "${" if token.kind == "template" else token.text
            if not expected or expected[-1] != want:
                raise Untranslatable(
                    f"line {token.line}: {token.text[:1]} does not close a bracket"
                )
            expected.pop()
        if _opens(token):
            expected.append("${" if token.kind == "template" else _PAIRS[token.text])
    if expected:
        raise Untranslatable("a bracket is not closed at the end of the source")


def _punct(token: Token | None, *texts: str) -> bool:
    return token is not None and token.kind == "punct" and token.text in texts


def _word(token: Token | None, *texts: str) -> bool:
    return token is not None and token.kind == "name" and token.text in texts


def _end_line(token: Token) -> int:
    return token.line + (token.text.count("\n") if token.kind == "template" else 0)


def _can_end(token: Token) -> bool:
    if token.kind == "template":
        return token.text.endswith("`")
    if token.kind == "punct":
        return token.text in _ENDS
    return True


def _can_start(token: Token) -> bool:
    if token.kind == "template":
        return token.text.startswith("`")
    if token.kind == "punct":
        return token.text not in _CONTINUES
    return not (token.kind == "name" and token.text in _NOT_STARTS)


def _text(tokens: Iterable[Token]) -> str:
    """Code as its tokens, joined by spaces."""
    return " ".join(
        json.dumps(t.text, ensure_ascii=False) if t.kind == "string" else t.text for t in tokens
    )


# Python


def _snake(name: str) -> str:
    return _CAMEL.sub("_", name).lower()


def _py_name(name: str) -> str:
    """``name`` as a Python name that means the same in every place it appears."""
    out = name.replace("$", "_dollar_")
    if not out.isidentifier():
        out = "".join(c if ("a" + c).isidentifier() else f"_u{ord(c):x}_" for c in out)
        if not out[:1].isidentifier():
            out = "_" + out
    if keyword.iskeyword(out) or out == "re":
        out += "_"
    return out


def _name(identifier: str) -> ast.Name:
    return ast.Name(id=identifier, ctx=ast.Load())


def _call(func: ast.expr | str, *args: ast.expr, keywords: list[ast.keyword] | None = None
          ) -> ast.Call:
    callee = _name(func) if isinstance(func, str) else func
    return ast.Call(func=callee, args=list(args), keywords=keywords or [])


def _attr(value: ast.expr, name: str) -> ast.Attribute:
    return ast.Attribute(value=value, attr=name, ctx=ast.Load())


def _dotted(*parts: str) -> ast.expr:
    node: ast.expr = _name(parts[0])
    for part in parts[1:]:
        node = _attr(node, part)
    return node


def _const(value: object) -> ast.Constant:
    return ast.Constant(value=value)


def _arguments(names: Iterable[str]) -> ast.arguments:
    return ast.arguments(
        posonlyargs=[], args=[ast.arg(arg=n) for n in names], vararg=None, kwonlyargs=[],
        kw_defaults=[], kwarg=None, defaults=[],
    )


def _def(name: str, params: list[str], body: list[ast.stmt],
         decorators: list[ast.expr] | None = None) -> ast.FunctionDef:
    return ast.FunctionDef(
        name=name, args=_arguments(params), body=body or [ast.Pass()],
        decorator_list=decorators or [], returns=None, type_comment=None, type_params=[],
    )


def _unique(names: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(names))


def _module(name: str) -> str:
    if name in FIXTURE_MODULES:
        return PLAYWRIGHT
    return "ts_module_" + hashlib.sha256(name.encode()).hexdigest()[:12]


def _number(text: str) -> int | float:
    digits = text.replace("_", "").removesuffix("n")
    low = digits.lower()
    try:
        for prefix, base in (("0x", 16), ("0o", 8), ("0b", 2)):
            if low.startswith(prefix):
                return int(digits[2:], base)
        value: int | float = float(digits) if ("." in low or "e" in low) else int(digits)
    except ValueError:
        raise _Unread(f"the number {text}") from None
    if isinstance(value, float) and value.is_integer() and abs(value) <= 2**53:
        return int(value)
    return value


def _cook(piece: str) -> str:
    """The text of one piece of a template literal, its escapes resolved."""
    body = piece[1:]
    body = body[:-2] if body.endswith("${") else body[:-1]
    body = body.replace("\r\n", "\n").replace("\r", "\n")

    def one(match: re.Match[str]) -> str:
        braced, four, two, newline, other = match.groups()
        if newline is not None:
            return ""
        code = braced or four or two
        if code is not None:
            return chr(int(code, 16)) if int(code, 16) <= 0x10FFFF else ""
        return _SIMPLE_ESCAPES.get(other, other)

    return _TEMPLATE_ESCAPE.sub(one, body)


def _regex(text: str) -> ast.Call:
    """A regular expression literal as ``re.compile``: a ``/`` in its pattern, which the
    literal has to escape, is the ``/`` Python writes."""
    end = text.rfind("/")
    pattern = _ESCAPED_SLASH.sub(lambda m: "/" if m.group() == "\\/" else m.group(), text[1:end])
    args: list[ast.expr] = [_const(pattern)]
    if text[end + 1:]:
        args.append(_const(text[end + 1:]))
    return _call(_dotted("re", "compile"), *args)


_ESCAPED_SLASH = re.compile(r"\\.", re.DOTALL)


def _expect_call(node: ast.expr) -> bool:
    """Whether ``node`` is ``expect(...)`` or ``expect.poll(...)``, with ``.resolves`` or
    ``.rejects`` after it."""
    while isinstance(node, ast.Attribute) and node.attr in ("resolves", "rejects"):
        node = node.value
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "poll":
        func = func.value
    return isinstance(func, ast.Name) and func.id == "expect"


def _mark(kind: str, args: list[ast.expr] | None = None) -> ast.expr:
    """``pytest.mark.<kind>``, called with ``args`` when there are any; ``skipif`` for a
    conditional skip."""
    if args:
        return _call(_dotted("pytest", "mark", "skipif" if kind == "skip" else kind), *args)
    return _dotted("pytest", "mark", kind)


def _valid_target(node: ast.expr) -> bool:
    if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
        return True
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(
            _valid_target(e.value if isinstance(e, ast.Starred) else e) for e in node.elts
        )
    return False


def _async_assertion(node: ast.expr) -> bool:
    """Whether ``node`` is an assertion that returns a promise."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    matcher = node.func.attr
    if matcher.startswith("not_to_"):
        matcher = matcher[len("not_"):]
    base = node.func.value
    chained = False
    while isinstance(base, ast.Attribute) and base.attr in ("not_", "resolves", "rejects"):
        chained = chained or base.attr != "not_"
        base = base.value
    if not isinstance(base, ast.Call):
        return False
    func = base.func
    if isinstance(func, ast.Name) and func.id == "expect":
        return chained or matcher in _ASYNC
    return (
        isinstance(func, ast.Attribute) and func.attr == "poll"
        and isinstance(func.value, ast.Name) and func.value.id == "expect"
    )


_ASYNC = frozenset(_snake(m) for m in ASYNC_MATCHERS)


# Reading


@dataclass
class _Fn:
    """A function literal: its parameters and where its body is."""

    node: ast.Lambda
    params: list[str]
    fixtures: list[str]
    """For a test's callback: the keys of its first parameter, then the other parameters."""
    block: tuple[int, int] | None
    """The indices of the braces of its body; None for an expression body."""
    expr: ast.expr | None


@dataclass
class _Group:
    title: str | None
    named: bool
    marks: list[ast.expr] = field(default_factory=list)
    defs: list[ast.FunctionDef] = field(default_factory=list)


@dataclass
class _Declared:
    title: str | None
    path: tuple[str | None, ...]
    func: ast.FunctionDef
    line: int


class _Reader:
    def __init__(self, tokens: list[Token]) -> None:
        self.toks = tokens
        self.i = 0
        self.end = len(tokens)
        self.loops = 0
        self.functions_depth = 0
        self.opaque: list[int] = []
        self.functions: dict[int, _Fn] = {}
        self.objects: dict[int, tuple[ast.Dict, list[tuple[str, ast.expr] | None]]] = {}
        self.assigns: dict[int, tuple[ast.Call, str, ast.expr, ast.expr]] = {}
        self.increments: dict[int, tuple[ast.Call, ast.expr, str]] = {}
        self.test_names = {"test"}
        self.expect_names: set[str] = set()
        self.groups = [_Group(None, False)]
        self.declared: list[_Declared] = []
        self.hooks: Counter[str] = Counter()

    # Positions

    def peek(self, offset: int = 0) -> Token | None:
        k = self.i + offset
        return self.toks[k] if k < self.end else None

    def take(self) -> Token:
        token = self.peek()
        if token is None:
            raise _Unread("the code ends early")
        self.i += 1
        return token

    def expect(self, text: str) -> None:
        if not _punct(self.peek(), text):
            raise _Unread(f"{text} expected")
        self.i += 1

    def newline_before(self, k: int) -> bool:
        return 0 < k < len(self.toks) and self.toks[k].line > _end_line(self.toks[k - 1])

    def close(self, k: int) -> int:
        """The index of the bracket that closes the one at ``k``."""
        depth = 0
        for j in range(k, len(self.toks)):
            if _closes(self.toks[j]):
                depth -= 1
                if depth == 0:
                    return j
            if _opens(self.toks[j]):
                depth += 1
        raise Untranslatable(f"line {self.toks[k].line}: a bracket is not closed")

    @contextmanager
    def limit(self, end: int) -> Iterator[None]:
        saved = self.end
        self.end = end
        try:
            yield
        finally:
            self.end = saved

    def end_statement(self) -> None:
        token = self.peek()
        if token is None or _punct(token, "}"):
            return
        if _punct(token, ";"):
            self.i += 1
            return
        if self.newline_before(self.i):
            return
        raise _Unread("the statement goes on")

    # The module and its tests

    def module(self) -> list[ast.stmt]:
        return self.statements(len(self.toks), "module")

    def name_tests(self) -> tuple[TestDecl, ...]:
        titles = Counter(d.title for d in self.declared if d.title is not None)
        own = {
            d.title for d in self.declared
            if d.title is not None and titles[d.title] == 1 and _is_test_name(d.title)
        }
        generated = 0
        out = []
        for d in self.declared:
            if d.title in own:
                name = d.title
            else:
                generated += 1
                while f"test__ts_{generated}" in own:
                    generated += 1
                name = f"test__ts_{generated}"
            d.func.name = name
            out.append(TestDecl(title=d.title, path=d.path, name=name, line=d.line))
        return tuple(out)

    # Statements

    def statements(self, stop: int, scope: str) -> list[ast.stmt]:
        """The statements up to the token at ``stop``: a block's closing brace, or the end."""
        out: list[ast.stmt] = []
        while self.i < stop:
            out.extend(self.recovering(stop, scope))
        return out

    def recovering(self, stop: int, scope: str) -> list[ast.stmt]:
        """One statement; one that cannot be read is kept as its text."""
        start = self.i
        try:
            with self.limit(stop):
                return self.statement(scope)
        except (_Unread, IndexError, KeyError, TypeError, AttributeError):
            end = self.statement_end(start, stop)
            self.i = end
            tokens = self.toks[start:end]
            if tokens and _punct(tokens[-1], ";"):
                tokens = tokens[:-1]
            self.opaque.append(self.toks[start].line)
            return [ast.Expr(value=_call("ts", _const(_text(tokens))))]

    def statement_end(self, start: int, stop: int) -> int:
        """Where a statement that could not be read ends, as far as its tokens tell."""
        depth = 0
        for k in range(start, stop):
            token = self.toks[k]
            if _closes(token):
                if depth == 0:
                    return max(k, start + 1)
                depth -= 1
            if _opens(token):
                depth += 1
            if depth == 0:
                if _punct(token, ";"):
                    return k + 1
                following = k + 1
                if (following < stop and self.newline_before(following) and _can_end(token)
                        and _can_start(self.toks[following])):
                    return following
        return max(stop, start + 1)

    def statement(self, scope: str) -> list[ast.stmt]:
        token = self.peek()
        assert token is not None
        if _punct(token, ";"):
            self.i += 1
            return []
        if _punct(token, "{"):
            return self.block(scope)
        if token.kind == "name":
            word = token.text
            following = self.peek(1)
            if word in ("const", "var") or (
                word == "let" and (_punct(following, "[", "{") or
                                   (following is not None and following.kind == "name"))
            ):
                return self.declaration(scope)
            handler = {
                "if": self.if_, "for": self.for_, "while": self.while_, "do": self.do,
                "try": self.try_, "switch": self.switch, "return": self.return_,
                "throw": self.throw, "break": self.jump, "continue": self.jump,
                "import": self.import_,
            }.get(word)
            if handler is not None and not (word == "import" and _punct(following, "(", ".")):
                return handler(scope)
            if word == "function" or (word == "async" and _word(following, "function")):
                return [self.function_declaration(scope)]
            if word == "export":
                self.i += 1
                if _word(self.peek(), "default"):
                    self.i += 1
                return self.statement(scope)
            if word == "debugger":
                self.i += 1
                self.end_statement()
                return []
            if word in ("class", "type", "interface", "declare", "enum", "namespace", "module",
                        "abstract", "with") and following is not None and (
                    following.kind == "name" or _punct(following, "(", "{")):
                raise _Unread(f"{word} is not read")
            if _punct(following, ":") and not keyword.iskeyword(word):
                self.i += 2  # a label
                return self.statement(scope)
        return self.expression_statement(scope)

    def block(self, scope: str) -> list[ast.stmt]:
        if not _punct(self.peek(), "{"):
            raise _Unread("{ expected")
        open_ = self.i
        close = self.close(open_)
        self.i = open_ + 1
        body = self.statements(close, scope)
        self.i = close + 1
        return body

    def body(self, scope: str) -> list[ast.stmt]:
        """The statement a control statement runs, as a body."""
        return self.statement(scope) or [ast.Pass()]

    def declaration(self, scope: str) -> list[ast.stmt]:
        self.i += 1
        found: list[tuple[ast.expr, ast.expr | None]] = []
        while True:
            start = self.i
            token = self.take()
            if token.kind == "name":
                target: ast.expr = _name(_py_name(token.text))
            elif _punct(token, "{", "["):
                close = self.close(start)
                names, _ = self.bound(start, close + 1)
                target = ast.Tuple(elts=[_name(_py_name(n)) for n in names], ctx=ast.Load())
                self.i = close + 1
            else:
                raise _Unread("a declaration of this form")
            if _punct(self.peek(), "!"):
                self.i += 1
            if _punct(self.peek(), ":"):
                self.i += 1
                self.skip_type(frozenset({"=", ",", ";"}))
            value = None
            if _punct(self.peek(), "="):
                self.i += 1
                value = self.assignment()
            found.append((target, value))
            if not _punct(self.peek(), ","):
                break
            self.i += 1
        self.end_statement()
        out: list[ast.stmt] = []
        for target, value in found:
            fn = self.fn(value)
            if fn is not None and fn.block is not None and isinstance(target, ast.Name):
                out.append(self.function_def(target.id, fn, scope))
            else:
                out.append(ast.Assign(targets=[target],
                                      value=_const(None) if value is None else value,
                                      type_comment=None))
        return out

    def paren_expression(self) -> ast.expr:
        if not _punct(self.peek(), "("):
            raise _Unread("( expected")
        close = self.close(self.i)
        self.i += 1
        with self.limit(close):
            value = self.expression()
            if self.i != close:
                raise _Unread(") expected")
        self.i = close + 1
        return value

    def if_(self, scope: str) -> list[ast.stmt]:
        self.i += 1
        test = self.paren_expression()
        body = self.body(scope)
        orelse: list[ast.stmt] = []
        if _word(self.peek(), "else"):
            self.i += 1
            orelse = self.body(scope)
        return [ast.If(test=test, body=body, orelse=orelse)]

    def loop_body(self, scope: str) -> list[ast.stmt]:
        self.loops += 1
        try:
            return self.body(scope)
        finally:
            self.loops -= 1

    def for_(self, scope: str) -> list[ast.stmt]:
        self.i += 1
        if _word(self.peek(), "await"):
            self.i += 1
        if not _punct(self.peek(), "("):
            raise _Unread("( expected")
        open_ = self.i
        close = self.close(open_)
        semis = self.top_level(open_ + 1, close, ";")
        if semis:
            if len(semis) != 2:
                raise _Unread("a for header of this form")
            counted = self.counting(open_ + 1, semis[0], semis[1], close)
            if counted is not None:
                target, iterable = counted
            else:
                target = _name("ts_for")
                iterable = _call("cfor", _const(_text(self.toks[open_ + 1:close])))
        else:
            target, iterable = self.for_in_of(open_, close)
        self.i = close + 1
        body = self.loop_body(scope)
        return [ast.For(target=target, iter=iterable, body=body, orelse=[], type_comment=None)]

    def top_level(self, start: int, stop: int, text: str) -> list[int]:
        """Indices of the punctuator ``text`` at depth 0 in ``start..stop``."""
        found = []
        depth = 0
        for k in range(start, stop):
            token = self.toks[k]
            if _closes(token):
                depth -= 1
            if _opens(token):
                depth += 1
            if depth == 0 and _punct(token, text):
                found.append(k)
        return found

    def counting(self, start: int, first: int, second: int, close: int
                 ) -> tuple[ast.expr, ast.expr] | None:
        """``for (let i = 0; i < n; i++)`` as ``(i, range(n))``; None for any other header."""
        init = self.toks[start:first]
        if init and _word(init[0], "let", "var", "const"):
            init = init[1:]
        cond = self.toks[first + 1:second]
        update = self.toks[second + 1:close]
        if not (len(init) == 3 and init[0].kind == "name" and _punct(init[1], "=")
                and init[2].kind == "number" and init[2].text == "0"):
            return None
        counter = init[0].text
        if not (len(cond) == 3 and _word(cond[0], counter) and _punct(cond[1], "<")
                and cond[2].kind in ("number", "name")):
            return None
        texts = [(t.kind, t.text) for t in update]
        if texts not in (
            [("name", counter), ("punct", "++")], [("punct", "++"), ("name", counter)],
            [("name", counter), ("punct", "+="), ("number", "1")],
        ):
            return None
        bound = cond[2]
        limit = _const(_number(bound.text)) if bound.kind == "number" else _name(
            _py_name(bound.text))
        return _name(_py_name(counter)), _call("range", limit)

    def for_in_of(self, open_: int, close: int) -> tuple[ast.expr, ast.expr]:
        k = open_ + 1
        if _word(self.toks[k], "const", "let", "var"):
            k += 1
        token = self.toks[k]
        if token.kind == "name" and not _word(token, "of", "in"):
            names, after = [token.text], k + 1
            target: ast.expr = _name(_py_name(token.text))
        elif _punct(token, "{", "["):
            end = self.close(k)
            names, _ = self.bound(k, end + 1)
            after = end + 1
            target = ast.Tuple(elts=[_name(_py_name(n)) for n in names], ctx=ast.Load())
        else:
            raise _Unread("a for target of this form")
        if after >= close or not _word(self.toks[after], "of", "in"):
            raise _Unread("for ... of or for ... in expected")
        kind = self.toks[after].text
        self.i = after + 1
        with self.limit(close):
            iterable = self.expression()
            if self.i != close:
                raise _Unread(") expected")
        if kind == "in":
            return target, _call("ts_keys", iterable)
        if (isinstance(iterable, ast.Call) and isinstance(iterable.func, ast.Attribute)
                and iterable.func.attr == "entries" and not iterable.args
                and not iterable.keywords):
            iterable = _call("enumerate", iterable.func.value)
        return target, iterable

    def while_(self, scope: str) -> list[ast.stmt]:
        self.i += 1
        test = self.paren_expression()
        return [ast.While(test=test, body=self.loop_body(scope), orelse=[])]

    def do(self, scope: str) -> list[ast.stmt]:
        self.i += 1
        body = self.loop_body(scope)
        if not _word(self.peek(), "while"):
            raise _Unread("while expected")
        self.i += 1
        test = self.paren_expression()
        if _punct(self.peek(), ";"):
            self.i += 1
        return [ast.While(test=_call("ts_do", test), body=body, orelse=[])]

    def try_(self, scope: str) -> list[ast.stmt]:
        self.i += 1
        body = self.block(scope) or [ast.Pass()]
        handlers: list[ast.excepthandler] = []
        final: list[ast.stmt] = []
        has_final = False
        if _word(self.peek(), "catch"):
            self.i += 1
            name = None
            if _punct(self.peek(), "("):
                close = self.close(self.i)
                first = self.toks[self.i + 1]
                if first.kind == "name" and close > self.i + 1:
                    name = _py_name(first.text)
                self.i = close + 1
            handlers.append(ast.ExceptHandler(type=_name("Exception"), name=name,
                                              body=self.block(scope) or [ast.Pass()]))
        if _word(self.peek(), "finally"):
            self.i += 1
            has_final = True
            final = self.block(scope) or [ast.Pass()]
        if not handlers and not has_final:
            raise _Unread("try without catch or finally")
        return [ast.Try(body=body, handlers=handlers, orelse=[], finalbody=final)]

    def switch(self, scope: str) -> list[ast.stmt]:
        self.i += 1
        value = self.paren_expression()
        if not _punct(self.peek(), "{"):
            raise _Unread("{ expected")
        close = self.close(self.i)
        self.i += 1
        cases: list[tuple[ast.expr | None, list[ast.stmt]]] = []
        self.loops += 1
        try:
            while self.i < close:
                with self.limit(close):
                    if _word(self.peek(), "case"):
                        self.i += 1
                        test: ast.expr | None = self.expression()
                    elif _word(self.peek(), "default"):
                        self.i += 1
                        test = None
                    else:
                        raise _Unread("case expected")
                    self.expect(":")
                body: list[ast.stmt] = []
                while self.i < close and not _word(self.toks[self.i], "case", "default"):
                    body.extend(self.recovering(close, scope))
                cases.append((test, body))
        finally:
            self.loops -= 1
        self.i = close + 1
        chain: list[ast.stmt] = next((body for test, body in cases if test is None), [])
        for test, body in reversed(cases):
            if test is not None:
                chain = [ast.If(test=_call("ts_case", test), body=body or [ast.Pass()],
                                orelse=chain)]
        return [ast.For(target=_name("ts_switch"), iter=ast.List(elts=[value], ctx=ast.Load()),
                        body=chain or [ast.Pass()], orelse=[], type_comment=None)]

    def return_(self, scope: str) -> list[ast.stmt]:
        self.i += 1
        value = None
        token = self.peek()
        if token is not None and not _punct(token, ";", "}") and not self.newline_before(self.i):
            value = self.expression()
        self.end_statement()
        if not self.functions_depth:
            raise _Unread("return outside a function")
        return [ast.Return(value=value)]

    def throw(self, scope: str) -> list[ast.stmt]:
        self.i += 1
        value = self.expression()
        self.end_statement()
        return [ast.Raise(exc=value, cause=None)]

    def jump(self, scope: str) -> list[ast.stmt]:
        word = self.take().text
        token = self.peek()
        if token is not None and token.kind == "name" and not self.newline_before(self.i):
            self.i += 1  # a label
        self.end_statement()
        if not self.loops:
            raise _Unread(f"{word} outside a loop")
        return [ast.Break() if word == "break" else ast.Continue()]

    def import_(self, scope: str) -> list[ast.stmt]:
        self.i += 1
        token = self.peek()
        if _word(token, "type") and not _word(self.peek(1), "from") and not _punct(
                self.peek(1), ","):
            while self.peek() is not None and not (self.peek().kind == "string"
                                                   and _word(self.toks[self.i - 1], "from")):
                self.i += 1
            self.take()
            self.end_statement()
            return []
        if token is not None and token.kind == "string":
            self.i += 1
            self.skip_attributes()
            self.end_statement()
            return [ast.Import(names=[ast.alias(name=_module(token.text), asname=None)])]
        default = namespace = None
        specifiers: list[tuple[str, str]] = []
        token = self.take()
        if token.kind == "name" and not _word(token, "from"):
            default = token.text
            if _punct(self.peek(), ","):
                self.i += 1
            token = self.take()
        if _punct(token, "*"):
            if not _word(self.take(), "as"):
                raise _Unread("as expected")
            namespace = self.take().text
            token = self.take()
        elif _punct(token, "{"):
            close = self.close(self.i - 1)
            k = self.i
            while k < close:
                if _word(self.toks[k], "type") and self.toks[k + 1].kind in ("name", "string") \
                        and not _punct(self.toks[k + 1], ","):
                    k = self.specifier_end(k + 1, close)
                    continue
                imported = self.toks[k].text
                local = imported
                k += 1
                if _word(self.toks[k] if k < close else None, "as"):
                    local = self.toks[k + 1].text
                    k += 2
                specifiers.append((imported, local))
                if k < close and _punct(self.toks[k], ","):
                    k += 1
            self.i = close + 1
            token = self.take()
        if not _word(token, "from"):
            raise _Unread("from expected")
        module = self.take()
        if module.kind != "string":
            raise _Unread("a module name expected")
        self.skip_attributes()
        self.end_statement()
        mod = _module(module.text)
        out: list[ast.stmt] = []
        if namespace is not None:
            out.append(ast.Import(names=[ast.alias(name=mod, asname=_py_name(namespace))]))
        aliases = []
        if default is not None:
            aliases.append(ast.alias(name="default", asname=_py_name(default)))
        for imported, local in specifiers:
            if module.text in FIXTURE_MODULES:
                if imported == "test":
                    self.test_names.add(local)
                if imported == "expect" and local != "expect":
                    self.expect_names.add(local)
            asname = None if local == imported else _py_name(local)
            aliases.append(ast.alias(name=_py_name(imported), asname=asname))
        if aliases:
            out.append(ast.ImportFrom(module=mod, names=aliases, level=0))
        return out

    def specifier_end(self, k: int, close: int) -> int:
        k += 1
        if k < close and _word(self.toks[k], "as"):
            k += 2
        if k < close and _punct(self.toks[k], ","):
            k += 1
        return k

    def skip_attributes(self) -> None:
        if _word(self.peek(), "with", "assert") and _punct(self.peek(1), "{"):
            self.i = self.close(self.i + 1) + 1

    def function_declaration(self, scope: str) -> ast.stmt:
        if _word(self.peek(), "async"):
            self.i += 1
        self.i += 1  # function
        if _punct(self.peek(), "*"):
            self.i += 1
        name = self.take()
        if name.kind != "name":
            raise _Unread("a function name expected")
        fn = self.function_rest()
        return self.function_def(_py_name(name.text), fn, scope)

    def function_def(self, name: str, fn: _Fn, scope: str) -> ast.FunctionDef:
        if scope != "function" and name.startswith("test"):
            name = "ts_" + name
        return _def(name, fn.params, self.function_body(fn))

    def function_body(self, fn: _Fn, scope: str = "function") -> list[ast.stmt]:
        if fn.block is None:
            assert fn.expr is not None
            return [ast.Expr(value=fn.expr)]
        open_, close = fn.block
        saved = (self.i, self.loops, self.functions_depth)
        self.i, self.loops = open_ + 1, 0
        if scope == "function":
            self.functions_depth += 1
        try:
            body = self.statements(close, scope)
        finally:
            self.i, self.loops, self.functions_depth = saved
        return body or [ast.Pass()]

    # Expression statements, and the tests they declare

    def expression_statement(self, scope: str) -> list[ast.stmt]:
        start = self.i
        awaited = _word(self.peek(), "await")
        value = self.expression()
        self.end_statement()
        line = self.toks[start].line
        if scope != "function":
            declared = self.declaration_call(value, scope, line)
            if declared is not None:
                return declared
        else:
            special = self.in_test_call(value)
            if special is not None:
                return special
        assigned = self.assigns.get(id(value))
        if assigned is not None and assigned[0] is value and _valid_target(assigned[2]):
            _, op, target, right = assigned
            if op == "=":
                return [ast.Assign(targets=[target], value=right, type_comment=None)]
            if op[:-1] in _ARITHMETIC:
                return [ast.AugAssign(target=target, op=_ARITHMETIC[op[:-1]](), value=right)]
        stepped = self.increments.get(id(value))
        if stepped is not None and stepped[0] is value and _valid_target(stepped[1]):
            op = ast.Add() if stepped[2] == "++" else ast.Sub()
            return [ast.AugAssign(target=stepped[1], op=op, value=_const(1))]
        if not awaited and _async_assertion(value):
            value = _call("nowait", value)
        return [ast.Expr(value=value)]

    def chain(self, func: ast.expr) -> list[str] | None:
        """``test.a.b`` as ``["a", "b"]`` when its root is a name of ``test``, else None."""
        parts: list[str] = []
        while isinstance(func, ast.Attribute):
            parts.append(func.attr)
            func = func.value
        if isinstance(func, ast.Name) and func.id in self.py_test_names():
            return parts[::-1]
        return None

    def py_test_names(self) -> set[str]:
        return {_py_name(n) for n in self.test_names}

    def declaration_call(self, value: ast.expr, scope: str, line: int) -> list[ast.stmt] | None:
        """A test, a group, a hook or a group-level modifier, at module or group level."""
        if not isinstance(value, ast.Call):
            return None
        parts = self.chain(value.func)
        if parts is None:
            return None
        args = value.args
        fn = self.fn(args[-1]) if args else None
        declares = not parts or parts == ["fail", "only"] or (
            len(parts) == 1 and parts[0] in _DECLARE)
        if fn is not None and len(args) >= 2 and declares:
            mark = _DECLARE.get(parts[0] if parts else None)
            title = args[0].value if (
                isinstance(args[0], ast.Constant) and isinstance(args[0].value, str)) else None
            own = [_mark(mark)] if mark else []
            return [self.declare_test(title, fn, own, line)]
        if fn is not None and parts[:1] == ["describe"] and set(parts[1:]) <= _GROUP:
            title_node = args[0] if len(args) >= 2 else None
            title = title_node.value if (
                isinstance(title_node, ast.Constant) and isinstance(title_node.value, str)
            ) else None
            group = _Group(title, title_node is not None)
            if {"skip", "fixme"} & set(parts[1:]):
                group.marks.append(_mark("skip"))
            self.groups.append(group)
            try:
                if fn.block is None:
                    assert fn.expr is not None
                    inner = self.declaration_call(fn.expr, "describe", line)
                    return inner if inner is not None else [ast.Expr(value=fn.expr)]
                return self.function_body(fn, "describe")
            finally:
                self.groups.pop()
        if fn is not None and len(parts) == 1 and parts[0] in _HOOKS:
            self.hooks[parts[0]] += 1
            count = self.hooks[parts[0]]
            name = f"ts_hook_{parts[0]}" + (f"_{count}" if count > 1 else "")
            decorator = _call(_dotted("pytest", "fixture"),
                              keywords=[ast.keyword(arg="autouse", value=_const(True))])
            return [_def(name, fn.fixtures, self.function_body(fn), [decorator])]
        if len(parts) == 1 and parts[0] in _SKIPS:
            self.add_mark(_mark(_SKIPS[parts[0]], list(args)))
            return []
        return None

    def declare_test(self, title: str | None, fn: _Fn, own: list[ast.expr], line: int
                     ) -> ast.FunctionDef:
        marks = [copy.deepcopy(m) for g in self.groups for m in g.marks] + own
        func = _def("test__pending", fn.fixtures, self.function_body(fn), marks)
        for group in self.groups:
            group.defs.append(func)
        path = tuple(g.title for g in self.groups if g.named)
        self.declared.append(_Declared(title, path, func, line))
        return func

    def add_mark(self, mark: ast.expr) -> None:
        group = self.groups[-1]
        for func in group.defs:
            func.decorator_list.append(copy.deepcopy(mark))
        group.marks.append(mark)

    def in_test_call(self, value: ast.expr) -> list[ast.stmt] | None:
        """``test.step`` with a callback, and ``test.skip``, ``test.fixme``, ``test.fail``."""
        if not isinstance(value, ast.Call):
            return None
        parts = self.chain(value.func)
        if parts == ["step"] and len(value.args) >= 2:
            fn = self.fn(value.args[1])
            if fn is not None:
                item = ast.withitem(context_expr=_call(value.func, value.args[0]),
                                    optional_vars=None)
                return [ast.With(items=[item], body=self.function_body(fn), type_comment=None)]
        if parts is not None and len(parts) == 1 and parts[0] in _SKIPS:
            call = _call(_dotted("pytest", _SKIPS[parts[0]]), *value.args[1:])
            if not value.args:
                return [ast.Expr(value=call)]
            return [ast.If(test=value.args[0], body=[ast.Expr(value=call)], orelse=[])]
        return None

    def fn(self, node: ast.expr | None) -> _Fn | None:
        found = self.functions.get(id(node)) if node is not None else None
        return found if found is not None and found.node is node else None

    # Expressions

    def expression(self) -> ast.expr:
        first = self.assignment()
        if not _punct(self.peek(), ","):
            return first
        items = [first]
        while _punct(self.peek(), ","):
            self.i += 1
            items.append(self.assignment())
        return ast.Tuple(elts=items, ctx=ast.Load())

    def assignment(self) -> ast.expr:
        if self.arrow_ahead():
            return self.arrow()
        left = self.conditional()
        token = self.peek()
        if token is not None and token.kind == "punct" and token.text in _ASSIGN:
            self.i += 1
            right = self.assignment()
            node = _call("ts_assign", left, _const(token.text), right)
            self.assigns[id(node)] = (node, token.text, left, right)
            return node
        return left

    def conditional(self) -> ast.expr:
        test = self.binary(0)
        if not _punct(self.peek(), "?"):
            return test
        self.i += 1
        body = self.assignment()
        self.expect(":")
        orelse = self.assignment()
        return ast.IfExp(test=test, body=body, orelse=orelse)

    def binary_op(self) -> str | None:
        token = self.peek()
        if token is None:
            return None
        if token.kind == "punct" and token.text in _BINARY:
            return token.text
        if _word(token, "in", "instanceof"):
            return token.text
        return None

    def binary(self, floor: int) -> ast.expr:
        left = self.unary()
        while True:
            if _word(self.peek(), "as", "satisfies") and floor < 8:
                self.i += 1
                self.skip_type(_TYPE_STOPS)
                continue
            op = self.binary_op()
            if op is None or _BINARY[op] <= floor:
                return left
            self.i += 1
            right = self.binary(_BINARY[op] - 1 if op == "**" else _BINARY[op])
            left = self.combine(op, left, right)

    @staticmethod
    def combine(op: str, left: ast.expr, right: ast.expr) -> ast.expr:
        if op == "&&":
            return ast.BoolOp(op=ast.And(), values=[left, right])
        if op == "||":
            return ast.BoolOp(op=ast.Or(), values=[left, right])
        if op == "??":
            return _call("ts_nullish", left, right)
        if op == "instanceof":
            return _call("ts_instanceof", left, right)
        if op in _COMPARE:
            return ast.Compare(left=left, ops=[_COMPARE[op]()], comparators=[right])
        return ast.BinOp(left=left, op=_ARITHMETIC[op](), right=right)

    def unary(self) -> ast.expr:
        token = self.peek()
        if _punct(token, "!", "-", "+", "~"):
            self.i += 1
            op = {"!": ast.Not, "-": ast.USub, "+": ast.UAdd, "~": ast.Invert}[token.text]()
            return ast.UnaryOp(op=op, operand=self.unary())
        if _word(token, "typeof", "void", "delete"):
            self.i += 1
            return _call(f"ts_{token.text}", self.unary())
        if _word(token, "await"):
            self.i += 1
            return self.unary()
        if _punct(token, "++", "--"):
            self.i += 1
            return self.stepped(self.unary(), token.text)
        if _punct(token, "<"):  # a type assertion, <Locator>value
            end = self.type_arguments_end(self.i)
            if end is not None:
                self.i = end
                return self.unary()
        value = self.call()
        token = self.peek()
        if _punct(token, "++", "--") and not self.newline_before(self.i):
            self.i += 1
            return self.stepped(value, token.text)
        return value

    def stepped(self, target: ast.expr, op: str) -> ast.Call:
        node = _call("ts_increment" if op == "++" else "ts_decrement", target)
        self.increments[id(node)] = (node, target, op)
        return node

    def call(self) -> ast.expr:
        if _word(self.peek(), "new") and not _punct(self.peek(1), "."):
            self.i += 1
            callee = self.tail(self.primary(), calls=False)
            args, keywords = self.arguments() if _punct(self.peek(), "(") else ([], [])
            if isinstance(callee, ast.Name) and callee.id == "RegExp":
                value: ast.expr = _call(_dotted("re", "compile"), *args, keywords=keywords)
            else:
                value = _call("ts_new", callee, *args, keywords=keywords)
            return self.tail(value, calls=True)
        return self.tail(self.primary(), calls=True)

    def tail(self, value: ast.expr, *, calls: bool) -> ast.expr:
        while True:
            token = self.peek()
            if _punct(token, ".", "?."):
                following = self.peek(1)
                if _punct(token, "?.") and _punct(following, "(", "["):
                    self.i += 1
                    continue
                if following is None or following.kind not in ("name", "private"):
                    raise _Unread("a member name expected")
                self.i += 2
                value = self.member(value, following.text, calls)
            elif _punct(token, "["):
                close = self.close(self.i)
                self.i += 1
                with self.limit(close):
                    index = self.expression()
                    if self.i != close:
                        raise _Unread("] expected")
                self.i = close + 1
                value = ast.Subscript(value=value, slice=index, ctx=ast.Load())
            elif _punct(token, "(") and calls:
                args, keywords = self.arguments()
                value = self.called(value, args, keywords)
            elif token is not None and token.kind == "template" and token.text.startswith("`"):
                self.i += 1
                value = _call(value, self.template(token))
            elif _punct(token, "!") and not self.newline_before(self.i):
                self.i += 1  # TypeScript's non-null assertion
            elif _punct(token, "<"):  # type arguments, page.evaluate<number>(...)
                end = self.type_arguments_end(self.i)
                following = self.toks[end] if end is not None and end < self.end else None
                if following is None or not (_punct(following, "(") or (
                        following.kind == "template" and following.text.startswith("`"))):
                    return value
                self.i = end
            else:
                return value

    def type_arguments_end(self, k: int) -> int | None:
        """The index after the ``>`` that closes the type arguments opening with the ``<`` at
        ``k``; None when the tokens there cannot be type arguments."""
        angle = 0
        j = k
        while j < self.end:
            token = self.toks[j]
            if _punct(token, "(", "[", "{"):
                j = self.close(j) + 1
                continue
            if _punct(token, "<"):
                angle += 1
            elif _punct(token, ">", ">>", ">>>"):
                angle -= len(token.text)
                if angle < 0:
                    return None
                if angle == 0:
                    return j + 1
            elif token.kind not in ("name", "string", "number") and not _punct(
                    token, ",", ".", "|", "&", "=>", "?", ":", "...", "-"):
                return None
            j += 1
        return None

    def member(self, value: ast.expr, name: str, calls: bool) -> ast.expr:
        if isinstance(value, ast.Name):
            if value.id == "expect" and name == "soft":
                return _name("expect")
            if value.id in self.py_test_names() and name == "expect":
                return _name("expect")
        if name in _PROPERTIES and calls and _punct(self.peek(), "(") and _punct(self.peek(1), ")"):
            self.i += 2
            return _attr(value, _PROPERTIES[name])
        if (isinstance(value, ast.Attribute) and value.attr == "not_"
                and _expect_call(value.value) and name.startswith("to")):
            return _attr(value.value, "not_" + _py_name(_snake(name)))  # not_to_be_visible
        if name.startswith("#"):
            return _attr(value, "_private_" + _py_name(name[1:]))
        return _attr(value, _py_name(_snake(name)))

    @staticmethod
    def called(func: ast.expr, args: list[ast.expr], keywords: list[ast.keyword]) -> ast.expr:
        if isinstance(func, ast.Name) and func.id == "RegExp":
            return _call(_dotted("re", "compile"), *args, keywords=keywords)
        if isinstance(func, ast.Name) and func.id == "reEscape":
            return _call(_dotted("re", "escape"), *args, keywords=keywords)
        return _call(func, *args, keywords=keywords)

    def arguments(self) -> tuple[list[ast.expr], list[ast.keyword]]:
        close = self.close(self.i)
        self.i += 1
        args: list[ast.expr] = []
        with self.limit(close):
            while self.i < close:
                if _punct(self.peek(), "..."):
                    self.i += 1
                    args.append(ast.Starred(value=self.assignment(), ctx=ast.Load()))
                else:
                    args.append(self.assignment())
                if self.i < close:
                    self.expect(",")
        self.i = close + 1
        keywords = self.keywords(args[-1]) if args else None
        if keywords is None:
            return args, []
        return args[:-1], keywords

    def keywords(self, node: ast.expr) -> list[ast.keyword] | None:
        """An object literal's entries as keyword arguments, when its keys allow."""
        found = self.objects.get(id(node))
        if found is None or found[0] is not node:
            return None
        keywords = []
        for entry in found[1]:
            if entry is None:
                return None
            key, value = entry
            name = _snake(key)
            if not name.isidentifier():
                return None
            keywords.append(ast.keyword(arg=_py_name(name), value=value))
        if len({k.arg for k in keywords}) != len(keywords):
            return None
        return keywords

    def primary(self) -> ast.expr:
        token = self.take()
        if token.kind == "number":
            return _const(_number(token.text))
        if token.kind == "string":
            return _const(token.text)
        if token.kind == "template":
            return self.template(token)
        if token.kind == "regex":
            return _regex(token.text)
        if token.kind == "private":
            return _name("_private_" + _py_name(token.text[1:]))
        if token.kind == "name":
            word = token.text
            if word == "function" or (word == "async" and _word(self.peek(), "function")):
                if word == "async":
                    self.i += 1
                if _punct(self.peek(), "*"):
                    self.i += 1
                if self.peek() is not None and self.peek().kind == "name":
                    self.i += 1
                return self.function_rest().node
            if word == "class":
                start = self.i - 1
                k = start
                while not _punct(self.toks[k], "{"):
                    k += 1
                self.i = self.close(k) + 1
                return _call("ts_class", _const(_text(self.toks[start:self.i])))
            if word in ("true", "false"):
                return _const(word == "true")
            if word == "null":
                return _const(None)
            if word == "import":
                if _punct(self.peek(), "."):
                    self.i += 2
                    return _name("ts_import_meta")
                return _name("ts_import")
            if word in self.expect_names:
                return _name("expect")
            return _name(_py_name(word))
        if _punct(token, "("):
            close = self.close(self.i - 1)
            with self.limit(close):
                value = self.expression()
                if self.i != close:
                    raise _Unread(") expected")
            self.i = close + 1
            return value
        if _punct(token, "["):
            return self.array()
        if _punct(token, "{"):
            return self.object_()
        raise _Unread(f"{token.text} does not start an expression")

    def template(self, first: Token) -> ast.expr:
        pieces = [_cook(first.text)]
        values: list[ast.expr] = []
        token = first
        while token.text.endswith("${"):
            values.append(self.expression())
            token = self.take()
            if token.kind != "template" or not token.text.startswith("}"):
                raise _Unread("the end of a substitution expected")
            pieces.append(_cook(token.text))
        if not values:
            return _const(pieces[0])
        args: list[ast.expr] = [_const(pieces[0])]
        for value, piece in zip(values, pieces[1:], strict=True):
            args += [value, _const(piece)]
        return _call("ts_template", *args)

    def array(self) -> ast.expr:
        close = self.close(self.i - 1)
        items: list[ast.expr] = []
        with self.limit(close):
            while self.i < close:
                if _punct(self.peek(), ","):
                    self.i += 1
                    items.append(_const(None))
                    continue
                if _punct(self.peek(), "..."):
                    self.i += 1
                    items.append(ast.Starred(value=self.assignment(), ctx=ast.Load()))
                else:
                    items.append(self.assignment())
                if self.i < close:
                    self.expect(",")
        self.i = close + 1
        return ast.List(elts=items, ctx=ast.Load())

    def object_(self) -> ast.expr:
        close = self.close(self.i - 1)
        keys: list[ast.expr | None] = []
        values: list[ast.expr] = []
        entries: list[tuple[str, ast.expr] | None] = []
        with self.limit(close):
            while self.i < close:
                token = self.take()
                if _punct(token, "..."):
                    keys.append(None)
                    values.append(self.assignment())
                    entries.append(None)
                else:
                    method = False
                    if (_word(token, "get", "set", "async") or _punct(token, "*")) and not _punct(
                            self.peek(), ",", ":", "(", "}", "="):
                        method = True
                        if _punct(self.peek(), "*"):
                            self.i += 1
                        token = self.take()
                    if _punct(token, "["):
                        end = self.close(self.i - 1)
                        with self.limit(end):
                            key_node: ast.expr = self.assignment()
                        self.i = end + 1
                        key_text = None
                    elif token.kind in ("name", "string", "number", "private"):
                        key_text = token.text
                        key_node = _const(token.text)
                    else:
                        raise _Unread("a property name expected")
                    if method or _punct(self.peek(), "("):
                        value: ast.expr = self.function_rest().node
                        key_text = None
                    elif _punct(self.peek(), ":"):
                        self.i += 1
                        value = self.assignment()
                    elif token.kind == "name":
                        value = _name(_py_name(token.text))
                        if _punct(self.peek(), "="):
                            self.i += 1
                            self.assignment()
                    else:
                        raise _Unread(": expected")
                    keys.append(key_node)
                    values.append(value)
                    entries.append(None if key_text is None else (key_text, value))
                if self.i < close:
                    self.expect(",")
        self.i = close + 1
        node = ast.Dict(keys=keys, values=values)
        self.objects[id(node)] = (node, entries)
        return node

    # Functions

    def arrow_ahead(self) -> bool:
        k = self.i
        if _word(self.peek(), "async") and not self.newline_before(k + 1):
            following = self.peek(1)
            if following is not None and (following.kind == "name" or _punct(following, "(", "<")):
                k += 1
        if k < self.end and _punct(self.toks[k], "<"):  # type parameters, <T,>(x: T) => x
            k = self.type_arguments_end(k) or k
        token = self.toks[k] if k < self.end else None
        if token is None:
            return False
        if token.kind == "name" and not keyword.iskeyword(token.text):
            return k + 1 < self.end and _punct(self.toks[k + 1], "=>")
        if _punct(token, "("):
            close = self.close(k)
            if close + 1 >= self.end:
                return False
            if _punct(self.toks[close + 1], "=>"):
                return True
            if _punct(self.toks[close + 1], ":"):
                depth = 0
                for j in range(close + 2, self.end):
                    if _closes(self.toks[j]):
                        if depth == 0:
                            return False
                        depth -= 1
                    if _opens(self.toks[j]):
                        depth += 1
                    if depth == 0 and _punct(self.toks[j], "=>"):
                        return True
                    if depth == 0 and _punct(self.toks[j], ";", ","):
                        return False
        return False

    def arrow(self) -> ast.Lambda:
        if _word(self.peek(), "async") and not _punct(self.peek(1), "=>"):
            self.i += 1
        if _punct(self.peek(), "<"):
            self.i = self.type_arguments_end(self.i) or self.i
        token = self.take()
        if token.kind == "name":
            params = fixtures = [_py_name(token.text)]
        else:
            close = self.close(self.i - 1)
            params, fixtures = self.params(self.i - 1, close)
            self.i = close + 1
            if _punct(self.peek(), ":"):
                self.i += 1
                self.skip_type(frozenset({"=>"}))
        self.expect("=>")
        return self.function_end(params, fixtures).node

    def function_rest(self) -> _Fn:
        """A function from its parameter list on: ``(params) [: type] { body }``."""
        if _punct(self.peek(), "<"):
            self.skip_type(frozenset({"("}))
        if not _punct(self.peek(), "("):
            raise _Unread("( expected")
        close = self.close(self.i)
        params, fixtures = self.params(self.i, close)
        self.i = close + 1
        if _punct(self.peek(), ":"):
            self.i += 1
            self.skip_type(frozenset({"{"}))
        if not _punct(self.peek(), "{"):
            raise _Unread("a function body expected")
        return self.function_end(params, fixtures)

    def function_end(self, params: list[str], fixtures: list[str]) -> _Fn:
        """The body of a function, after its parameters (and =>, for an arrow)."""
        if _punct(self.peek(), "{"):
            open_ = self.i
            close = self.close(open_)
            self.i = close + 1
            body: ast.expr = _call("ts_block", _const(_text(self.toks[open_ + 1:close])))
            block: tuple[int, int] | None = (open_, close)
            expr = None
        else:
            body = expr = self.assignment()
            block = None
        node = ast.Lambda(args=_arguments(params), body=body)
        fn = _Fn(node, params, fixtures, block, expr)
        self.functions[id(node)] = fn
        return fn

    def params(self, open_: int, close: int) -> tuple[list[str], list[str]]:
        """The names a parameter list binds, and the fixtures a test's callback asks for."""
        params: list[str] = []
        fixtures: list[str] = []
        for index, (start, stop) in enumerate(_split(self.toks, open_ + 1, close)):
            if start >= stop:
                continue
            if _word(self.toks[start], "this") and _punct(self.toks[start + 1], ":"):
                continue
            names, keys = self.bound(start, stop)
            params += names
            fixtures += keys if index == 0 and keys is not None else names
        return _unique(map(_py_name, params)), _unique(map(_py_name, fixtures))

    def bound(self, start: int, stop: int) -> tuple[list[str], list[str] | None]:
        """The names the binding pattern at ``start`` binds, and an object pattern's keys."""
        k = start
        if k < stop and _punct(self.toks[k], "..."):
            k += 1
        if k >= stop:
            raise _Unread("a binding expected")
        token = self.toks[k]
        if _punct(token, "{"):
            close = self.close(k)
            names: list[str] = []
            keys: list[str] = []
            for s, e in _split(self.toks, k + 1, close):
                if s >= e:
                    continue
                first = self.toks[s]
                if _punct(first, "..."):
                    names += self.bound(s, e)[0]
                    continue
                if _punct(first, "["):
                    colon = self.close(s) + 1
                else:
                    keys.append(first.text)
                    colon = s + 1
                if colon < e and _punct(self.toks[colon], ":"):
                    names += self.bound(colon + 1, e)[0]
                elif not _punct(first, "["):
                    names.append(first.text)
            return names, keys
        if _punct(token, "["):
            close = self.close(k)
            names = []
            for s, e in _split(self.toks, k + 1, close):
                if s < e:
                    names += self.bound(s, e)[0]
            return names, None
        if token.kind == "name":
            return [token.text], None
        raise _Unread("a binding of this form")

    def skip_type(self, stops: frozenset[str]) -> None:
        """Pass over a TypeScript type, up to one of ``stops`` outside any bracket."""
        depth = angle = 0
        first = True
        while self.i < self.end:
            token = self.toks[self.i]
            if depth == 0 and angle == 0:
                if token.kind in ("punct", "name") and token.text in stops and not first:
                    return
                if _punct(token, "{") and "{" in stops:
                    return
                if (not first and self.newline_before(self.i) and _can_end(self.toks[self.i - 1])
                        and _can_start(token) and not _punct(token, "|", "&")):
                    return
            if _closes(token):
                if depth == 0:
                    return
                depth -= 1
            elif _opens(token):
                depth += 1
            elif _punct(token, "<"):
                angle += 1
            elif _punct(token, ">", ">>", ">>>") and angle:
                angle = max(0, angle - len(token.text))
            self.i += 1
            first = False


def _split(tokens: list[Token], start: int, end: int) -> list[tuple[int, int]]:
    """The comma-separated parts of ``tokens[start:end]`` at their own depth, as ranges."""
    parts = []
    depth = 0
    part = start
    for k in range(start, end):
        if _closes(tokens[k]):
            depth -= 1
        if _opens(tokens[k]):
            depth += 1
        if depth == 0 and _punct(tokens[k], ","):
            parts.append((part, k))
            part = k + 1
    if part < end:
        parts.append((part, end))
    return parts


def _is_test_name(title: str) -> bool:
    return title.isidentifier() and title.startswith("test") and not keyword.iskeyword(title)
