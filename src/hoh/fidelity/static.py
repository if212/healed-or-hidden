"""Static checks on a TypeScript spec before it runs through the fixture.

The fidelity check runs specs, the ports and what Playwright's healer makes of them,
through the fixture package ``pwfix``, whose ``page`` is the one open tab. Before a
spec runs, ``check_spec`` looks for what would step around that fixture: an import of
anything but ``pwfix``, a test callback that asks for a fixture other than ``page``,
``test.extend``, and code that names a way to another browser, another page or the
Node process. It is a tripwire for code a test writer or a healer would plausibly
write, not a sandbox: it reads tokens, never runs the source, and code built to slip
past it can.

The checks read tokens, so comments, string literals, regex literals and the text of
template literals never count as code, while the code inside ``${...}`` does. A source
that cannot be split into tokens is one problem.
"""

from __future__ import annotations

import bisect
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import NoReturn

PACKAGE = "pwfix"

# Names that may not appear as code at all: ways to another browser, context, page or
# connection, and to the Node process.
FORBIDDEN_NAMES = frozenset(
    {
        "connectOverCDP",
        "launch",
        "launchPersistentContext",
        "newPage",
        "newContext",
        "newCDPSession",
        "chromium",
        "firefox",
        "webkit",
        "process",
        "globalThis",
        "child_process",
        "__proto__",
    }
)
# Names that may not be called, with ( or ?.( or as a template tag.
FORBIDDEN_CALLS = frozenset({"connect", "eval", "Function", "require"})


class SpecSyntaxError(ValueError):
    """The source cannot be split into tokens."""


@dataclass(frozen=True)
class Token:
    """One token of a spec.

    ``kind`` is name, private, punct, number, string, template or regex. ``text`` is
    the token as written, except that a string is its value and a name has its escapes
    resolved. A template literal is a token per piece, ``\\`a${``, ``}b${`` and
    ``}c\\```, with the tokens of each substitution's code between them.
    """

    kind: str
    text: str
    line: int


_PUNCTUATORS = sorted(
    (
        ">>>= ... === !== **= <<= >>= >>> &&= ||= ??= => == != <= >= && || ?? ?. ++ -- "
        "+= -= *= /= %= &= |= ^= ** << >> { } ( ) [ ] ; , < > + - * / % & | ^ ! ~ ? : = . @"
    ).split(),
    key=len,
    reverse=True,
)
_LINE_TERMINATORS = "\n\r\u2028\u2029"
_LINE_BREAK = re.compile(r"\r\n|[\n\r\u2028\u2029]")
_NUMBER = re.compile(
    r"0[xX][0-9a-fA-F_]+n?|0[oO][0-7_]+n?|0[bB][01_]+n?"
    r"|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d[\d_]*)?n?"
)
_NAME_ESCAPE = re.compile(r"\\u(?:([0-9a-fA-F]{4})|\{([0-9a-fA-F]+)\})")
_STRING_ESCAPE = re.compile(r"\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4})|u\{([0-9a-fA-F]+)\})")
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
# After these keywords a slash starts a regular expression; after any other name it divides.
_BEFORE_REGEX = frozenset(
    "await case delete do else extends in instanceof new of return throw typeof void yield".split()
)
# A slash after the ) of these starts a regular expression: if (a) /re/.test(b).
_CONTROL = frozenset({"if", "while", "for", "with"})


def _is_space(ch: str) -> bool:
    return ch in "\t\v\f \u00a0\ufeff" or unicodedata.category(ch) == "Zs"


def _starts_name(ch: str) -> bool:
    if ch.isascii():
        return ch.isalpha() or ch in "$_"
    return ch.isidentifier()


def _continues_name(ch: str) -> bool:
    if ch.isascii():
        return ch.isalnum() or ch in "$_"
    return ch in "\u200c\u200d" or ("a" + ch).isidentifier()


def tokenize(source: str) -> list[Token]:
    """The tokens of ``source``, without white space and comments; SpecSyntaxError if none."""
    return _Tokenizer(source).run()


