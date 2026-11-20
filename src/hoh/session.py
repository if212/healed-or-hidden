"""One agent session: a pristine slot, one pinned launch, and how the session ended.

A *slot* is a directory reused by every session one worker runs, so that every path the CLI
can see or embed is the same string each time (``docs/research/launch-config.md``, 6a)::

    <slot>/ws/       the workspace: tests/<file> and test-results/, nothing else
           cfg/      CLAUDE_CONFIG_DIR, created empty
           home/     HOME, created empty
           h/        settings.json, mcp.json, prompt.md, append.md: the CLI's inputs
           out/      stream.jsonl, stderr.log, launch.json, end.json
           session   a link to the session directory

The session directory holds the ``run_test`` server's configuration, counts and run snapshots,
and an untouched copy of the handed-out tests. It lies outside the slot, where the agent cannot
read it, and wherever the caller likes (under the job's results, say): the server reaches it
through the slot's link, so no path derived from the job is anywhere the CLI looks.

Every session is launched the same way whichever credential pays for it: never ``--bare``, the
binary by absolute path, an environment built from nothing with exactly one credential in it.
The credential's value is never written to a file, a log, the argument list, a ``repr`` or an
exception message. After each session every file under the slot and the session directory is
searched for it; a hit is redacted and stops the queue.

Two time limits apply. ``wall_clock_s`` caps the agent's own time: wall time minus the time
spent inside ``run_test`` calls, which the server records. ``hard_wall_clock_s`` caps the wall
time outright; only an environment that hangs or crawls can reach it first.

How a session ended is classified with the table of section 5.5 of the research notes, in its
order, and mapped to what the job queue does next:

=============  ==========  ===================================================================
class          outcome     what it means
=============  ==========  ===================================================================
environment    env_failed  the configuration or the environment failed: rerun, never judge
limit          limit       a usage or rate limit: pause the queue until the reset time
credential     error       the credential was refused (or found on disk), or the money behind
                           it is spent (no credit left, a spend limit): stop and look
capacity       env_failed  overloaded or a server error after the CLI's own retries: rerun
model_changed  error       a safety-triggered model switch or refusal: not judged under the
                           pinned model; the analysis plan decides
censored       done        the turn, budget or time cap ended it: judged as it stood
completed      done        the agent finished
=============  ==========  ===================================================================

One deviation from the table's order: spent credit or a spend limit is ``credential`` even when
it comes as a 429, which row 3 would take for a limit, since waiting does not bring it back.

``run_session`` adds what only it can see. An init that fails ``check_init`` or the pinned
version and model is ``environment``, and the session is stopped as soon as that line arrives.
A session the harness stopped at the agent-time cap is ``censored`` (kept and judged as it
stood), unless nothing was edited, no result line followed and the stream does not show the
agent at work up to the stop (no model turn, or the CLI was retrying a failed request): then
``environment``. One stopped at the hard cap is ``environment``. A file ``env_failed`` or
``harness_error`` in the session directory makes the session ``environment`` whatever the stream
says, and so does a ``run_test`` call that did not run as the server reported it. The credential
found on disk makes it ``credential``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import IO, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hoh.backend import Variant
from hoh.jobs import FORBIDDEN_WORDS
from hoh.prompts import PROMPTS_DIR, system_prompt, user_message
from hoh.queue import DEFAULT_PAUSE_SECONDS, Outcome, atomic_write_json
from hoh.run_test_server import (
    CREDENTIAL_VARIABLES,
    ENV_FAILED_TEXT,
    HARNESS_ERROR_TEXT,
    TOOL_NAME,
)

log = logging.getLogger(__name__)

SETTINGS_FILE = PROMPTS_DIR / "session" / "settings.json"
SERVER_KEY = "runner"
RUN_TEST_TOOL = f"mcp__{SERVER_KEY}__{TOOL_NAME}"
BUILTIN_TOOLS = "Read,Glob,Grep,Edit,Write"
EDIT_RULE = "Edit(./**)"
SLOT_DIRS = ("ws", "cfg", "home", "h", "out")
SESSION_LINK = "session"
ORIGINAL_TESTS = "original_tests"
# Files the run_test server or the harness leaves in the session directory when the
# environment failed: such a session is rerun, never judged (CLAUDE.md, hard rule 9).
FAILURE_MARKERS = ("env_failed", "harness_error")
# PATH for the run_test server: on macOS, docker is in /usr/local/bin (Docker Desktop) or
# /opt/homebrew/bin (Homebrew).
SERVER_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# What the CLI finds by walking up from its working directory (research notes 1.1 to 1.4).
PROJECT_MARKERS = (".git", ".claude", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md")
STOP_GRACE_S = 10.0
EFFORTS = ("low", "medium", "high", "xhigh", "max")
CACHE_TTLS = ("5m", "1h")
REDACTED = "<redacted>"

CREDENTIAL_VARS: Mapping[str, str] = MappingProxyType(
    {"subscription": "CLAUDE_CODE_OAUTH_TOKEN", "api_key": "ANTHROPIC_API_KEY"}
)
# What the init event's apiKeySource says for each credential (research notes, section 2 row 19).
API_KEY_SOURCE: Mapping[str, str] = MappingProxyType(
    {"subscription": "none", "api_key": "ANTHROPIC_API_KEY"}
)
_SECRET_NAMES = frozenset(CREDENTIAL_VARS.values())

CLASSES = (
    "environment", "limit", "credential", "capacity", "model_changed", "censored", "completed"
)
OUTCOME_STATE: Mapping[str, str] = MappingProxyType(
    {
        "environment": "env_failed",
        "limit": "limit",
        "credential": "error",
        "capacity": "env_failed",
        "model_changed": "error",
        "censored": "done",
        "completed": "done",
    }
)
# Init fields that must equal the reference (section 6b, step 3, item 1).
INIT_FIELDS = (
    "claude_code_version", "model", "permissionMode", "tools", "mcp_servers", "skills",
    "slash_commands", "agents", "plugins", "output_style", "betas",
)
_CREDENTIAL_ERRORS = frozenset(
    {"billing_error", "authentication_failed", "oauth_org_not_allowed", "account_on_hold"}
)
# The money behind the credential is gone: an API key without credit, or the usage-credit cap
# of a subscription (research notes 2.4). The status may be 400 or 429 and the category may
# not be billing_error, so the error text decides too. Only the API's errors are read (assistant
# events with an error, and the result of a failed session), never what the agent wrote.
_BILLING_TEXT = re.compile(r"credit balance is too low|spend limit", re.IGNORECASE)
_CAPACITY_ERRORS = frozenset({"overloaded", "server_error"})
_REFUSALS = frozenset({"model_refusal_fallback", "model_refusal_no_fallback"})
_CENSORING = frozenset({"error_max_turns", "error_max_budget_usd"})
_SIGTERM_EXITS = frozenset({143, -signal.SIGTERM})
# Classes whose cause lies outside the session: a stop by the harness does not override them.
_EXTERNAL = frozenset({"limit", "credential", "capacity", "model_changed"})
_LIMIT_PREFIX = "You've hit your"
_LIMIT_WORD = re.compile(r"\blimit", re.IGNORECASE)


# --- the credential


class CredentialError(ValueError):
    """A credential could not be read. The message never contains a credential value."""


@dataclass(frozen=True, slots=True)
class Credential:
    """The one credential a session runs on. Its repr and str never show the value."""

    kind: Literal["subscription", "api_key"]
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        # Neither message may echo an argument: with the two arguments swapped, the kind
        # would be the secret.
        if self.kind not in CREDENTIAL_VARS:
            raise ValueError("credential kind must be 'subscription' or 'api_key'")
        value = self.value
        if not isinstance(value, str) or not value:
            raise ValueError("credential value must be a non-empty string")
        if not (value.isascii() and value.isprintable()) or any(c.isspace() for c in value):
            raise ValueError("credential value must be printable ASCII without whitespace")

    @property
    def env_name(self) -> str:
        """The environment variable the CLI reads this credential from."""
        return CREDENTIAL_VARS[self.kind]

    def __repr__(self) -> str:
        return f"Credential(kind={self.kind!r}, value={REDACTED})"

    __str__ = __repr__


_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def load_credential(kind: str, env_file: Path) -> Credential:
    """Read ``CLAUDE_CODE_OAUTH_TOKEN`` (subscription) or ``ANTHROPIC_API_KEY`` from ``.env``.

    Lines are ``NAME=value``, optionally after ``export`` and optionally quoted; ``#`` starts a
    comment. The variable must be set exactly once. No error message quotes the file.
    """
    if kind not in CREDENTIAL_VARS:
        raise CredentialError("credential kind must be 'subscription' or 'api_key'")
    name = CREDENTIAL_VARS[kind]
    env_file = Path(env_file)
    try:
        data = env_file.read_bytes()
    except OSError as exc:
        raise CredentialError(f"cannot read {env_file}: {exc.strerror}") from None
    text = data.decode("utf-8", errors="replace")
    del data
    found: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _ASSIGNMENT.match(line.strip())
        if match and match.group(1) == name:
            found.append((number, _unquote(match.group(2))))
    del text
    if not found:
        raise CredentialError(f"{name} is not set in {env_file}")
    if len(found) > 1:
        lines = ", ".join(str(number) for number, _ in found)
        raise CredentialError(f"{name} is set more than once in {env_file} (lines {lines})")
    number, value = found[0]
    try:
        return Credential(kind, value)
    except ValueError as exc:
        problem = str(exc)  # raised below, outside this handler, so no context is attached
    raise CredentialError(f"{name} on line {number} of {env_file}: {problem}")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    comment = value.find(" #")
    return value[:comment].rstrip() if comment >= 0 else value


# --- the pinned configuration


def _looks_secret(name: str) -> bool:
    upper = name.upper()
    return upper.startswith(("ANTHROPIC_", "CLAUDE_")) or any(
        word in upper for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "APIKEY", "CREDENTIAL")
    )


@dataclass(frozen=True, kw_only=True)
class LaunchConfig:
    """Everything a launch depends on besides the slot, the credential and the session id."""

    claude_bin: Path
    cli_version: str
    model: str = "claude-sonnet-5-5"
    effort: str = "medium"
    cache_ttl: str = "1h"
    max_turns: int = 80
    max_budget_usd: float = 1.50
    wall_clock_s: int = 1200
    """The agent's own time: wall time minus the time spent inside ``run_test`` calls."""
    hard_wall_clock_s: int = 3600
    """Wall time, all of it."""
    python: Path
    """The interpreter that runs the ``run_test`` server."""
    server_env: Mapping[str, str] = field(default_factory=lambda: {"PATH": SERVER_PATH})
    """PATH and DOCKER_HOST for the ``run_test`` server; never a credential. Without a PATH it
    gets ``SERVER_PATH``."""

    def __post_init__(self) -> None:
        for name in ("claude_bin", "python"):
            path = Path(getattr(self, name))
            if not path.is_absolute():
                raise ValueError(f"{name} must be an absolute path, got {str(path)!r}")
            object.__setattr__(self, name, path)
        if not isinstance(self.cli_version, str) or not self.cli_version:
            raise ValueError("cli_version is required: every session asserts it")
        if not isinstance(self.model, str) or not self.model.startswith("claude-"):
            raise ValueError(
                f"model must be a full model ID such as 'claude-sonnet-5-5', never an alias; "
                f"got {self.model!r}"
            )
        if self.effort not in EFFORTS:
            raise ValueError(f"effort must be one of {EFFORTS}, got {self.effort!r}")
        if self.cache_ttl not in CACHE_TTLS:
            raise ValueError(f"cache_ttl must be one of {CACHE_TTLS}, got {self.cache_ttl!r}")
        if not isinstance(self.max_turns, int) or isinstance(self.max_turns, bool) or (
            self.max_turns < 1
        ):
            raise ValueError(f"max_turns must be a positive integer, got {self.max_turns!r}")
        if not self.max_budget_usd > 0:
            raise ValueError(f"max_budget_usd must be positive, got {self.max_budget_usd!r}")
        if not self.wall_clock_s > 0:
            raise ValueError(f"wall_clock_s must be positive, got {self.wall_clock_s!r}")
        if not self.hard_wall_clock_s >= self.wall_clock_s:
            raise ValueError(
                f"hard_wall_clock_s ({self.hard_wall_clock_s!r}) must not be below wall_clock_s "
                f"({self.wall_clock_s!r})"
            )
        env = dict(self.server_env)
        env.setdefault("PATH", SERVER_PATH)
        for key, value in env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("server_env must map names to strings")
            if _looks_secret(key):
                raise ValueError(
                    f"server_env must not carry {key}: the run_test server never holds a "
                    f"credential"
                )
        object.__setattr__(self, "server_env", MappingProxyType(env))

    def record(self) -> dict[str, Any]:
        return {
            "claude_bin": str(self.claude_bin),
            "cli_version": self.cli_version,
            "model": self.model,
            "effort": self.effort,
            "cache_ttl": self.cache_ttl,
            "max_turns": self.max_turns,
            "max_budget_usd": self.max_budget_usd,
            "wall_clock_s": self.wall_clock_s,
            "hard_wall_clock_s": self.hard_wall_clock_s,
            "python": str(self.python),
            "server_env": dict(self.server_env),
        }


