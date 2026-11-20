"""The credential equivalence check (research notes, section 6b).

Recorded sessions are paid for by a subscription token. An API key may stand in for it only if
both credentials give the same session setup. This tool compares four sessions of one job: two
paid by the API key (A1, A2) and two by the subscription (S1, S2). It reports every item it
compares and passes only when nothing differs between the credentials::

    python -m hoh.equivalence --api DIR DIR --subscription DIR DIR [--json OUT]

Each DIR is a session's ``out/`` directory, or a job attempt directory that holds ``out/``
(``stream.jsonl`` and ``launch.json`` are read; see ``hoh.session`` and ``hoh.worker``).

Compared items, and what each status means:

* the init event, field by field, except ``session_id``, ``uuid`` and ``apiKeySource``;
* ``apiKeySource``: ``ANTHROPIC_API_KEY`` in the API runs, ``none`` in the subscription runs;
* the size of the first model request (``input_tokens`` plus both cache counts), exactly;
* the keys of ``modelUsage`` in the final result;
* the cache lifetime split of every usage block; any 5-minute write fails (the launch asks for
  a one-hour cache);
* from ``launch.json``: argv (session id and directories masked), environment names and the
  values of the non-credential variables, the binary hash and the hashes of the input files.

Statuses: ``same``; ``noise`` (the item differs between the two runs of one credential and is
one that may, today only ``cwd``); ``DIFFERENT`` (the credentials disagree); ``UNSTABLE`` (an
item that must not vary moved between two runs of one credential, so it cannot be compared);
``MISSING`` (a run has no value); ``FAIL`` (a rule about one credential is broken).

Exit code: 0 pass, 1 fail, 2 unusable input. A value of a variable whose name contains TOKEN,
KEY or SECRET is never printed, and neither is anything that looks like a credential.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hoh.session import API_KEY_SOURCE, CREDENTIAL_VARS, REDACTED

LABELS = ("A1", "A2", "S1", "S2")
API_LABELS = LABELS[:2]
SUB_LABELS = LABELS[2:]
API_VAR = CREDENTIAL_VARS["api_key"]
SUB_VAR = CREDENTIAL_VARS["subscription"]
CREDENTIAL_NAMES = frozenset(CREDENTIAL_VARS.values())
EXPECTED_SOURCE = {"A": API_KEY_SOURCE["api_key"], "S": API_KEY_SOURCE["subscription"]}
IGNORED_INIT_FIELDS = frozenset({"session_id", "uuid", "apiKeySource"})
NOISE_ALLOWED_INIT_FIELDS = frozenset({"cwd"})
SECRET_WORDS = ("TOKEN", "KEY", "SECRET")
WITHHELD = "<withheld>"
CREDENTIAL_SHAPE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
MIN_SECRET_LENGTH = 6
FAILING = frozenset({"different", "unstable", "missing", "fail"})
EXIT_PASS, EXIT_FAIL, EXIT_UNUSABLE = 0, 1, 2


class Unusable(Exception):
    """An input that cannot be compared: a missing file, no init line, no result line."""


@dataclass(frozen=True)
class Missing:
    reason: str


_ABSENT = "<absent>"


@dataclass
class Row:
    item: str
    status: str
    values: dict[str, Any]
    note: str = ""


@dataclass
class Session:
    label: str
    path: Path
    init: dict[str, Any]
    size: int | Missing
    model_usage: list[str] | Missing
    cache: dict[str, int] | Missing
    argv: list[str] | Missing
    env: dict[str, Any] | Missing
    binary_hash: str | Missing
    file_hashes: dict[str, str] | Missing
    secrets: set[str] = field(default_factory=set)


def is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(word in upper for word in SECRET_WORDS)


# --- reading one session


def session_dir(path: Path) -> Path:
    out = path / "out"
    return out if out.is_dir() else path


def read_events(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _main_conversation(event: dict) -> bool:
    return not event.get("parent_tool_use_id")


SIZE_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _size(usage: Any, source: str) -> int | Missing:
    if not isinstance(usage, dict):
        return Missing(f"the first {source} has no usage block")
    absent = [name for name in SIZE_FIELDS if not _is_int(usage.get(name))]
    if absent:
        return Missing(f"the usage block of the first {source} lacks " + ", ".join(absent))
    return sum(usage[name] for name in SIZE_FIELDS)


def first_request_size(events: list[dict]) -> int | Missing:
    """``input + cache creation + cache read`` of the first request (notes, section 4)."""
    for event in events:
        inner = event.get("event")
        if (
            event.get("type") == "stream_event"
            and _main_conversation(event)
            and isinstance(inner, dict)
            and inner.get("type") == "message_start"
        ):
            message = inner.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            return _size(usage, "message_start event")
    for event in events:
        if event.get("type") == "assistant" and _main_conversation(event):
            message = event.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            return _size(usage, "assistant line")
    return Missing("no message_start event and no assistant line")


def _usage_blocks(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "usage" and isinstance(value, dict):
                yield value
            yield from _usage_blocks(value)
    elif isinstance(node, list):
        for value in node:
            yield from _usage_blocks(value)


def cache_split(events: list[dict]) -> dict[str, int] | Missing:
    """Cache writes by lifetime, summed over every usage block that carries the split."""
    five = one = blocks = five_blocks = incomplete = 0
    for event in events:
        for usage in _usage_blocks(event):
            split = usage.get("cache_creation")
            if split is None:
                continue
            five_n = split.get("ephemeral_5m_input_tokens") if isinstance(split, dict) else None
            one_n = split.get("ephemeral_1h_input_tokens") if isinstance(split, dict) else None
            if not (_is_int(five_n) and _is_int(one_n)):
                incomplete += 1
                continue
            blocks += 1
            five += five_n
            one += one_n
            five_blocks += five_n > 0
    if incomplete:
        return Missing(f"{incomplete} usage block(s) have a cache_creation without both counts")
    if not blocks:
        return Missing("no usage block carries cache_creation.ephemeral_5m/1h_input_tokens")
    return {"five_minute": five, "one_hour": one, "blocks": blocks,
            "five_minute_blocks": five_blocks}


def load_session(label: str, path: Path) -> Session:
    where = session_dir(path)
    if not path.is_dir():
        raise Unusable(f"{label}: the directory {path} does not exist")
    for name in ("stream.jsonl", "launch.json"):
        if not (where / name).is_file():
            raise Unusable(f"{label}: {name} is missing in {where}")
    events = read_events(where / "stream.jsonl")
    init = next(
        (e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None
    )
    if init is None:
        raise Unusable(f"{label}: no init line in {where / 'stream.jsonl'}")
    final = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if final is None:
        raise Unusable(f"{label}: no result line in {where / 'stream.jsonl'}")
    try:
        launch = json.loads((where / "launch.json").read_text(encoding="utf-8"))
    except ValueError as exc:
        raise Unusable(f"{label}: launch.json in {where} is not JSON ({exc})") from None
    if not isinstance(launch, dict):
        raise Unusable(f"{label}: launch.json in {where} is not an object")

    raw_model_usage = final.get("modelUsage")
    model_usage: list[str] | Missing = (
        sorted(raw_model_usage) if isinstance(raw_model_usage, dict)
        else Missing("the result line has no modelUsage")
    )
    raw_argv = launch.get("argv")
    argv: list[str] | Missing = (
        [str(a) for a in raw_argv] if isinstance(raw_argv, list)
        else Missing("launch.json has no argv")
    )
    raw_env = launch.get("env")
    env: dict[str, Any] | Missing = (
        dict(raw_env) if isinstance(raw_env, dict) else Missing("launch.json has no env")
    )
    cli = launch.get("cli")
    digest = cli.get("sha256") if isinstance(cli, dict) else None
    hashes = launch.get("sha256")
    secrets = set()
    if isinstance(env, dict):
        secrets = {
            v for k, v in env.items()
            if is_secret_name(str(k)) and isinstance(v, str) and v != REDACTED
            and len(v) >= MIN_SECRET_LENGTH
        }
    return Session(
        label=label,
        path=where,
        init=init,
        size=first_request_size(events),
        model_usage=model_usage,
        cache=cache_split(events),
        argv=argv,
        env=env,
        binary_hash=digest if isinstance(digest, str) and digest else Missing(
            "launch.json has no cli.sha256"),
        file_hashes=dict(hashes) if isinstance(hashes, dict) and hashes else Missing(
            "launch.json has no file hashes (sha256)"),
        secrets=secrets,
    )


# --- comparing


def _differs_within(values: dict[str, Any]) -> bool:
    return values["A1"] != values["A2"] or values["S1"] != values["S2"]


def compare(item: str, values: dict[str, Any], paths: dict[str, Path], *,
            noise_ok: bool = False, note: str = "") -> Row:
    """Compare one item across the four runs; see the module docstring for the statuses."""
    gaps = [f"{k} ({paths[k]}): {v.reason}" for k, v in values.items() if isinstance(v, Missing)]
    if gaps:
        shown = {k: (f"<missing: {v.reason}>" if isinstance(v, Missing) else v)
                 for k, v in values.items()}
        return Row(item, "missing", shown, "; ".join(gaps))
    if _differs_within(values):
        if noise_ok:
            return Row(item, "noise", values, "differs between runs of one credential; ignored")
        return Row(item, "unstable", values,
                   "differs between two runs of one credential, so it cannot be compared")
    if values["A1"] != values["S1"]:
        return Row(item, "different", values, note)
    return Row(item, "same", values, note)


def mask_directories(token: str) -> str:
    """``/slots/1/h/settings.json`` becomes ``<dir>/settings.json``: slots differ, names do not."""
    if token.startswith("/") and ":" not in token and not any(c.isspace() for c in token):
        return "<dir>/" + token.rstrip("/").rsplit("/", 1)[-1]
    return token


def masked_argv(argv: list[str] | Missing) -> list[str] | Missing:
    if isinstance(argv, Missing):
        return argv
    out = []
    mask_next = False
    for token in argv:
        out.append("<session-id>" if mask_next else mask_directories(token))
        mask_next = token == "--session-id"
    return out


def init_rows(sessions: dict[str, Session], paths: dict[str, Path]) -> list[Row]:
    fields = sorted({k for s in sessions.values() for k in s.init} - IGNORED_INIT_FIELDS)
    return [
        compare(
            f"init.{name}",
            {k: s.init.get(name, _ABSENT) for k, s in sessions.items()},
            paths,
            noise_ok=name in NOISE_ALLOWED_INIT_FIELDS,
        )
        for name in fields
    ]


def api_key_source_row(sessions: dict[str, Session]) -> Row:
    values = {k: s.init.get("apiKeySource", _ABSENT) for k, s in sessions.items()}
    wrong = [k for k, v in values.items() if v != EXPECTED_SOURCE[k[0]]]
    expected = f"expected {EXPECTED_SOURCE['A']!r} in A1, A2 and {EXPECTED_SOURCE['S']!r} in S1, S2"
    if wrong:
        return Row("apiKeySource", "fail", values, f"{expected}; wrong in {', '.join(wrong)}")
    return Row("apiKeySource", "same", values, expected)


def cache_row(sessions: dict[str, Session]) -> Row:
    values: dict[str, Any] = {}
    gaps = []
    bad = []
    for k, s in sessions.items():
        if isinstance(s.cache, Missing):
            values[k] = f"<missing: {s.cache.reason}>"
            gaps.append(f"{k} ({s.path}): {s.cache.reason}")
            continue
        c = s.cache
        values[k] = (f"5-minute: {c['five_minute']} tokens in {c['five_minute_blocks']} of "
                     f"{c['blocks']} blocks; 1-hour: {c['one_hour']} tokens")
        if c["five_minute"] > 0:
            bad.append(k)
    if bad:
        return Row("5-minute cache writes", "fail", values,
                   f"the launch uses a one-hour cache; 5-minute writes in {', '.join(bad)}")
    if gaps:
        return Row("5-minute cache writes", "missing", values, "; ".join(gaps))
    return Row("5-minute cache writes", "same", values, "none in any run")


def credential_variable_row(sessions: dict[str, Session]) -> Row:
    values: dict[str, Any] = {}
    bad = []
    for k, s in sessions.items():
        if isinstance(s.env, Missing):
            values[k] = f"<missing: {s.env.reason}>"
            bad.append(k)
            continue
        present = sorted(n for n in s.env if n in CREDENTIAL_NAMES)
        values[k] = present
        want = [API_VAR] if k[0] == "A" else [SUB_VAR]
        if present != want:
            bad.append(k)
    note = f"exactly {API_VAR} in A1, A2 and exactly {SUB_VAR} in S1, S2"
    if bad:
        return Row("credential variable", "fail", values, f"{note}; wrong in {', '.join(bad)}")
    return Row("credential variable", "same", values, note)


def environment_rows(sessions: dict[str, Session], paths: dict[str, Path]) -> list[Row]:
    envs = {k: s.env for k, s in sessions.items()}
    names = {
        k: (e if isinstance(e, Missing) else sorted(n for n in e if n not in CREDENTIAL_NAMES))
        for k, e in envs.items()
    }
    names_row = compare("environment names", names, paths,
                        note="the two credential variables are excluded (see credential variable)")
    if names_row.status == "different":
        only_api = sorted(set(names["A1"]) - set(names["S1"]))
        only_sub = sorted(set(names["S1"]) - set(names["A1"]))
        names_row.note = f"only in API runs: {only_api}; only in subscription runs: {only_sub}"
    rows = [names_row]
    if any(isinstance(e, Missing) for e in envs.values()):
        rows.append(Row("environment values", "missing", {}, "an environment is missing"))
        return rows
    common = set.intersection(*(set(names[k]) for k in LABELS))
    unstable: dict[str, dict[str, str]] = {}
    different: dict[str, dict[str, str]] = {}
    for name in sorted(common):
        raw = {k: envs[k][name] for k in LABELS}
        norm = {k: mask_directories(v) if isinstance(v, str) else v for k, v in raw.items()}
        if len(set(map(json.dumps, norm.values()))) == 1:
            continue
        shown = {
            k: (WITHHELD if is_secret_name(name) else norm[k]) for k in LABELS
        }
        if _differs_within(norm):
            unstable[name] = shown
        elif norm["A1"] != norm["S1"]:
            different[name] = shown
    if unstable:
        rows.append(Row("environment values", "unstable", unstable,
                        "variables that differ within one credential"))
    elif different:
        rows.append(Row("environment values", "different", different,
                        "variables whose values differ between the credentials "
                        f"(values of names containing {', '.join(SECRET_WORDS)} are withheld)"))
    else:
        rows.append(Row("environment values", "same",
                        {"all": f"{len(common)} variables compared"},
                        "directories masked; values of secret-named variables are not shown"))
    return rows


def all_rows(sessions: dict[str, Session]) -> list[Row]:
    paths = {k: s.path for k, s in sessions.items()}
    rows = [*init_rows(sessions, paths), api_key_source_row(sessions)]
    rows.append(compare("first request size",
                        {k: s.size for k, s in sessions.items()}, paths,
                        note="input + cache creation + cache read, exact"))
    rows.append(compare("modelUsage keys", {k: s.model_usage for k, s in sessions.items()}, paths))
    rows.append(cache_row(sessions))
    rows.append(compare("argv", {k: masked_argv(s.argv) for k, s in sessions.items()}, paths,
                        note="session id and directories masked"))
    rows.append(credential_variable_row(sessions))
    rows.extend(environment_rows(sessions, paths))
    rows.append(compare("binary hash", {k: s.binary_hash for k, s in sessions.items()}, paths))
    rows.append(compare("file hashes", {k: s.file_hashes for k, s in sessions.items()}, paths))
    return rows


# --- output


def _show(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def render(rows: list[Row], sessions: dict[str, Session], verdict: str) -> str:
    lines = ["Credential equivalence check", ""]
    for k in LABELS:
        kind = "API key     " if k[0] == "A" else "subscription"
        lines.append(f"  {k}  {kind}  {sessions[k].path}")
    lines.append("")
    for row in rows:
        label = row.status if row.status in ("same", "noise") else row.status.upper()
        lines.append(f"  {label:<9} {row.item}" + (f"  ({row.note})" if row.note else ""))
        vals = row.values
        if row.status in ("different", "unstable") and row.item == "environment values":
            for name, per in vals.items():
                shown = ", ".join(f"{k}={_show(v)}" for k, v in per.items())
                lines.append(f"      {name}: {shown}")
        elif set(vals) == set(LABELS):
            rendered = {k: _show(v) for k, v in vals.items()}
            if len(set(rendered.values())) == 1:
                lines.append(f"      all runs: {rendered['A1']}")
            else:
                for group, labels in (("API key", API_LABELS), ("subscription", SUB_LABELS)):
                    if rendered[labels[0]] == rendered[labels[1]]:
                        lines.append(f"      {group} ({'=='.join(labels)}): {rendered[labels[0]]}")
                    else:
                        lines.append(f"      {group}: " + "; ".join(
                            f"{k}={rendered[k]}" for k in labels))
        else:
            for k, v in vals.items():
                lines.append(f"      {k}: {_show(v)}")
    failing = [r.item for r in rows if r.status in FAILING]
    lines.append("")
    if verdict == "pass":
        lines.append("PASS: no difference between the credentials; the API key may stand in "
                     "for the subscription token for this setup.")
    else:
        lines.append(f"FAIL: {len(failing)} item(s): " + ", ".join(failing))
    return "\n".join(lines) + "\n"


def scrub(text: str, secrets: set[str]) -> str:
    """Remove anything that is, or looks like, a credential from output text."""
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, WITHHELD)
        escaped = json.dumps(secret)[1:-1]
        if escaped != secret:
            text = text.replace(escaped, WITHHELD)
    return CREDENTIAL_SHAPE.sub(WITHHELD, text)


def run(api: list[str], subscription: list[str], json_out: str | None) -> tuple[int, str]:
    given = dict(zip(LABELS, [*api, *subscription], strict=True))
    paths = {k: Path(v) for k, v in given.items()}
    problems: list[str] = []
    resolved: dict[Path, str] = {}
    for k, p in paths.items():
        key = session_dir(p).resolve()
        if key in resolved:
            problems.append(f"{k} and {resolved[key]} are the same directory ({p}); "
                            "four separate runs are needed")
        resolved.setdefault(key, k)
    sessions: dict[str, Session] = {}
    for k, p in paths.items():
        try:
            sessions[k] = load_session(k, p)
        except Unusable as exc:
            problems.append(str(exc))
        except OSError as exc:
            problems.append(f"{k}: cannot read {p} ({exc})")
    secrets: set[str] = set()
    for s in sessions.values():
        secrets |= s.secrets

    if problems:
        text = "Credential equivalence check\n\nUNUSABLE INPUT:\n" + "".join(
            f"  {p}\n" for p in problems)
        data: dict[str, Any] = {"verdict": "unusable", "exit_code": EXIT_UNUSABLE,
                                "problems": problems}
        code = EXIT_UNUSABLE
    else:
        rows = all_rows(sessions)
        failing = any(r.status in FAILING for r in rows)
        verdict = "fail" if failing else "pass"
        code = EXIT_FAIL if failing else EXIT_PASS
        text = render(rows, sessions, verdict)
        data = {
            "verdict": verdict,
            "exit_code": code,
            "runs": {k: str(s.path) for k, s in sessions.items()},
            "items": [
                {"item": r.item, "status": r.status, "values": r.values, "note": r.note}
                for r in rows
            ],
        }
    text = scrub(text, secrets)
    if json_out:
        blob = scrub(json.dumps(data, indent=2, ensure_ascii=False) + "\n", secrets)
        Path(json_out).write_text(blob, encoding="utf-8")
    return code, text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hoh.equivalence",
        description="Check that API-key sessions and subscription sessions have the same setup.",
    )
    parser.add_argument("--api", nargs=2, required=True, metavar="DIR",
                        help="the out/ (or attempt) directories of the two API key runs")
    parser.add_argument("--subscription", nargs=2, required=True, metavar="DIR",
                        help="the out/ (or attempt) directories of the two subscription runs")
    parser.add_argument("--json", metavar="OUT", help="also write the report as JSON")
    args = parser.parse_args(argv)
    code, text = run(args.api, args.subscription, args.json)
    sys.stdout.write(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