class _Tokenizer:
    def __init__(self, source: str) -> None:
        self.source = source
        self.pos = 0
        self.tokens: list[Token] = []
        self.line_starts = [0] + [match.end() for match in _LINE_BREAK.finditer(source)]
        # For each open brace: None, or for a substitution the line its template started on.
        self.braces: list[int | None] = []
        # For each open parenthesis: whether it follows if, while, for or with.
        self.parens: list[bool] = []
        self.regex_ok = True

    def run(self) -> list[Token]:
        if self.source.startswith("#!"):
            self.pos = self._line_end(0)
        while True:
            self._skip()
            if self.pos >= len(self.source):
                break
            self._token()
        open_templates = [line for line in self.braces if line is not None]
        if open_templates:
            raise SpecSyntaxError(
                f"Unterminated template literal starting on line {open_templates[-1]}."
            )
        return self.tokens

    def _line(self, offset: int) -> int:
        return bisect.bisect_right(self.line_starts, offset)

    def _fail(self, what: str, offset: int) -> NoReturn:
        raise SpecSyntaxError(f"{what} on line {self._line(offset)}.")

    def _unexpected(self, offset: int) -> NoReturn:
        ch = self.source[offset]
        shown = f'"{ch}"' if ch.isprintable() and not ch.isspace() else f"U+{ord(ch):04X}"
        self._fail(f"Unexpected character {shown}", offset)

    def _line_end(self, offset: int) -> int:
        while offset < len(self.source) and self.source[offset] not in _LINE_TERMINATORS:
            offset += 1
        return offset

    def _skip(self) -> None:
        """Skip white space, line breaks and comments."""
        source = self.source
        while self.pos < len(source):
            ch = source[self.pos]
            if ch in _LINE_TERMINATORS or _is_space(ch):
                self.pos += 1
            elif source.startswith("//", self.pos):
                self.pos = self._line_end(self.pos)
            elif source.startswith("/*", self.pos):
                end = source.find("*/", self.pos + 2)
                if end < 0:
                    self._fail("Unterminated comment starting", self.pos)
                self.pos = end + 2
            else:
                return

    def _token(self) -> None:
        source, start = self.source, self.pos
        ch = source[start]
        following = source[start + 1 : start + 2]
        if ch in "'\"":
            self._string()
        elif ch == "`":
            self.pos += 1
            self._template(start, self._line(start))
        elif ch == "}" and self.braces and self.braces[-1] is not None:
            line = self.braces.pop()
            assert line is not None
            self.pos += 1
            self._template(start, line)
        elif ch == "/" and self.regex_ok:
            self._regex()
        elif _is_digit(ch) or (ch == "." and _is_digit(following)):
            match = _NUMBER.match(source, start)
            assert match is not None
            self.pos = match.end()
            self._emit("number", match.group(), start)
        elif ch == "#" and (following == "\\" or (following != "" and _starts_name(following))):
            self.pos += 1
            self._emit("private", "#" + self._name(start), start)
        elif ch == "\\" or _starts_name(ch):
            self._emit("name", self._name(start), start)
        else:
            self._punctuator()

    def _emit(self, kind: str, text: str, offset: int) -> None:
        previous = self.tokens[-1] if self.tokens else None
        after_dot = _is_punct(previous, ".", "?.")
        if kind == "name":
            self.regex_ok = text in _BEFORE_REGEX and not after_dot
        elif kind == "template":
            self.regex_ok = text.endswith("${")
        elif kind == "punct":
            if text == "(":
                control = _is_name_token(previous) and previous.text in _CONTROL
                self.parens.append(control and not _after_dot(self.tokens, len(self.tokens) - 1))
            elif text == "{":
                self.braces.append(None)
            elif text == "}" and self.braces:
                self.braces.pop()
            if text == ")":
                self.regex_ok = self.parens.pop() if self.parens else False
            else:
                self.regex_ok = text not in ("]", "++", "--")
        else:
            self.regex_ok = False
        self.tokens.append(Token(kind, text, self._line(offset)))

    def _name(self, start: int) -> str:
        """Read a name from ``self.pos``, resolving \\u escapes."""
        source = self.source
        out: list[str] = []
        while self.pos < len(source):
            ch, end = source[self.pos], self.pos + 1
            if ch == "\\":
                match = _NAME_ESCAPE.match(source, self.pos)
                if match is None or int(match.group(1) or match.group(2), 16) > 0x10FFFF:
                    self._unexpected(self.pos)
                ch, end = chr(int(match.group(1) or match.group(2), 16)), match.end()
            if not (_continues_name(ch) if out else _starts_name(ch)):
                if end - self.pos > 1:
                    # An escape that does not stand for a name character.
                    self._unexpected(self.pos)
                break
            out.append(ch)
            self.pos = end
        if not out:
            self._unexpected(start)
        return "".join(out)

    def _string(self) -> None:
        source, start = self.source, self.pos
        quote = source[start]
        out: list[str] = []
        i = start + 1
        while True:
            if i >= len(source) or source[i] in "\n\r":
                self._fail("Unterminated string starting", start)
            ch = source[i]
            if ch == quote:
                break
            if ch != "\\":
                out.append(ch)
                i += 1
                continue
            if i + 1 >= len(source):
                self._fail("Unterminated string starting", start)
            escaped = source[i + 1]
            match = _STRING_ESCAPE.match(source, i)
            code = int(next(group for group in match.groups() if group), 16) if match else None
            if escaped == "\r" and source.startswith("\n", i + 2):
                i += 3
            elif escaped in _LINE_TERMINATORS:
                i += 2
            elif match is not None and code is not None and code <= 0x10FFFF:
                out.append(chr(code))
                i = match.end()
            else:
                out.append(_SIMPLE_ESCAPES.get(escaped, escaped))
                i += 2
        self.pos = i + 1
        self._emit("string", "".join(out), start)

    def _template(self, start: int, line: int) -> None:
        """Read a piece of a template literal that began on ``line``, from after its ` or }."""
        source = self.source
        i = self.pos
        while True:
            if i >= len(source):
                raise SpecSyntaxError(f"Unterminated template literal starting on line {line}.")
            if source[i] == "\\":
                i += 2
            elif source[i] == "`":
                self.pos = i + 1
                self._emit("template", source[start : self.pos], start)
                return
            elif source.startswith("${", i):
                self.pos = i + 2
                self._emit("template", source[start : self.pos], start)
                self.braces.append(line)
                return
            else:
                i += 1

    def _regex(self) -> None:
        source, start = self.source, self.pos
        i = start + 1
        in_class = False
        while True:
            if i >= len(source) or source[i] in _LINE_TERMINATORS:
                self._fail("Unterminated regular expression starting", start)
            ch = source[i]
            if ch == "\\":
                if i + 1 >= len(source) or source[i + 1] in _LINE_TERMINATORS:
                    self._fail("Unterminated regular expression starting", start)
                i += 2
                continue
            i += 1
            if ch == "[":
                in_class = True
            elif ch == "]":
                in_class = False
            elif ch == "/" and not in_class:
                break
        while i < len(source) and _continues_name(source[i]):
            i += 1
        self.pos = i
        self._emit("regex", source[start:i], start)

    def _punctuator(self) -> None:
        source, start = self.source, self.pos
        for punct in _PUNCTUATORS:
            if not source.startswith(punct, start):
                continue
            # a?.5:1 is a conditional: ?. never comes before a digit.
            if punct == "?." and _is_digit(source[start + 2 : start + 3]):
                continue
            self.pos = start + len(punct)
            self._emit("punct", punct, start)
            return
        self._unexpected(start)