class LaunchEnv(dict[str, str]):
    """The complete environment of one launch. Its repr and str hide the credential."""

    def redacted(self) -> dict[str, str]:
        return {k: (REDACTED if k in _SECRET_NAMES else v) for k, v in self.items()}

    def __repr__(self) -> str:
        return f"LaunchEnv({self.redacted()!r})"

    __str__ = __repr__


@dataclass(frozen=True)
class SessionResult:
    outcome: Outcome
    session_id: str
    stream_path: Path
    exit_code: int | None
    wall_s: float
    init: dict | None
    result: dict | None
    run_test_calls: int
    """How many ``run_test`` calls ran the test, counted toward the limit or not (a first
    call before any change runs but is not counted; the state file's ``count`` has only the
    counted ones)."""
    end_class: str
    """The class of section 5.5 (one of ``CLASSES``); ``outcome`` is what the queue does."""


# --- the slot


def _check_visible(text: str, what: str) -> None:
    low = text.lower()
    for word in FORBIDDEN_WORDS:
        if word in low:
            raise ValueError(
                f"{what} {text!r} contains {word!r}, which the agent under test must not see"
            )


def _within(inner: Path, outer: Path) -> bool:
    return inner == outer or outer in inner.parents


def _listing(root: Path) -> list[str]:
    names = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted([*dirnames, *filenames]):
            path = Path(dirpath) / name
            if path.is_symlink():
                raise ValueError(f"{path} is a symbolic link; the initial results must be files")
            names.append(path.relative_to(root).as_posix())
    return names


def _mcp_config(slot: Path, config: LaunchConfig) -> dict[str, Any]:
    return {
        "mcpServers": {
            SERVER_KEY: {
                "type": "stdio",
                "command": str(config.python),
                # -I: the server starts in the workspace, which the agent writes to. Isolated
                # mode keeps that directory, and PYTHON* variables, off the import path.
                # The session directory is reached through the slot's link, so the job's own
                # paths stay out of every file the CLI reads.
                "args": [
                    "-I", "-m", "hoh.run_test_server", "--session-dir", str(slot / SESSION_LINK)
                ],
                # The CLI passes its own environment, the credential included, on to the
                # server; these empty values go over it. The server refuses to start if one of
                # them still holds a value, so a failure here costs a launch, never a leak.
                "env": {**config.server_env, **dict.fromkeys(CREDENTIAL_VARIABLES, "")},
                "alwaysLoad": True,
            }
        }
    }