def _is_digit(ch: str) -> bool:
    return ch != "" and ch.isascii() and ch.isdigit()


# The checks


def check_spec(source: str) -> list[str]:
    """What in ``source`` would step around the fixture, one short sentence each, by line.

    An empty list means the spec passes.
    """
    try:
        tokens = tokenize(source)
    except SpecSyntaxError as exc:
        return [str(exc)]
    problems: list[tuple[int, str]] = []
    tests = {"test"}  # and the names test is imported under
    for index in range(len(tokens)):
        if _is_keyword(tokens, index, "import"):
            _check_import(tokens, index, problems, tests)
        elif _is_keyword(tokens, index, "export"):
            _check_export(tokens, index, problems)
    for index, token in enumerate(tokens):
        if token.kind != "name":
            continue
        line = token.line
        if token.text in FORBIDDEN_NAMES:
            problems.append((line, f'"{token.text}" appears as code on line {line}.'))
        if token.text in FORBIDDEN_CALLS and _is_called(tokens, index):
            problems.append((line, f'"{token.text}" is called on line {line}.'))
        if token.text in tests and not _after_dot(tokens, index):
            _check_test_call(tokens, index, problems)
    messages: list[str] = []
    for _, message in sorted(problems, key=lambda problem: problem[0]):
        if message not in messages:
            messages.append(message)
    return messages


def _at(tokens: list[Token], index: int) -> Token | None:
    return tokens[index] if 0 <= index < len(tokens) else None


def _is_punct(token: Token | None, *texts: str) -> bool:
    return token is not None and token.kind == "punct" and token.text in texts


def _is_name(token: Token | None, text: str) -> bool:
    return token is not None and token.kind == "name" and token.text == text


def _is_name_token(token: Token | None) -> bool:
    return token is not None and token.kind == "name"


def _after_dot(tokens: list[Token], index: int) -> bool:
    return _is_punct(_at(tokens, index - 1), ".", "?.")


def _is_keyword(tokens: list[Token], index: int, word: str) -> bool:
    return _is_name(tokens[index], word) and not _after_dot(tokens, index)


def _opens(token: Token) -> bool:
    if token.kind == "template":
        return token.text.endswith("${")
    return token.kind == "punct" and token.text in ("(", "[", "{")


def _closes(token: Token) -> bool:
    if token.kind == "template":
        return token.text.startswith("}")
    return token.kind == "punct" and token.text in (")", "]", "}")


def _closing(tokens: list[Token], index: int) -> int | None:
    """The index of the token that closes the bracket at ``index``."""
    depth = 0
    for k in range(index, len(tokens)):
        if _closes(tokens[k]):
            depth -= 1
            if depth == 0:
                return k
        if _opens(tokens[k]):
            depth += 1
    return None


def _split(tokens: list[Token], start: int, end: int) -> list[tuple[int, int]]:
    """The comma-separated parts of ``tokens[start:end]``, as ranges, at their own depth."""
    parts = []
    depth = 0
    part = start
    for k in range(start, end):
        if _closes(tokens[k]):
            depth -= 1
        if _opens(tokens[k]):
            depth += 1
        if depth == 0 and _is_punct(tokens[k], ","):
            parts.append((part, k))
            part = k + 1
    if part < end:
        parts.append((part, end))
    return parts


def _check_import(
    tokens: list[Token], index: int, problems: list[tuple[int, str]], tests: set[str]
) -> None:
    line = tokens[index].line
    following = _at(tokens, index + 1)
    if _is_punct(following, "("):
        problems.append((line, f"Dynamic import() on line {line}."))
        return
    if _is_punct(following, ".") and _is_name(_at(tokens, index + 2), "meta"):
        problems.append((line, f"import.meta on line {line}."))
        return
    module, clause = _import_clause(tokens, index + 1)
    if module is not None and module != PACKAGE:
        named = json.dumps(module, ensure_ascii=False)
        problems.append(
            (line, f'Import of {named} on line {line}; only "{PACKAGE}" may be imported.')
        )
        return
    specifiers = None if clause is None else _specifiers(tokens, *clause)
    if specifiers is None:
        problems.append(
            (line, f'Import on line {line} is not of the form import {{ ... }} from "{PACKAGE}".')
        )
        return
    tests.update(local for imported, local in specifiers if imported == "test")


def _import_clause(tokens: list[Token], start: int) -> tuple[str | None, tuple[int, int] | None]:
    """The module an import names, and the range of the clause before its from.

    ``import "m"`` has no clause; a declaration of a shape not known here names no module.
    """
    first = _at(tokens, start)
    if first is not None and first.kind == "string":
        return first.text, None
    k = start
    while k < len(tokens):
        token = tokens[k]
        following = _at(tokens, k + 1)
        if _is_name(token, "from") and following is not None and following.kind == "string":
            return following.text, (start, k)
        if _is_punct(token, "{"):
            close = _closing(tokens, k)
            if close is None:
                break
            k = close + 1
        elif token.kind == "name" or _is_punct(token, "*", ","):
            k += 1
        else:
            break
    return None, None


def _specifiers(tokens: list[Token], start: int, end: int) -> list[tuple[str, str]] | None:
    """The (imported, local) names of a clause that is exactly ``{ a, b as c }``, or None."""
    if not _is_punct(_at(tokens, start), "{") or _closing(tokens, start) != end - 1:
        return None
    specifiers = []
    k = start + 1
    while k < end - 1:
        if tokens[k].kind != "name":
            return None
        imported = local = tokens[k].text
        k += 1
        if _is_name(tokens[k], "as") and k + 1 < end - 1 and tokens[k + 1].kind == "name":
            local = tokens[k + 1].text
            k += 2
        specifiers.append((imported, local))
        if k < end - 1:
            if not _is_punct(tokens[k], ","):
                return None
            k += 1
    return specifiers