def prepare_slot(
    slot_dir: Path,
    *,
    test_file: Path,
    initial_results: Path,
    arm: str,
    max_runs: int,
    session_dir: Path,
    variant: Variant,
    backend_spec: dict,
    config: LaunchConfig,
) -> None:
    """Empty and recreate the slot's five directories and its link, and fill them for a session.

    ``ws/`` gets ``tests/<test_file.name>`` and ``test-results/`` (a copy of
    ``initial_results``); ``h/`` gets the committed settings file, ``mcp.json``, the arm's user
    message and, for arms that have one, its system prompt; the link ``session`` points at
    ``session_dir``. ``session_dir`` (new or empty, outside the slot) gets ``session.json`` for
    the ``run_test`` server and ``original_tests/``, an untouched copy of the handed-out
    ``tests/``. Nothing is written until every input has been checked and every file assembled.
    """
    slot = Path(slot_dir).resolve()
    session = Path(session_dir).resolve()
    test_file, initial_results = Path(test_file), Path(initial_results)

    if not test_file.is_file():
        raise FileNotFoundError(f"test file not found: {test_file}")
    if not initial_results.is_dir():
        raise FileNotFoundError(f"initial results not found: {initial_results}")
    _check_visible(test_file.name, "the test file name")
    for name in _listing(initial_results):
        _check_visible(name, "the initial result")
    _check_visible(str(slot / "ws"), "the workspace path")  # the model is shown its cwd
    for directory in (slot, *slot.parents):
        for marker in PROJECT_MARKERS:
            if os.path.lexists(directory / marker):
                raise ValueError(
                    f"{directory / marker} exists, and the CLI looks for it from the workspace "
                    f"upward; put the slot outside every repository and project"
                )
    if _within(session, slot) or _within(slot, session):
        raise ValueError(
            "the session directory must lie outside the slot, where the agent cannot read it"
        )
    if session.exists() and any(session.iterdir()):
        raise FileExistsError(
            f"session directory {session} is not empty; every session needs a fresh one"
        )
    if not isinstance(variant, Variant):
        raise TypeError("variant must be a hoh.backend.Variant")
    if not isinstance(backend_spec, Mapping) or not isinstance(backend_spec.get("factory"), str):
        raise ValueError("backend_spec needs a 'factory' of the form 'package.module:callable'")

    prompt = user_message(arm, max_runs)
    appended = system_prompt(arm)
    settings = SETTINGS_FILE.read_bytes()
    json.loads(settings)  # in -p mode the CLI silently ignores a settings file that is invalid
    mcp_json = json.dumps(_mcp_config(slot, config), indent=2) + "\n"
    session_json = json.dumps(
        {
            "workspace": str(slot / "ws"),
            "original_tests": str(session / ORIGINAL_TESTS),
            "variant": asdict(variant),
            "max_runs": max_runs,
            "backend": dict(backend_spec),
        },
        indent=2,
    ) + "\n"

    slot.mkdir(parents=True, exist_ok=True)
    _end_slot_processes(slot)
    for name in (*SLOT_DIRS, SESSION_LINK):
        path = slot / name
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    for name in SLOT_DIRS:
        (slot / name).mkdir(mode=0o700 if name in ("cfg", "home") else 0o777)
    ws, h = slot / "ws", slot / "h"
    (ws / "tests").mkdir()
    shutil.copyfile(test_file, ws / "tests" / test_file.name)
    shutil.copytree(initial_results, ws / "test-results")
    (h / "settings.json").write_bytes(settings)
    (h / "mcp.json").write_text(mcp_json, encoding="utf-8")
    (h / "prompt.md").write_text(prompt, encoding="utf-8", newline="\n")
    if appended is not None:
        (h / "append.md").write_text(appended, encoding="utf-8", newline="\n")
    session.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ws / "tests", session / ORIGINAL_TESTS)
    (session / "session.json").write_text(session_json, encoding="utf-8")
    (slot / SESSION_LINK).symlink_to(session, target_is_directory=True)