def _check_export(tokens: list[Token], index: int, problems: list[tuple[int, str]]) -> None:
    k = index + 1
    if _is_name(_at(tokens, k), "type"):
        k += 1
    token = _at(tokens, k)
    if _is_punct(token, "*"):
        reexport = True
    elif _is_punct(token, "{"):
        close = _closing(tokens, k)
        reexport = close is not None and _is_name(_at(tokens, close + 1), "from")
    else:
        reexport = False
    if reexport:
        line = tokens[index].line
        problems.append((line, f"export ... from on line {line}."))


def _is_called(tokens: list[Token], index: int) -> bool:
    following = _at(tokens, index + 1)
    if _is_punct(following, "("):
        return True
    if _is_punct(following, "?.") and _is_punct(_at(tokens, index + 2), "("):
        return True
    return following is not None and following.kind == "template" and following.text[0] == "`"


def _check_test_call(tokens: list[Token], index: int, problems: list[tuple[int, str]]) -> None:
    """Check a call of test, or of test.only, test.describe and the like, at ``index``."""
    name = tokens[index].text
    chain: list[str] = []
    k = index + 1
    while _is_punct(_at(tokens, k), ".", "?.") and _is_name_token(_at(tokens, k + 1)):
        chain.append(tokens[k + 1].text)
        k += 2
    if chain[:1] == ["extend"]:
        line = tokens[index].line
        problems.append((line, f"{name}.extend on line {line}."))
    # A step's callback takes the step, not fixtures.
    if not _is_punct(_at(tokens, k), "(") or "step" in chain:
        return
    close = _closing(tokens, k)
    if close is None:
        return
    for start, end in _split(tokens, k + 1, close):
        _check_callback(tokens, start, end, problems)


def _check_callback(
    tokens: list[Token], start: int, end: int, problems: list[tuple[int, str]]
) -> None:
    """If ``tokens[start:end]`` is a function, check the fixtures its first parameter takes."""
    k = start + 1 if _is_name(_at(tokens, start), "async") and start + 1 < end else start
    token = _at(tokens, k)
    if _is_name(token, "function"):
        k += 1
        if _is_punct(_at(tokens, k), "*"):
            k += 1
        if _is_name_token(_at(tokens, k)):
            k += 1
        if not _is_punct(_at(tokens, k), "("):
            return
        close = _closing(tokens, k)
    elif token is not None and token.kind == "name" and _is_punct(_at(tokens, k + 1), "=>"):
        _whole(token, problems)
        return
    elif _is_punct(token, "("):
        close = _closing(tokens, k)
        if close is None or not _arrow_follows(tokens, close + 1, end):
            return
    else:
        return
    if close is None:
        return
    parameters = _split(tokens, k + 1, close)
    if parameters:
        _check_fixtures(tokens, *parameters[0], problems)


def _arrow_follows(tokens: list[Token], index: int, end: int) -> bool:
    """Whether => follows a parameter list, after a return type if there is one."""
    if _is_punct(_at(tokens, index), "=>"):
        return True
    if not _is_punct(_at(tokens, index), ":"):
        return False
    depth = 0
    for k in range(index + 1, end):
        if _closes(tokens[k]):
            depth -= 1
        if _opens(tokens[k]):
            depth += 1
        if depth == 0 and _is_punct(tokens[k], "=>"):
            return True
    return False


def _whole(token: Token, problems: list[tuple[int, str]]) -> None:
    named = f' as "{token.text}"' if token.kind == "name" else ""
    line = token.line
    problems.append((line, f"Fixtures taken whole{named} on line {line}; only page is allowed."))


def _check_fixtures(
    tokens: list[Token], start: int, end: int, problems: list[tuple[int, str]]
) -> None:
    """Check the first parameter of a test callback: it may destructure page alone."""
    if start >= end:
        return
    first = tokens[start]
    close = _closing(tokens, start) if _is_punct(first, "{") else None
    if close is None:
        _whole(first, problems)
        return
    for part, _ in _split(tokens, start + 1, close):
        key = tokens[part]
        line = key.line
        if _is_punct(key, "..."):
            rest = _at(tokens, part + 1)
            named = rest.text if rest is not None and rest.kind == "name" else ""
            problems.append(
                (line, f"Fixtures taken with ...{named} on line {line}; only page is allowed.")
            )
        elif _is_punct(key, "["):
            problems.append(
                (line, f"A computed fixture name on line {line}; only page is allowed.")
            )
        elif key.text != "page" or key.kind not in ("name", "string"):
            named = json.dumps(key.text, ensure_ascii=False)
            problems.append(
                (line, f"Fixture {named} requested on line {line}; only page is allowed.")
            )