def _end_slot_processes(slot: Path, grace: float = STOP_GRACE_S) -> list[int]:
    """End the process groups of processes whose command line names the slot: a CLI and its
    run_test server left running by a harness that was killed. Returns the groups ended.

    Nothing else names a slot's directories (the CLI gets ``h/mcp.json`` and ``h/settings.json``,
    the server ``--session-dir <slot>/session``), so nothing else is touched; this process's
    own group never is.
    """
    needle = f"{slot}{os.sep}"
    try:
        listing = subprocess.run(
            ["ps", "-ax", "-ww", "-o", "pid=,pgid=,command="], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("slot %s: cannot list processes (%s); leftovers are not looked for", slot, exc)
        return []
    own = {os.getpgrp(), 0, 1}
    groups = set()
    for line in listing.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and needle in fields[2] and fields[1].isdigit():
            if int(fields[1]) not in own and int(fields[0]) != os.getpid():
                groups.add(int(fields[1]))
    for pgid in sorted(groups):
        log.warning("slot %s: ending process group %d, left by an earlier session", slot, pgid)
        _killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(_group_alive(g) for g in groups):
        time.sleep(0.05)
    for pgid in groups:
        if _group_alive(pgid):
            _killpg(pgid, signal.SIGKILL)
    return sorted(groups)


# --- the launch


def _check_session_id(session_id: str) -> None:
    try:
        canonical = str(uuid.UUID(session_id))
    except (TypeError, ValueError, AttributeError):
        canonical = None
    if canonical != session_id:
        raise ValueError("session_id must be a UUID in canonical lowercase form")


def build_command(
    slot_dir: Path, credential: Credential, config: LaunchConfig, session_id: str, *, append: bool
) -> tuple[list[str], dict[str, str]]:
    """The argument list and the complete environment of one launch.

    The command of research notes section 6a, with these changes:
    the MCP server is called ``runner``, so the tool is ``mcp__runner__run_test``; the main
    conversation's cache lifetime is ``config.cache_ttl`` (one hour) without
    ``FORCE_PROMPT_CACHING_5M``; the short system prompt is chosen explicitly. The environment
    starts empty and holds exactly one credential. The prompt goes on standard input.
    """
    if not isinstance(credential, Credential):
        raise TypeError("credential must be a hoh.session.Credential")
    _check_session_id(session_id)
    slot = Path(slot_dir).resolve()
    h = slot / "h"
    argv = [
        str(config.claude_bin), "-p",
        "--model", config.model, "--effort", config.effort,
        "--restricted",
        "--setting-sources", "user", "--settings", str(h / "settings.json"),
        "--strict-mcp-config", "--mcp-config", str(h / "mcp.json"),
        "--tools", BUILTIN_TOOLS,
        "--allowedTools", EDIT_RULE, RUN_TEST_TOOL,
        "--permission-mode", "dontAsk", "--permission-prompts", "none",
        "--disable-slash-commands", "--no-chrome",
        "--system-prompt-snapshot", "on",
    ]
    if append:
        argv += ["--append-system-prompt-file", str(h / "append.md")]
    argv += [
        "--max-turns", str(config.max_turns), "--max-budget-usd", str(float(config.max_budget_usd)),
        "--session-id", session_id,
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
    ]

    env = LaunchEnv()
    env["HOME"] = str(slot / "home")
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    env["SHELL"] = "/bin/zsh"
    env["LANG"] = "en_US.UTF-8"
    env["TZ"] = "UTC"
    env["CLAUDE_CONFIG_DIR"] = str(slot / "cfg")
    env[credential.env_name] = credential.value
    env.update(
        {
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_GROWTHBOOK": "1",
            "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
            "CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT": "1",
            "CLAUDE_CODE_EFFORT_LEVEL": config.effort,
            "CLAUDE_CODE_PROMPT_CACHE_TTL": config.cache_ttl,
            "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
            "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
            "CLAUDE_CODE_DISABLE_ARTIFACT": "1",
            "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
            "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "false",
            "ENABLE_TOOL_SEARCH": "false",
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
            "MCP_TIMEOUT": "30000",
            # Never shorter than the hard cap: a call the CLI abandons keeps running in the
            # server, which counts it and rewrites test-results/ behind the agent. A call that
            # hangs is ended by the hard cap instead, as an environment failure.
            "MCP_TOOL_TIMEOUT": str(config.hard_wall_clock_s * 1000),
            "MAX_MCP_OUTPUT_TOKENS": "25000",
            "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS": "0",
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
            # Off, and said so: in 2.1.325 a set scrub forces the permission mode to "default"
            # whatever --permission-mode says, and sandboxes subprocesses; left unset, other
            # variables (GITHUB_ACTIONS, the entry point) can switch scrubbing or the sandbox
            # on. The run_test server is kept free of the credential by the MCP configuration.
            "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "0",
            "CLAUDE_CODE_STARTUP_FAILURE_RESULTS": "1",
            "CLAUDE_CODE_MAX_RETRIES": "10",
            "API_TIMEOUT_MS": "600000",
        }
    )
    return argv, env


# --- reading a stream


_MISSING = object()


def _loads(text: str | bytes) -> dict | None:
    try:
        event = json.loads(text)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _parse(lines: Iterable[str | bytes]) -> list[dict]:
    """One dict per non-blank line; a line that is not a JSON object becomes a placeholder."""
    events = []
    for raw in lines:
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", errors="replace")
        if not raw.strip():
            continue
        event = _loads(raw)
        events.append(event if event is not None else {"type": None, "unparsed": True})
    return events


def _is_init(event: Any) -> bool:
    return (
        isinstance(event, dict)
        and event.get("type") == "system"
        and event.get("subtype") == "init"
    )


def _text(event: Mapping, key: str) -> str | None:
    value = event.get(key)
    return value if isinstance(value, str) else None


def _clip(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _show(value: Any) -> str:
    return _clip(json.dumps(value, default=str, ensure_ascii=False))


def _difference(key: str, got: Any, want: Any) -> str:
    if got is _MISSING:
        return f"{key} is missing; the reference has {_show(want)}"
    if want is _MISSING:
        return f"{key} is {_show(got)}; the reference has none"
    if isinstance(got, list) and isinstance(want, list):
        missing = [x for x in want if x not in got]
        extra = [x for x in got if x not in want]
        if not missing and not extra:
            return f"{key}: the same entries in a different order or number"
        parts = ([f"missing {_show(missing)}"] if missing else []) + (
            [f"extra {_show(extra)}"] if extra else []
        )
        return f"{key} differ from the reference: {', '.join(parts)}"
    return f"{key} is {_show(got)}; the reference has {_show(want)}"


def _init_problems(init: Any, reference: Mapping[str, Any] | None) -> list[str]:
    """Section 6b step 3 item 1: the checks every init must pass, then the reference fields."""
    if not _is_init(init):
        return ["the first line is not the init event"]
    problems = []
    servers = init.get("mcp_servers")
    entries = [s for s in servers if isinstance(s, dict)] if isinstance(servers, list) else []
    runner = [s for s in entries if s.get("name") == SERVER_KEY]
    if not runner:
        problems.append(f"MCP server {SERVER_KEY!r} is missing")
    elif len(runner) > 1:
        problems.append(f"more than one MCP server is called {SERVER_KEY!r}")
    elif runner[0].get("status") != "connected":
        problems.append(
            f"MCP server {SERVER_KEY!r} is {_show(runner[0].get('status'))}, not 'connected'"
        )
    others = [s.get("name") for s in entries if s.get("name") != SERVER_KEY]
    if others or (isinstance(servers, list) and len(entries) != len(servers)):
        problems.append(f"unexpected MCP servers: {_show(others)}")
    tools = init.get("tools")
    if not isinstance(tools, list) or RUN_TEST_TOOL not in tools:
        problems.append(f"the tool {RUN_TEST_TOOL} is not offered")
    if init.get("permissionMode") != "dontAsk":
        problems.append(f"permissionMode is {_show(init.get('permissionMode'))}, not 'dontAsk'")
    for key in ("mcp_server_errors", "plugin_errors"):
        if init.get(key):
            problems.append(f"{key}: {_show(init[key])}")
    if reference is not None:
        for key in INIT_FIELDS:
            got, want = init.get(key, _MISSING), reference.get(key, _MISSING)
            if got != want:
                problems.append(_difference(key, got, want))
    return problems


def check_init(
    init: dict, reference: Mapping[str, Any] | None, credential: Credential
) -> list[str]:
    """The problems with an init event; empty when the session may be kept.

    Section 6b step 3: item 1 (the fields that must equal the calibration reference, no MCP or
    plugin errors, the ``runner`` server connected and its tool offered) and item 2 (the
    credential the CLI reports using is the one injected). With ``reference`` None only the
    checks that need no reference are made.
    """
    problems = _init_problems(init, reference)
    if _is_init(init):
        expected = API_KEY_SOURCE[credential.kind]
        if init.get("apiKeySource") != expected:
            problems.append(
                f"apiKeySource is {_show(init.get('apiKeySource'))}; the {credential.kind} "
                f"credential that was injected gives {expected!r}"
            )
    return problems


def _pinned_problems(init: Any, config: LaunchConfig) -> list[str]:
    if not _is_init(init):
        return []
    problems = []
    if init.get("claude_code_version") != config.cli_version:
        problems.append(
            f"claude_code_version is {_show(init.get('claude_code_version'))}; the pinned "
            f"version is {config.cli_version!r}"
        )
    if init.get("model") != config.model:
        problems.append(
            f"model is {_show(init.get('model'))}; the pinned model is {config.model!r}"
        )
    return problems


# --- reset times


_RESETS = re.compile(r"\bresets\b\s*(?P<tail>.*)", re.IGNORECASE | re.DOTALL)
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60}
_DURATION = r"(\d+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m)\b"
_RELATIVE = re.compile(rf"in\s+((?:{_DURATION}[\s,]*(?:and\s+)?)+)", re.IGNORECASE)
_ZONE = re.compile(r"\(([^()]+)\)")
_TIME_12 = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b", re.IGNORECASE)
_TIME_24 = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_WEEKDAY = re.compile(
    r"\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|sday|nesday|rsday|urday)?\b",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b",
    re.IGNORECASE,
)
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def resume_time_from_text(text: str, now: float) -> float | None:
    """The reset time a limit message names, in epoch seconds, or None when there is none.

    Reads the forms of the error reference and the one observed (research notes 5.3, 5.4):
    ``resets 3:45pm``, ``resets Mon 12:00am``, ``resets 2pm (America/Los_Angeles)``, and also
    a month and day (``resets Nov 21, 2pm``), a 24-hour time and ``resets in 3 hours``. A time
    without a zone in parentheses is UTC, the zone the CLI is launched with. The result is the
    first such moment after ``now``.
    """
    match = _RESETS.search(text.replace("’", "'"))
    if match is None:
        return None
    tail = match.group("tail").split("·")[0].strip()
    relative = _RELATIVE.match(tail)
    if relative:
        seconds = sum(
            int(n) * _UNIT_SECONDS[unit[0].lower()]
            for n, unit in re.findall(_DURATION, relative.group(1), re.IGNORECASE)
        )
        return now + seconds if seconds > 0 else None

    zone: tzinfo = UTC
    named = _ZONE.search(tail)
    if named:
        key = named.group(1).strip()
        if key.upper() not in ("UTC", "GMT", "Z"):
            try:
                zone = ZoneInfo(key)
            except (ZoneInfoNotFoundError, ValueError):
                return None  # an unknown zone: better no time than a wrong one
        tail = tail[: named.start()] + tail[named.end() :]

    hour = minute = 0
    twelve = _TIME_12.search(tail)
    twenty_four = None if twelve else _TIME_24.search(tail)
    if twelve:
        hour, minute = int(twelve.group(1)), int(twelve.group(2) or 0)
        if not (1 <= hour <= 12 and minute < 60):
            return None
        hour = hour % 12 + (12 if twelve.group(3).lower() == "p" else 0)
    elif twenty_four:
        hour, minute = int(twenty_four.group(1)), int(twenty_four.group(2))
    weekday, date = _WEEKDAY.search(tail), _DATE.search(tail)
    if not (twelve or twenty_four or weekday or date):
        return None

    local_now = datetime.fromtimestamp(now, zone)
    if date:
        month, day = _MONTHS[date.group(1)[:3].lower()], int(date.group(2))
        for year in (local_now.year, local_now.year + 1):
            try:
                moment = datetime(year, month, day, hour, minute, tzinfo=zone)
            except ValueError:
                return None
            if moment.timestamp() > now:
                return moment.timestamp()
        return None
    moment = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday:
        ahead = (_WEEKDAYS[weekday.group(1)[:3].lower()] - local_now.weekday()) % 7
        moment += timedelta(days=ahead)
        if moment.timestamp() <= now:
            moment += timedelta(days=7)
    elif moment.timestamp() <= now:
        moment += timedelta(days=1)
    return moment.timestamp()


def _epoch(value: Any) -> float | None:
    """``resetsAt`` as epoch seconds: a number (seconds, or milliseconds) or an ISO 8601 time."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            seconds = float(text)
        except ValueError:
            try:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return moment.timestamp()
    else:
        return None
    if not math.isfinite(seconds):
        return None
    return seconds / 1000.0 if seconds > 1e11 else seconds


def _rate_info(event: Mapping) -> Mapping:
    info = event.get("rate_limit_info")
    return info if isinstance(info, Mapping) else {}


def _resume_time(events: list[dict], texts: list[str], now: float) -> tuple[float, str]:
    """``resetsAt`` (a rejection's first), else the message text, else now + 15 minutes.

    A time that is not in the future is no reset time: resuming at once would only meet the
    limit again.
    """
    infos = [_rate_info(e) for e in events if e.get("type") == "rate_limit_event"]
    rejected = [i for i in infos if i.get("status") == "rejected"]
    for info in [*reversed(rejected), *reversed(infos)]:
        moment = _epoch(info.get("resetsAt"))
        if moment is not None and moment > now:
            return moment, "resetsAt"
    for text in texts:
        moment = resume_time_from_text(text, now)
        if moment is not None and moment > now:
            return moment, "the message text"
    return now + DEFAULT_PAUSE_SECONDS, f"no reset time given, {DEFAULT_PAUSE_SECONDS // 60} min"


# --- classification


def _working_at_the_stop(events: list[dict]) -> str | None:
    """None when the stream shows the agent at work up to the stop; else why it does not: no
    model turn at all, or the CLI was retrying a failed API request (its time then went into
    waiting for the API, not into the agent's work)."""
    if not any(e.get("type") == "assistant" for e in events):
        return "no model turn after the init"
    last = next((e for e in reversed(events) if e.get("type") != "rate_limit_event"), None)
    if last is not None and last.get("type") == "system" and last.get("subtype") == "api_retry":
        return "stopped while the CLI retried a failed API request"
    return None


def _verdict(cls: str, reason: str = "", resume_at: float | None = None) -> tuple[str, Outcome]:
    return cls, Outcome(OUTCOME_STATE[cls], f"{cls}: {reason}" if reason else cls, resume_at)


def _message_texts(event: Mapping) -> list[str]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [b["text"] for b in content if isinstance(b, Mapping) and isinstance(b.get("text"), str)]


def _is_limit_text(text: str) -> bool:
    text = text.replace("’", "'").strip()
    return text.startswith(_LIMIT_PREFIX) and _LIMIT_WORD.search(text) is not None


def classify_stream(
    lines: Iterable[str | bytes],
    exit_code: int | None,
    reference_init: Mapping[str, Any] | None,
    *,
    now: float | None = None,
) -> tuple[str, Outcome]:
    """The class of a finished session (research notes 5.5, in its order) and the queue outcome.

    ``reference_init`` is the init event of a calibration run, or None before there is one.
    ``now`` (epoch seconds) dates a limit's resume time; it defaults to the current time. The
    checks that need the credential or the pinned configuration are made by ``run_session``.
    """
    events = _parse(lines)
    now = time.time() if now is None else float(now)

    # 1. The init assertions; the first line must be the init event.
    if not events or not _is_init(events[0]):
        return _verdict("environment", "the first line is not the init event")
    problems = _init_problems(events[0], reference_init)
    if problems:
        return _verdict("environment", "init: " + "; ".join(problems))

    # 2. No final result line, or the exit code of SIGTERM.
    final = events[-1] if events[-1].get("type") == "result" else None
    if final is None:
        return _verdict("environment", "no final result line")
    if exit_code in _SIGTERM_EXITS:
        return _verdict("environment", f"terminated (exit code {exit_code})")

    failed = final.get("is_error") is True
    status = final.get("api_error_status")
    status = status if isinstance(status, int) and not isinstance(status, bool) else None
    errored = [e for e in events if e.get("type") == "assistant" and _text(e, "error")]
    categories = [e["error"] for e in errored]
    texts = [t for e in errored for t in _message_texts(e)]
    if failed:
        errors = final.get("errors")
        texts += [t for t in (final.get("result"),) if isinstance(t, str)]
        texts += [t for t in errors if isinstance(t, str)] if isinstance(errors, list) else []

    # 3a. Spent credit or a spend limit, before row 3: it can come as a 429, yet waiting does
    # not help, so the stage stops as for a refused credential (row 4).
    billing = (["billing_error"] if "billing_error" in categories else []) + [
        _clip(t) for t in texts if _BILLING_TEXT.search(t)
    ][:1]
    if billing:
        return _verdict("credential", "billing: " + ", ".join(billing))

    # 3. A usage or rate limit.
    rejected = any(
        _rate_info(e).get("status") == "rejected"
        for e in events
        if e.get("type") == "rate_limit_event"
    )
    limit_texts = [t for t in texts if _is_limit_text(t)]
    signals = (
        (["HTTP 429"] if failed and status == 429 else [])
        + (["assistant error rate_limit"] if "rate_limit" in categories else [])
        + (["error after a rejected rate_limit_event"] if failed and rejected else [])
        + (["limit message"] if limit_texts else [])
    )
    if signals:
        resume_at, source = _resume_time(events, limit_texts + texts, now)
        what = _clip(limit_texts[0]) if limit_texts else ", ".join(signals)
        return _verdict("limit", f"{what} (resume time from {source})", resume_at)

    # 4. The credential was refused.
    refused = [c for c in categories if c in _CREDENTIAL_ERRORS]
    if refused or (failed and status in (401, 403)):
        return _verdict("credential", ", ".join(dict.fromkeys(refused)) or f"HTTP {status}")

    # 5. Capacity: overloaded, or a server error, after the CLI's own retries.
    overloaded = [c for c in categories if c in _CAPACITY_ERRORS]
    if overloaded or (failed and status is not None and (status == 529 or 500 <= status <= 599)):
        retries = sum(1 for e in events if e.get("subtype") == "api_retry")
        cause = ", ".join(dict.fromkeys(overloaded)) or f"HTTP {status}"
        return _verdict("capacity", f"{cause} after {retries} retries")

    # 6. A safety-triggered model switch, or a refusal without one.
    for event in events:
        kind = next((v for v in (_text(event, "subtype"), _text(event, "type")) if v in _REFUSALS),
                    None)
        if kind:
            return _verdict(
                "model_changed",
                f"{kind}: {event.get('original_model')} -> {event.get('fallback_model')}",
            )

    # 7. The turn or budget cap.
    subtype = _text(final, "subtype")
    if subtype in _CENSORING:
        return _verdict("censored", subtype)

    # 8. A normal end.
    if final.get("is_error") is False and final.get("terminal_reason") in (None, "completed"):
        return _verdict("completed")

    return _verdict(
        "environment",
        "unrecognized ending: "
        + ", ".join(
            f"{key}={_show(final.get(key))}"
            for key in ("subtype", "is_error", "api_error_status", "terminal_reason")
        ),
    )


# --- running a session


class _Watch:
    """What the stream reader has seen, shared with the thread that waits for the process."""

    def __init__(self) -> None:
        self.first_seen = False
        self.init: dict | None = None
        self.problems: list[str] = []
        self.result_at: float | None = None
        self.abort = threading.Event()
        self._lock = threading.Lock()
        self._calls: dict[str, float] = {}  # run_test calls under way: tool_use id -> start

    def call_started(self, tool_id: str, at: float) -> None:
        with self._lock:
            self._calls.setdefault(tool_id, at)

    def call_ended(self, tool_id: str) -> None:
        with self._lock:
            self._calls.pop(tool_id, None)

    def call_running(self, now: float) -> float:
        """Seconds since the oldest run_test call the stream shows unanswered began."""
        with self._lock:
            return now - min(self._calls.values()) if self._calls else 0.0


def _blocks(event: Mapping) -> list[Mapping]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    return [b for b in content if isinstance(b, Mapping)] if isinstance(content, list) else []


def _pump(
    pipe: IO[bytes],
    path: Path,
    watch: _Watch,
    check: Callable[[dict | None], list[str]],
    abort_on_bad_init: bool,
) -> None:
    """Copy the CLI's stdout to ``path`` line by line, check the init event as it arrives, and
    note when run_test calls start and end."""
    with open(path, "wb") as sink:
        for raw in iter(pipe.readline, b""):
            sink.write(raw)
            sink.flush()
            if not raw.strip():
                continue
            event = _loads(raw)
            if not watch.first_seen:
                watch.first_seen = True
                watch.init = event if _is_init(event) else None
                try:
                    watch.problems = check(watch.init)
                except Exception as exc:  # a crash here must not leave the session running
                    watch.problems = [f"the init check failed: {type(exc).__name__}"]
                if watch.problems and abort_on_bad_init:
                    watch.abort.set()
            if event is None:
                continue
            kind = event.get("type")
            if kind == "assistant":
                for block in _blocks(event):
                    if (
                        block.get("type") == "tool_use"
                        and block.get("name") == RUN_TEST_TOOL
                        and isinstance(block.get("id"), str)
                    ):
                        watch.call_started(block["id"], time.monotonic())
            elif kind == "user":
                for block in _blocks(event):
                    if block.get("type") == "tool_result" and isinstance(
                        block.get("tool_use_id"), str
                    ):
                        watch.call_ended(block["tool_use_id"])
            elif kind == "result":
                watch.result_at = time.monotonic()


def _number(value: Any) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    )


def _state_tool_time(path: Path) -> tuple[float, float | None]:
    """From the server's ``run_test_state.json``: the seconds of finished calls, and the epoch
    time at which the oldest call still under way (started, no ``seconds`` yet) began."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0, None
    calls = state.get("calls") if isinstance(state, dict) else None
    done, running_since = 0.0, None
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict):
            continue
        seconds, started = call.get("seconds"), call.get("started_at")
        if _number(seconds):
            done += max(0.0, float(seconds))
        elif _number(started):
            running_since = started if running_since is None else min(running_since, started)
    return done, running_since


def _tool_seconds(state_file: Path, watch: _Watch) -> float:
    """Seconds spent inside run_test calls so far.

    Finished calls and the call under way come from the server's state file. A call the stream
    shows started and unanswered counts too, so that a server which records a call only when it
    ends cannot make the agent's time look longer than it is; for a call both show, the larger
    running time is taken. The stream is read first: the server records a finished call before
    it answers, so a call that ends between the two reads is counted twice for a moment, never
    not at all.
    """
    streamed = watch.call_running(time.monotonic())
    done, running_since = _state_tool_time(state_file)
    running = max(0.0, time.time() - running_since) if running_since is not None else 0.0
    return done + max(running, streamed)


def _wait(
    proc: subprocess.Popen, watch: _Watch, state_file: Path, t0: float, config: LaunchConfig
) -> str | None:
    """Wait for the process; the reason to stop it, or None when it ended by itself."""
    while proc.poll() is None:
        if watch.abort.is_set():
            return "init"
        elapsed = time.monotonic() - t0
        if elapsed >= config.hard_wall_clock_s:
            return "hard_wall_clock"
        if elapsed - _tool_seconds(state_file, watch) >= config.wall_clock_s:
            return "wall_clock"
        watch.abort.wait(max(0.01, min(0.1, config.hard_wall_clock_s - elapsed)))
    return None


def _killpg(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        log.warning("not allowed to send %s to process group %d", sig.name, pgid)


def _stop(
    proc: subprocess.Popen, grace: float, session_id: str, reason: str
) -> list[tuple[str, float]]:
    """SIGINT, then SIGTERM after ``grace``, then SIGKILL to the whole group after ``grace``."""
    sent: list[tuple[str, float]] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        if proc.poll() is not None:
            return sent
        log.info("session %s: stopping (%s): %s", session_id, reason, sig.name)
        proc.send_signal(sig)
        sent.append((sig.name, time.monotonic()))
        try:
            proc.wait(timeout=grace)
            return sent
        except subprocess.TimeoutExpired:
            pass
    if proc.poll() is None:
        log.warning("session %s: stopping (%s): SIGKILL", session_id, reason)
        _killpg(proc.pid, signal.SIGKILL)
        sent.append(("SIGKILL", time.monotonic()))
        proc.wait()
    return sent


def _live_group_members(pgid: int) -> list[int] | None:
    """Processes of the group that have not ended, or None where /proc cannot tell.

    A zombie nobody reaps (in a container whose first process does not) has ended.
    """
    proc_root = Path("/proc")
    if not (proc_root / "self" / "stat").exists():
        return None
    members = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            fields = stat[stat.rfind(")") + 2 :].split()
            if fields[0] != "Z" and int(fields[2]) == pgid:
                members.append(int(entry.name))
        except (OSError, IndexError, ValueError):
            continue
    return members


def _group_alive(pgid: int) -> bool:
    members = _live_group_members(pgid)
    if members is not None:
        return bool(members)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# The process groups of the sessions this process is running, for an operator's stop: the CLI
# runs in a group of its own, so Ctrl-C in the terminal does not reach it.
_LIVE: dict[int, str] = {}
_LIVE_LOCK = threading.Lock()


def end_live_sessions(grace: float = STOP_GRACE_S) -> int:
    """End every agent session this process is running: SIGTERM to each session's process
    group, then SIGKILL to what is left after ``grace``. Returns how many there were."""
    with _LIVE_LOCK:
        groups = list(_LIVE)
    for pgid in groups:
        _killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(_group_alive(g) for g in groups):
        time.sleep(0.05)
    for pgid in groups:
        if _group_alive(pgid):
            _killpg(pgid, signal.SIGKILL)
    return len(groups)


def _end_group(pgid: int, grace: float) -> bool:
    """End what the CLI left running in its process group, such as an orphaned ``run_test``
    server that could otherwise write into the next session's workspace. True if anything
    was left."""
    if not _group_alive(pgid):
        return False
    deadline = time.monotonic() + min(grace, 2.0)
    while _group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.05)
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        if not _group_alive(pgid):
            break
        log.warning("process group %d outlived its CLI; sending %s", pgid, sig.name)
        _killpg(pgid, sig)
        deadline = time.monotonic() + wait
        while _group_alive(pgid) and time.monotonic() < deadline:
            time.sleep(0.05)
    return True


def _regular_files(root: Path) -> Iterable[Path]:
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_file() and not path.is_symlink():
                yield path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@lru_cache(maxsize=8)
def _sha256_cached(path: str, size: int, mtime_ns: int) -> str:
    return _sha256_file(Path(path))


def _binary_record(path: Path) -> dict[str, str]:
    try:
        real = path.resolve(strict=True)
        stat = real.stat()
    except OSError:
        real = None
    if real is None or not real.is_file():
        raise FileNotFoundError(f"the CLI binary {path} does not exist")
    return {
        "path": str(path),
        "resolved": str(real),
        "sha256": _sha256_cached(str(real), stat.st_size, stat.st_mtime_ns),
    }


def _tree_digest(ws: Path) -> list[tuple[str, str]]:
    """Every path in the workspace except ``test-results/``, with a hash of each file."""
    items = []
    for dirpath, dirnames, filenames in os.walk(ws):
        here = Path(dirpath)
        if here == ws and "test-results" in dirnames:
            dirnames.remove("test-results")
        for name in sorted(dirnames):
            items.append(((here / name).relative_to(ws).as_posix() + "/", ""))
        for name in sorted(filenames):
            path = here / name
            rel = path.relative_to(ws).as_posix()
            if path.is_symlink():
                items.append((rel, "link:" + os.readlink(path)))
            elif path.is_file():
                items.append((rel, _sha256_file(path)))
    return sorted(items)


def _contains(path: Path, needle: bytes) -> bool:
    keep = len(needle) - 1
    tail = b""
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            if needle in tail + block:
                return True
            tail = (tail + block)[-keep:] if keep else b""
    return False


def _redact_everywhere(secret: str, roots: Mapping[str, Path]) -> list[str]:
    """Search every file under the roots for ``secret``; redact each hit. The hits, by place."""
    needle = secret.encode("utf-8")
    found = []
    for label, root in roots.items():
        for path in _regular_files(root):
            try:
                if not _contains(path, needle):
                    continue
                path.write_bytes(path.read_bytes().replace(needle, REDACTED.encode()))
            except OSError as exc:
                log.warning("could not search %s for the credential: %s", path, exc.strerror)
                continue
            found.append(f"{label}:{path.relative_to(root).as_posix()}")
    return found


_RAN = frozenset({"pass", "fail", "not_executed", "broken"})
# A run_test result that the server did not write as the end of a run: the CLI gave up on the
# call or lost the server (MCP error -32001 timeout, -32000 connection closed, a server that is
# not connected), or the server failed without leaving a marker. Read only on results the CLI
# marked as errors, so a test log that mentions such words is never taken for one.
_TOOL_FAILURE = re.compile(r"\bMCP error\b|\bnot connected\b|\bdisconnected\b")
_SERVER_FAILURE_TEXTS = (ENV_FAILED_TEXT, HARNESS_ERROR_TEXT)


def _tool_result_text(block: Mapping[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"] for b in content if isinstance(b, Mapping) and isinstance(b.get("text"), str)
        )
    return ""


def run_test_problems(events: Iterable[Mapping[str, Any]], server_calls: int | None) -> list[str]:
    """Why the session's run_test calls cannot be taken as the runs the server reported.

    ``server_calls`` is the number of entries in the server's state file (None without one).
    A call whose result the CLI or a lost server wrote, or a call the server recorded that the
    session never made (another process wrote into this session directory), makes the session
    an environment failure. An error the agent caused, such as input the CLI refused, does not.
    """
    uses: list[str] = []
    results: dict[str, tuple[str, bool]] = {}
    for event in events:
        message = event.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if (event.get("type") == "assistant" and block.get("type") == "tool_use"
                    and block.get("name") == RUN_TEST_TOOL and block.get("id") not in uses):
                uses.append(str(block.get("id")))
            elif event.get("type") == "user" and block.get("type") == "tool_result":
                results[str(block.get("tool_use_id"))] = (
                    _tool_result_text(block), block.get("is_error") is True
                )
    problems = []
    for tool_id in uses:
        text, is_error = results.get(tool_id, ("", False))
        if is_error and (_TOOL_FAILURE.search(text) or text.strip() in _SERVER_FAILURE_TEXTS):
            problems.append(f"a run_test call ended with {_clip(text, 120)!r}, not with a run")
    if server_calls is not None and server_calls > len(uses):
        problems.append(
            f"the run_test server recorded {server_calls} calls, the session made {len(uses)}"
        )
    return problems


def _server_call_entries(session: Path) -> int | None:
    try:
        calls = json.loads((session / "run_test_state.json").read_text(encoding="utf-8"))["calls"]
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return len(calls) if isinstance(calls, list) else None


def _run_test_count(session: Path) -> int:
    """Calls that ran the test: their status is a run status, whether or not they counted."""
    try:
        calls = json.loads((session / "run_test_state.json").read_text(encoding="utf-8"))["calls"]
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    if not isinstance(calls, list):
        return 0
    return sum(1 for c in calls if isinstance(c, dict) and c.get("status") in _RAN)


def run_session(
    slot_dir: Path,
    session_dir: Path,
    credential: Credential,
    config: LaunchConfig,
    *,
    reference_init: Mapping[str, Any] | None = None,
    session_id: str | None = None,
    stop_grace_s: float = STOP_GRACE_S,
    abort_on_bad_init: bool = True,
) -> SessionResult:
    """Launch the CLI once in a slot that ``prepare_slot`` made, and classify how it ended.

    The CLI runs in its own process group with the user message on standard input; its output
    is copied line by line to ``out/stream.jsonl``. When the first line arrives it is checked
    (``check_init`` and the pinned version and model); with ``abort_on_bad_init`` a session
    whose init fails is stopped at once, since it will be discarded anyway. When the agent's
    own time (wall time minus time inside ``run_test`` calls) reaches ``config.wall_clock_s``,
    or the wall time reaches ``config.hard_wall_clock_s``, the CLI gets SIGINT,
    ``stop_grace_s`` later SIGTERM, and ``stop_grace_s`` after that its group gets SIGKILL.
    Whatever the CLI leaves running in its group is ended. ``launch.json`` and ``end.json`` in
    ``out/`` record the launch (with the credential redacted) and the ending. Nothing outside
    the slot and the session directory is touched.
    """
    if not isinstance(credential, Credential):
        raise TypeError("credential must be a hoh.session.Credential")
    if not isinstance(config, LaunchConfig):
        raise TypeError("config must be a hoh.session.LaunchConfig")
    if reference_init is not None and not isinstance(reference_init, Mapping):
        raise TypeError("reference_init must be an init event (a mapping) or None")
    slot = Path(slot_dir).resolve()
    session = Path(session_dir).resolve()
    ws, h, out = slot / "ws", slot / "h", slot / "out"
    needed = [slot / d for d in SLOT_DIRS] + [
        h / "settings.json", h / "mcp.json", h / "prompt.md", session / "session.json"
    ]
    for path in needed:
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing; run prepare_slot first")
    link = slot / SESSION_LINK
    if not link.is_symlink() or link.resolve() != session:
        raise ValueError(f"the slot {slot} was not prepared for the session directory {session}")
    if any(out.iterdir()) or (session / "run_test_state.json").exists():
        raise FileExistsError(f"the slot {slot} was used after it was prepared; prepare it again")
    binary = _binary_record(config.claude_bin)
    session_id = str(uuid.uuid4()) if session_id is None else session_id
    argv, env = build_command(
        slot, credential, config, session_id, append=(h / "append.md").is_file()
    )
    assert isinstance(env, LaunchEnv)
    stream_path = out / "stream.jsonl"
    before = _tree_digest(ws)
    atomic_write_json(
        out / "launch.json",
        {
            "session_id": session_id,
            "started_at": time.time(),
            "credential": credential.kind,
            "credential_variable": credential.env_name,
            "argv": argv,
            "env": env.redacted(),
            "cwd": str(ws),
            "config": config.record(),
            "cli": binary,
            "sha256": {
                name: _sha256_file(h / name)
                for name in ("settings.json", "mcp.json", "prompt.md", "append.md")
                if (h / name).is_file()
            },
            "reference_init": reference_init is not None,
        },
    )

    def check(init: dict | None) -> list[str]:
        if init is None:
            return ["the first line is not the init event"]
        try:
            return check_init(init, reference_init, credential) + _pinned_problems(init, config)
        except Exception as exc:  # a malformed init or reference must not crash the harness
            return [f"the init check failed: {type(exc).__name__}"]

    watch = _Watch()
    log.info("session %s: launching in %s with the %s credential", session_id, slot,
             credential.kind)
    t0 = time.monotonic()
    with open(h / "prompt.md", "rb") as stdin, open(out / "stderr.log", "wb") as stderr:
        proc = subprocess.Popen(
            argv, env=env, cwd=ws, stdin=stdin, stdout=subprocess.PIPE, stderr=stderr,
            start_new_session=True,
        )
    with _LIVE_LOCK:
        _LIVE[proc.pid] = session_id
    try:
        assert proc.stdout is not None
        reader = threading.Thread(
            target=_pump, args=(proc.stdout, stream_path, watch, check, abort_on_bad_init),
            name=f"session-stream-{session_id[:8]}", daemon=True,
        )
        reader.start()
        state_file = session / "run_test_state.json"
        stop_reason = _wait(proc, watch, state_file, t0, config)
        stop_at = time.monotonic() if stop_reason else None
        sent = _stop(proc, stop_grace_s, session_id, stop_reason) if stop_reason else []
        proc.wait()
        wall_s = time.monotonic() - t0
        tool_s = _tool_seconds(state_file, watch)
        agent_s = wall_s - tool_s
        reader.join(timeout=stop_grace_s)
        leftovers = _end_group(proc.pid, stop_grace_s)
    finally:
        with _LIVE_LOCK:
            _LIVE.pop(proc.pid, None)
    reader.join(timeout=stop_grace_s)
    if reader.is_alive():
        log.warning("session %s: its output is still open; classifying what arrived", session_id)
    else:
        proc.stdout.close()

    # The search comes first, so that nothing read from the stream below can carry the value.
    leaks = _redact_everywhere(credential.value, {"slot": slot, "session": session})

    raw = stream_path.read_bytes().splitlines() if stream_path.exists() else []
    events = _parse(raw)
    init = events[0] if events and _is_init(events[0]) else None
    final = events[-1] if events and events[-1].get("type") == "result" else None
    result_before_stop = (
        stop_at is not None and watch.result_at is not None and watch.result_at < stop_at
    )
    # A CLI that wrote its result in time and only lingered: the exit code is the harness's.
    exit_for_table = None if result_before_stop else proc.returncode
    cls, outcome = classify_stream(raw, exit_for_table, reference_init, now=time.time())
    init_problems = check(init)
    edited = _tree_digest(ws) != before
    markers = [name for name in FAILURE_MARKERS if (session / name).exists()]

    if init_problems:
        cls, outcome = _verdict("environment", "init: " + "; ".join(init_problems))
    elif stop_reason == "wall_clock" and not result_before_stop and cls not in _EXTERNAL:
        limit = (
            f"stopped at the wall clock: {agent_s:.1f} s of the agent's own time (cap "
            f"{config.wall_clock_s} s), besides {tool_s:.1f} s inside run_test"
        )
        working = _working_at_the_stop(events)
        if final is not None:
            cls, outcome = _verdict("censored", f"{limit}; a result line followed the stop")
        elif edited:
            cls, outcome = _verdict("censored", f"{limit}; the workspace was edited")
        elif working is None:
            # Analysis plan section 8: a session at the cap is kept and judged as it stood,
            # unchanged here; rerunning it would resample the agents that think longest.
            cls, outcome = _verdict("censored", f"{limit}; the agent was still working, with "
                                                f"no edits")
        else:
            cls, outcome = _verdict("environment", f"{limit}; no result line and no edits, "
                                                   f"{working}")
    elif stop_reason == "hard_wall_clock" and not result_before_stop and cls not in _EXTERNAL:
        # The agent's own time was under its cap, so run_test ate the rest: the environment.
        cls, outcome = _verdict(
            "environment",
            f"stopped at the hard wall clock ({config.hard_wall_clock_s} s) with "
            f"{tool_s:.1f} s inside run_test and {agent_s:.1f} s of the agent's own time",
        )
    if markers:
        cls, outcome = _verdict(
            "environment", f"the session directory holds {', '.join(markers)}"
        )
    elif cls in ("completed", "censored"):
        tool_problems = run_test_problems(events, _server_call_entries(session))
        if tool_problems:
            cls, outcome = _verdict("environment", "; ".join(tool_problems))
    if leaks:
        cls, outcome = _verdict(
            "credential",
            "the credential value was found, and redacted, in " + ", ".join(leaks),
        )
        log.error("session %s: the credential value was found on disk: %s", session_id,
                  ", ".join(leaks))

    calls = _run_test_count(session)
    atomic_write_json(
        out / "end.json",
        {
            "session_id": session_id,
            "class": cls,
            "outcome": {
                "state": outcome.state, "detail": outcome.detail, "resume_at": outcome.resume_at
            },
            "exit_code": proc.returncode,
            "wall_s": round(wall_s, 3),
            "tool_s": round(tool_s, 3),
            "agent_s": round(agent_s, 3),
            "stop": {
                "reason": stop_reason,
                "signals": [{"signal": n, "at_s": round(t - t0, 3)} for n, t in sent],
            },
            "init_problems": init_problems,
            "result_line": final is not None,
            "result_before_stop": result_before_stop,
            "edited": edited,
            "failure_markers": markers,
            "run_test_calls": calls,
            "api_retries": sum(1 for e in events if e.get("subtype") == "api_retry"),
            "compact_boundaries": sum(1 for e in events if e.get("subtype") == "compact_boundary"),
            "rate_limit_events": sum(1 for e in events if e.get("type") == "rate_limit_event"),
            "leftover_processes_ended": leftovers,
            "credential_found_in": leaks,
        },
    )
    log.info("session %s: %s (exit code %s, %.1f s)", session_id, outcome.detail,
             proc.returncode, wall_s)
    return SessionResult(
        outcome=outcome,
        session_id=session_id,
        stream_path=stream_path,
        exit_code=proc.returncode,
        wall_s=wall_s,
        init=init,
        result=final,
        run_test_calls=calls,
        end_class=cls,
    )
