"""The fidelity check's sessions: Playwright's own healer, run as ``playwright init-agents
--loop=claude`` sets it up (the fidelity plan's decisions 2, 3, 9 and 10; design doc 5.6, last
bullet).

A session is the study's session (``hoh.session``) with these changes, and no others:

- The CLI runs as the healer: ``--agent playwright-test-healer``, the agent that Playwright
  1.65.1 generates, given inline with ``--agents`` (``inline_agents``: its name, description,
  tools and model, and its body as the prompt; only the header's ``color`` is left out). The
  study's launch keeps ``--restricted``, which stops CLI 2.1.325 from loading agents from the
  configuration directory, so the file itself is not read by the CLI: it is kept in ``h/``, the
  harness's inputs, and nothing of Claude Code's enters the workspace or ``cfg/``. The file
  route without ``--restricted`` and the inline route with it send the same system prompt and
  tools (``tests/test_fidelity_cli.py``); ``--restricted`` also turns off the CLI's messaging
  socket, as in H's sessions. The
  agent's body must be the vendored healer's body (``hoh.prompts.UPSTREAM_FILE``), and its tools
  Playwright's mapping of the vendored list.
- ``--tools`` and ``--allowedTools`` are the agent's tools: its built-in tools, and
  ``Edit(./**)`` with its nine ``mcp__playwright-test__*`` tools. Nothing is appended to the
  system prompt.
- The one MCP server is ``playwright-test``, Playwright's ``run-test-mcp-server`` in a container
  (``docker run``, built by ``hoh.fidelity.slot``), started through ``hoh.fidelity.proxy``,
  which relays every byte and writes each MCP call's time into the session directory's
  ``run_test_state.json``, the file ``hoh.session`` reads for the time inside ``run_test``. So
  the agent's own time is wall time less the time inside MCP calls, resets included, as the
  study counts it.
- The user message is H's task line (``prompts/user_H.txt``) alone. The operating note is left
  out: it describes ``run_test`` and a ``test-results/`` that holds the failing run before the
  session starts, and this arm has neither; the healer runs the test itself, and Playwright
  writes its own ``test-results/``.
- The checks of decision 3: the init event (the pinned version and model, the credential, the
  permission mode, the healer as the one agent, one MCP server and the agent's tools, no MCP or
  plugin errors); every assistant message and every ``modelUsage`` key is the pinned model; the
  system prompt the CLI sent (its request shape in the transcript, ``--system-prompt-snapshot
  on``) holds the healer's body. A session that fails one is not labeled, and the runs stop for
  a look (``check_failed``), except a bad init whose one server did not come up (failed, or still
  pending) or that reports MCP server errors: that one is the environment's (decision 9).
- Decision 9: an environment record in the control directory (a test whose fixture failed to
  get a fresh stack, to connect or to wait), a failed reset, or an MCP call that failed because
  the server did, makes a session that would be kept (``completed`` or ``censored``) an
  environment failure: discarded and queued again, never labeled; a session of any other class
  keeps it. Each records what ran: the test files as given or edited, the records, and the last
  MCP call.

The workspace holds ``tests/<task>.spec.ts`` and, once the healer runs a test, Playwright's
``test-results/``, whose names Playwright makes from the test's file, title and project.
``prepare_slot`` checks the test file's name, the workspace's path and the control directory's
against the words the agent must not see.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from hoh import session as study
from hoh.fidelity import slot as slot_module
from hoh.prompts import PROMPTS_DIR, UPSTREAM_FILE
from hoh.queue import Outcome, atomic_write_json
from hoh.run_test_server import CREDENTIAL_VARIABLES

log = logging.getLogger(__name__)

AGENT_NAME = "playwright-test-healer"
AGENT_MODEL = "sonnet"
"""The model the vendored healer names; the CLI's ``--model`` pins the full ID."""
SERVER_KEY = "playwright-test"
MCP_PREFIX = f"mcp__{SERVER_KEY}__"
STATE_FILE = "run_test_state.json"
TASK_FILE = PROMPTS_DIR / "user_H.txt"
CLAUDE_TOOL_MAP = {"search": ("Glob", "Grep", "Read", "LS"), "edit": ("Edit", "MultiEdit", "Write")}
"""Playwright 1.65.1's ``ClaudeGenerator``: how a vendored tool becomes Claude Code's tools."""


# --- the agent file


CARRIED_KEYS = ("name", "description", "tools", "model")
"""The keys of the agent file's header that the inline definition carries (``inline_agents``)."""
DROPPED_KEYS = ("color",)
"""The one key of Playwright's header it leaves out: the colour of the agent's name in a
terminal."""


@dataclass(frozen=True)
class Agent:
    """An agent file as ``read_agent`` found it."""

    name: Any
    model: Any
    tools: tuple[str, ...]
    body: str
    builtin_tools: tuple[str, ...]
    mcp_tools: tuple[str, ...]
    description: Any = None
    header_keys: tuple[str, ...] = ()


def _front_matter(text: str) -> tuple[dict, str]:
    """The YAML header and what follows it, split as Playwright's ``agentParser`` splits them."""
    lines = text.split("\n")
    if lines[0] != "---":
        raise ValueError("an agent file starts with a YAML header (---)")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise ValueError("the agent file's YAML header is not closed (---)") from None
    header = yaml.safe_load("\n".join(lines[1:end]))
    if not isinstance(header, dict):
        raise ValueError("the agent file's YAML header is not a mapping")
    return header, "\n".join(lines[end + 1:])


def read_agent(text: str) -> Agent:
    """Parse an agent file. Raises ``ValueError`` on one Claude Code could not read as Playwright
    writes it: no closed YAML header, or tools that are not a list of names."""
    header, content = _front_matter(text)
    tools = header.get("tools")
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(",") if t.strip()]
    if not isinstance(tools, list) or not tools or not all(isinstance(t, str) for t in tools):
        raise ValueError(f"the agent file's tools are not a list of names: {tools!r}")
    return Agent(
        name=header.get("name"),
        model=header.get("model"),
        tools=tuple(tools),
        body=content.strip(),
        builtin_tools=tuple(t for t in tools if not t.startswith("mcp__")),
        mcp_tools=tuple(t for t in tools if t.startswith("mcp__")),
        description=header.get("description"),
        header_keys=tuple(str(key) for key in header),
    )


def vendored_agent() -> tuple[tuple[str, ...], str]:
    """The tools and the body Playwright 1.65.1 makes of the vendored healer: its ``agentSpec``
    maps each tool (``CLAUDE_TOOL_MAP``, ``server/tool`` to ``mcp__server__tool``) and keeps
    the text before the first ``<example>``, trimmed."""
    header, content = _front_matter(UPSTREAM_FILE.read_text(encoding="utf-8"))
    tools: list[str] = []
    for tool in header["tools"]:
        first, _, second = tool.partition("/")
        tools += [f"mcp__{first}__{second}"] if second else CLAUDE_TOOL_MAP.get(first, (first,))
    return tuple(tools), content.split("<example>")[0].strip()


def agent_problems(agent: Agent) -> list[str]:
    """Why ``agent`` is not the healer as Playwright generates it from the vendored file (decision
    2): its name, its model, its tools in order, and its body. Empty when it is."""
    tools, body = vendored_agent()
    problems = []
    if agent.name != AGENT_NAME:
        problems.append(f"the agent's name is {agent.name!r}, not {AGENT_NAME!r}")
    if agent.model != AGENT_MODEL:
        problems.append(f"the agent's model is {agent.model!r}, not {AGENT_MODEL!r}")
    if agent.tools != tools:
        missing = [t for t in tools if t not in agent.tools]
        extra = [t for t in agent.tools if t not in tools]
        problems.append(
            "the agent's tools are not Playwright's mapping of the vendored list"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; not in it: {', '.join(extra)}" if extra else "")
            + ("; out of order" if not missing and not extra else "")
        )
    if agent.body != body:
        problems.append("the agent's body is not the vendored healer's body")
    if not isinstance(agent.description, str) or not agent.description.strip():
        problems.append("the agent's description is not a text")
    unknown = [k for k in agent.header_keys if k not in CARRIED_KEYS + DROPPED_KEYS]
    if unknown:
        problems.append(f"the agent's header has keys the inline definition does not carry: "
                        f"{', '.join(unknown)}")
    return problems


def inline_agents(agent: Agent) -> str:
    """The value of ``--agents``: the healer defined inline, from its file field by field (the
    name as the key; ``CARRIED_KEYS``, the body as its prompt). ``--restricted``, which the study's
    launch keeps, stops CLI 2.1.325 from loading agents from the configuration directory."""
    return json.dumps({AGENT_NAME: {"description": agent.description, "tools": list(agent.tools),
                                    "model": agent.model, "prompt": agent.body}})


# --- the launch


def mcp_config(slot: Path, config: study.LaunchConfig, server_argv: Sequence[str]) -> dict:
    """The one MCP server, ``playwright-test``: ``server_argv`` (the ``docker run`` line) under
    ``hoh.fidelity.proxy``, which writes the calls' times into the session directory, reached
    through the slot's link as the study's server is."""
    if not server_argv or not all(isinstance(a, str) and a for a in server_argv):
        raise ValueError("server_argv must be a non-empty list of arguments")
    state = Path(slot) / study.SESSION_LINK / STATE_FILE
    return {
        "mcpServers": {
            SERVER_KEY: {
                "type": "stdio",
                "command": str(config.python),
                # -I as for the study's server: the proxy starts in the workspace.
                "args": ["-I", "-m", "hoh.fidelity.proxy", "--state", str(state), "--",
                         *server_argv],
                # As the study's: empty values over the credential the CLI passes on.
                "env": {**config.server_env, **dict.fromkeys(CREDENTIAL_VARIABLES, "")},
                "alwaysLoad": True,
            }
        }
    }


def build_command(
    slot_dir: Path, credential: study.Credential, config: study.LaunchConfig, session_id: str,
    agent: Agent,
) -> tuple[list[str], dict[str, str]]:
    """The study's launch (``hoh.session.build_command``, nothing appended to the system prompt)
    with the agent's tools in place of H's, the agent given inline (``--agents``), and
    ``--agent``. The environment is the study's."""
    argv, env = study.build_command(slot_dir, credential, config, session_id, append=False)
    tools = argv.index("--tools")
    allowed = argv.index("--allowedTools")
    if argv[tools + 1] != study.BUILTIN_TOOLS or argv[allowed + 1: allowed + 3] != [
        study.EDIT_RULE, study.RUN_TEST_TOOL
    ]:
        raise RuntimeError("hoh.session.build_command no longer gives the tools this module "
                           "replaces; read it again before launching")
    argv[allowed + 1: allowed + 3] = [study.EDIT_RULE, *agent.mcp_tools,
                                      "--agents", inline_agents(agent), "--agent", AGENT_NAME]
    argv[tools + 1] = ",".join(agent.builtin_tools)
    return argv, env


def prepare_slot(
    slot_dir: Path,
    *,
    test_file: Path,
    session_dir: Path,
    agent_file: Path,
    control_dir: Path,
    server_argv: Sequence[str],
    config: study.LaunchConfig,
) -> None:
    """Empty and recreate the slot's directories and its link, and fill them for a session.

    ``ws/`` gets ``tests/<test_file.name>``; ``h/`` the study's settings file, ``mcp.json``, the
    user message and ``agents/`` with the agent file, byte for byte (the CLI is given it inline;
    ``cfg/`` stays empty, as H's); the link ``session``
    points at ``session_dir`` (new or empty, outside the slot), which gets ``original_tests/``,
    an untouched copy of the handed-out ``tests/``; ``control_dir``, which the server's container
    and the watcher share (``pwfix``'s ``lib/control.js``), is emptied. Nothing is written until
    every input has been checked.
    """
    slot = Path(slot_dir).resolve()
    session = Path(session_dir).resolve()
    control = Path(control_dir).resolve()
    test_file, agent_file = Path(test_file), Path(agent_file)
    if not test_file.is_file():
        raise FileNotFoundError(f"test file not found: {test_file}")
    study._check_visible(test_file.name, "the test file name")
    study._check_visible(str(slot / "ws"), "the workspace path")
    # The server's container gets its path; a test the agent writes runs there.
    study._check_visible(str(control), "the control directory")
    if study._within(control, slot / "ws") or study._within(slot / "ws", control) or (
        study._within(control, session) or study._within(session, control)
    ):
        raise ValueError(f"the control directory {control} must lie outside the workspace and "
                         f"the session directory")
    for directory in (slot, *slot.parents):
        for marker in study.PROJECT_MARKERS:
            if os.path.lexists(directory / marker):
                raise ValueError(
                    f"{directory / marker} exists, and the CLI looks for it from the workspace "
                    f"upward; put the slot outside every repository and project"
                )
    if study._within(session, slot) or study._within(slot, session):
        raise ValueError(
            "the session directory must lie outside the slot, where the agent cannot read it"
        )
    if session.exists() and any(session.iterdir()):
        raise FileExistsError(
            f"session directory {session} is not empty; every session needs a fresh one"
        )
    agent_bytes = agent_file.read_bytes()
    problems = agent_problems(read_agent(agent_bytes.decode("utf-8")))
    if problems:
        raise ValueError(f"{agent_file}: " + "; ".join(problems))
    prompt = TASK_FILE.read_text(encoding="utf-8").strip()
    settings = study.SETTINGS_FILE.read_bytes()
    json.loads(settings)  # in -p mode the CLI silently ignores a settings file that is invalid
    mcp_json = json.dumps(mcp_config(slot, config, server_argv), indent=2) + "\n"

    slot.mkdir(parents=True, exist_ok=True)
    study._end_slot_processes(slot)
    for name in (*study.SLOT_DIRS, study.SESSION_LINK):
        path = slot / name
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    for name in study.SLOT_DIRS:
        (slot / name).mkdir(mode=0o700 if name in ("cfg", "home") else 0o777)
    ws, h = slot / "ws", slot / "h"
    (ws / "tests").mkdir()
    shutil.copyfile(test_file, ws / "tests" / test_file.name)
    (h / "agents").mkdir()
    (h / "agents" / f"{AGENT_NAME}.md").write_bytes(agent_bytes)
    (h / "settings.json").write_bytes(settings)
    (h / "mcp.json").write_text(mcp_json, encoding="utf-8")
    (h / "prompt.md").write_text(prompt, encoding="utf-8", newline="\n")
    session.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ws / "tests", session / study.ORIGINAL_TESTS)
    (slot / study.SESSION_LINK).symlink_to(session, target_is_directory=True)
    if control.exists():
        shutil.rmtree(control)
    control.mkdir(mode=0o777, parents=True)


# --- the checks of decision 3


def init_problems(
    init: Any, credential: study.Credential, config: study.LaunchConfig, agent: Agent
) -> list[str]:
    """The problems with a healer session's init event; empty when the session may be kept: one
    MCP server, ``playwright-test``, connected; every MCP tool of the agent offered and no tool
    beyond the agent's (the built-in tools CLI 2.1.325 does not have are left out, and the
    session records which); ``dontAsk``; no MCP or plugin errors; the credential the CLI reports
    is the one injected; the pinned version and model."""
    if not study._is_init(init):
        return ["the first line is not the init event"]
    problems = []
    servers = init.get("mcp_servers")
    entries = [s for s in servers if isinstance(s, dict)] if isinstance(servers, list) else []
    names = [s.get("name") for s in entries]
    if names != [SERVER_KEY] or len(entries) != len(servers):
        problems.append(f"the MCP servers are {study._show(names)}; only {SERVER_KEY!r} is "
                        f"expected")
    elif entries[0].get("status") != "connected":
        problems.append(f"MCP server {SERVER_KEY!r} is {study._show(entries[0].get('status'))}, "
                        f"not 'connected'")
    tools = init.get("tools")
    if not isinstance(tools, list):
        problems.append("the init lists no tools")
    else:
        missing = [t for t in agent.mcp_tools if t not in tools]
        extra = [t for t in tools if t not in agent.tools]
        if missing:
            problems.append(f"the agent's MCP tools not offered: {', '.join(missing)}")
        if extra:
            problems.append(f"tools offered beyond the agent's: {', '.join(map(str, extra))}")
    if init.get("permissionMode") != "dontAsk":
        problems.append(f"permissionMode is {study._show(init.get('permissionMode'))}, not "
                        f"'dontAsk'")
    if init.get("agents") != [AGENT_NAME]:
        problems.append(f"the agents are {study._show(init.get('agents'))}; only {AGENT_NAME!r} "
                        f"is expected")
    for key in ("mcp_server_errors", "plugin_errors"):
        if init.get(key):
            problems.append(f"{key}: {study._show(init[key])}")
    expected = study.API_KEY_SOURCE[credential.kind]
    if init.get("apiKeySource") != expected:
        problems.append(f"apiKeySource is {study._show(init.get('apiKeySource'))}; the "
                        f"{credential.kind} credential that was injected gives {expected!r}")
    return problems + study._pinned_problems(init, config)


def init_environment_problems(init: Any) -> list[str]:
    """The problems of an init event that the environment makes rather than the setup: no init
    event, the one server not connected (failed, or still pending, so its tools are not listed
    either), or MCP server errors. A bad init with none of these is the setup's: the arm is not
    the healer as Playwright sets it up, so it fails the check of decision 3."""
    if not study._is_init(init):
        return ["the first line is not the init event"]
    problems = []
    servers = init.get("mcp_servers")
    entries = [s for s in servers if isinstance(s, dict)] if isinstance(servers, list) else []
    if [s.get("name") for s in entries] == [SERVER_KEY] and \
            entries[0].get("status") != "connected":
        problems.append(f"MCP server {SERVER_KEY!r} is {study._show(entries[0].get('status'))}")
    if init.get("mcp_server_errors"):
        problems.append(f"mcp_server_errors: {study._show(init['mcp_server_errors'])}")
    return problems


def _init_by_environment(init: Any) -> bool:
    """Whether a bad init is the environment's; one that cannot even be read counts as the
    environment's, as in the study."""
    try:
        return bool(init_environment_problems(init))
    except Exception:  # a malformed init must not crash the harness
        return True


SYNTHETIC_MODEL = "<synthetic>"
"""The model the CLI names on a message it writes itself, such as an API error's."""

SDK_IDENTITY = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
"""The line CLI 2.1.325 puts before an ``--agent``'s prompt, the agent given inline or as a file
(``tests/test_fidelity_cli.py``)."""


def model_problems(events: Iterable[Mapping[str, Any]], model: str) -> list[str]:
    """Every assistant message and every ``modelUsage`` key of the result must be ``model`` (the
    agent file names ``sonnet``; the CLI's ``--model`` pins the full ID)."""
    others: dict[str, int] = {}
    usage: list[str] = []
    for event in events:
        if event.get("type") == "assistant":
            message = event.get("message")
            got = message.get("model") if isinstance(message, Mapping) else None
            if got not in (model, SYNTHETIC_MODEL):
                others[str(got)] = others.get(str(got), 0) + 1
        elif event.get("type") == "result" and isinstance(event.get("modelUsage"), Mapping):
            usage += [str(k) for k in event["modelUsage"] if k != model]
    problems = [f"{n} assistant message(s) from {name}, not {model}"
                for name, n in sorted(others.items())]
    if usage:
        problems.append(f"modelUsage names {', '.join(usage)} besides {model}")
    return problems


def snapshot_problems(cfg_dir: Path, session_id: str) -> tuple[list[str], dict[str, Any]]:
    """Whether the system prompt the CLI sent holds the healer's body. The CLI keeps each
    request's shape in its transcript (``cfg/projects/<cwd>/<session id>.jsonl``, type
    ``api-request-shape``, ``--system-prompt-snapshot on``). Returns the problems and what was
    seen, which the report states: how many shapes, how many hold the body, whether Claude
    Code's own system prompt comes first (as for H), and how many are exactly the SDK's identity
    line and the body, as CLI 2.1.325 sends an ``--agent``'s prompt (text beside the body still
    holds it, so only this count shows it)."""
    seen: dict[str, Any] = {"shapes": 0, "with_body": 0, "claude_code_prompt": None,
                            "identity_and_body": 0}
    paths = sorted(Path(cfg_dir).glob(f"projects/*/{session_id}.jsonl"))
    if not paths:
        return [f"no transcript of the session under {Path(cfg_dir) / 'projects'}"], seen
    _, body = vendored_agent()
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            entry = study._loads(line)
            shape = entry.get("shape") if entry and entry.get("type") == "api-request-shape" \
                else None
            system = shape.get("system") if isinstance(shape, Mapping) else None
            if not isinstance(system, list):
                continue
            texts = [b.get("text") for b in system
                     if isinstance(b, Mapping) and isinstance(b.get("text"), str)]
            seen["shapes"] += 1
            seen["with_body"] += any(body in t for t in texts)
            seen["identity_and_body"] += texts == [SDK_IDENTITY, body]
            if seen["claude_code_prompt"] is None:
                seen["claude_code_prompt"] = bool(texts) and texts[0].startswith(
                    "You are Claude Code")
    if not seen["shapes"]:
        return ["the transcript holds no request shape (is --system-prompt-snapshot on?)"], seen
    if not seen["with_body"]:
        return ["no request's system prompt holds the healer's body"], seen
    return [], seen


# --- decision 9: environment failures


def environment_records(control_dir: Path) -> list[dict[str, Any]]:
    """The fixture's records (``records/<id>.json`` in the control directory) that show an
    environment failure: a failed reset, connection or wait. A record that cannot be read
    counts as one."""
    found = []
    for path in sorted((Path(control_dir) / "records").glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            found.append({"id": path.stem, "test": None, "file": None,
                          "env_error": f"unreadable record: {type(exc).__name__}"})
            continue
        if not isinstance(record, Mapping):
            found.append({"id": path.stem, "test": None, "file": None,
                          "env_error": "unreadable record: not an object"})
        elif record.get("env_error"):
            found.append({k: record.get(k) for k in ("id", "test", "file", "env_error")})
    return found


def _mcp_uses(events: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [block for event in events if event.get("type") == "assistant"
            for block in study._blocks(event)
            if block.get("type") == "tool_use" and str(block.get("name", "")).startswith(
                MCP_PREFIX)]


def server_problems(events: Sequence[Mapping[str, Any]]) -> list[str]:
    """MCP calls of ``playwright-test`` that failed because the server did: a result the CLI
    marked as an error with the study's marks of a lost server (``MCP error``, ``not
    connected``, ``disconnected``). A test that fails is not one: its result is the test's."""
    names = {b.get("id"): b.get("name") for b in _mcp_uses(events)}
    problems = []
    for event in events:
        if event.get("type") != "user":
            continue
        for block in study._blocks(event):
            if (block.get("type") == "tool_result" and block.get("tool_use_id") in names
                    and block.get("is_error") is True):
                text = study._tool_result_text(block)
                if study._TOOL_FAILURE.search(text):
                    problems.append(f"{names[block['tool_use_id']]} failed in the server: "
                                    f"{study._clip(text)}")
    return problems


def mcp_calls(events: Sequence[Mapping[str, Any]]) -> tuple[dict[str, int], dict | None]:
    """How many times the agent called each MCP tool, and its last call (the tool and input):
    what was running when a session failed."""
    counts: dict[str, int] = {}
    uses = _mcp_uses(events)
    for block in uses:
        counts[block["name"]] = counts.get(block["name"], 0) + 1
    last = {"tool": uses[-1]["name"], "input": uses[-1].get("input")} if uses else None
    return counts, last


_STAND_IN_INIT = {"type": "system", "subtype": "init", "permissionMode": "dontAsk",
                  "mcp_servers": [{"name": study.SERVER_KEY, "status": "connected"}],
                  "tools": [study.RUN_TEST_TOOL]}
"""What ``hoh.session.classify_stream``'s first row asks of H's init. A healer session's init
is checked by ``init_problems`` instead, so the stream is classified with this in its place,
and the other rows (no result line, limits, the credential, capacity, a model switch, the caps,
a normal end) apply unchanged: none of them reads the init."""


def _stderr_line(path: Path, limit: int = 300) -> str | None:
    """The first line of text in the CLI's ``stderr.log`` (read after the credential has been
    redacted from the slot), shortened to ``limit`` characters; None when there is none."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), None)
    return None if line is None else line[:limit]


def classify(lines: Iterable[str | bytes], exit_code: int | None) -> tuple[str, Outcome]:
    """The study's class of a finished session (``hoh.session.classify_stream``), for a
    healer's stream: its init is ``init_problems``'s, the rest is the study's."""
    lines = list(lines)
    first = next((i for i, raw in enumerate(lines) if raw.strip()), None)
    if first is None or not study._is_init(study._loads(lines[first])):
        return study._verdict("environment", "the first line is not the init event")
    lines[first] = json.dumps(_STAND_IN_INIT)
    return study.classify_stream(lines, exit_code, None, now=time.time())


# --- a session


class Watcher(Protocol):
    """The slot's reset watcher (``hoh.fidelity.slot.ResetWatcher``), as a session uses it."""

    error: str | None
    """What ended the watcher, when anything but ``stop`` did."""

    def start(self) -> Any: ...

    def stop(self, timeout: float | None = None) -> bool:
        """Stop, after finishing a reset under way; True once the watcher has ended."""
        ...

    @property
    def resets(self) -> list[dict[str, Any]]:
        """Every reset: its request's id, when it began, its seconds, ``ok`` and ``error``."""
        ...

    @property
    def failures(self) -> list[dict[str, Any]]:
        """The resets that did not give a fresh stack."""
        ...


WATCHER_STOP_S = float(slot_module.RESET_TIMEOUT_S)
"""How long the session waits for the watcher to end after the CLI has: a reset under way is
finished and answered first, and the fixture waits as long for its answer."""


CHECK_FAILED = "check_failed"
"""A session that failed a check of decision 3: it is not labeled, and the runs stop."""


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    session_id: str
    end_class: str
    """One of ``hoh.session.CLASSES``, or ``CHECK_FAILED``."""
    stream_path: Path
    exit_code: int | None
    wall_s: float
    init: dict | None
    result: dict | None
    check_problems: list[str]
    mcp_calls: dict[str, int]


def run_session(
    slot_dir: Path,
    session_dir: Path,
    credential: study.Credential,
    config: study.LaunchConfig,
    *,
    control_dir: Path,
    watcher: Watcher | None = None,
    session_id: str | None = None,
    stop_grace_s: float = study.STOP_GRACE_S,
    abort_on_bad_init: bool = True,
) -> Result:
    """Launch the healer once in a slot that ``prepare_slot`` made, and classify how it ended.

    As ``hoh.session.run_session``: the CLI in its own process group, the user message on
    standard input, its output copied line by line to ``out/stream.jsonl``, the init checked as
    it arrives (with ``abort_on_bad_init``, a session whose init fails is stopped at once; the
    spike may let one run on for a look, and it is still not labeled), the agent's own time and
    the wall time capped, then SIGINT, SIGTERM and SIGKILL, and whatever the CLI leaves in its
    group ended. The watcher runs while the CLI does. Then the session's class: the study's, then
    decision 9 (an environment record, a failed reset, a lost server: ``environment``, for a
    session that would be kept), then the checks of decision 3 on a session that would be
    labeled (``CHECK_FAILED``), then a credential found on disk (``credential``). ``launch.json``
    and ``end.json`` in ``out/`` record the launch, with the credential redacted, and the ending.
    """
    if not isinstance(credential, study.Credential):
        raise TypeError("credential must be a hoh.session.Credential")
    if not isinstance(config, study.LaunchConfig):
        raise TypeError("config must be a hoh.session.LaunchConfig")
    slot = Path(slot_dir).resolve()
    session = Path(session_dir).resolve()
    control = Path(control_dir).resolve()
    ws, h, out, cfg = slot / "ws", slot / "h", slot / "out", slot / "cfg"
    agent_path = h / "agents" / f"{AGENT_NAME}.md"
    needed = [slot / d for d in study.SLOT_DIRS] + [
        h / "settings.json", h / "mcp.json", h / "prompt.md", agent_path,
        session / study.ORIGINAL_TESTS, control,
    ]
    for path in needed:
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing; run prepare_slot first")
    link = slot / study.SESSION_LINK
    if not link.is_symlink() or link.resolve() != session:
        raise ValueError(f"the slot {slot} was not prepared for the session directory {session}")
    if any(out.iterdir()) or (session / STATE_FILE).exists():
        raise FileExistsError(f"the slot {slot} was used after it was prepared; prepare it again")
    if any(control.iterdir()):
        raise FileExistsError(f"the control directory {control} is not empty; prepare the slot "
                              f"again")
    agent_bytes = agent_path.read_bytes()
    agent = read_agent(agent_bytes.decode("utf-8"))
    problems = agent_problems(agent)
    if problems:
        raise ValueError(f"{agent_path}: " + "; ".join(problems))
    binary = study._binary_record(config.claude_bin)
    session_id = str(uuid.uuid4()) if session_id is None else session_id
    argv, env = build_command(slot, credential, config, session_id, agent)
    stream_path = out / "stream.jsonl"
    before = study._tree_digest(ws)
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
            "agent": {"path": str(agent_path), "sha256": study._sha256_file(agent_path)},
            "control_dir": str(control),
            "sha256": {name: study._sha256_file(h / name)
                       for name in ("settings.json", "mcp.json", "prompt.md")},
        },
    )

    def check(init: dict | None) -> list[str]:
        if init is None:
            return ["the first line is not the init event"]
        try:
            return init_problems(init, credential, config, agent)
        except Exception as exc:  # a malformed init must not crash the harness
            return [f"the init check failed: {type(exc).__name__}"]

    watch = study._Watch()
    state_file = session / STATE_FILE
    log.info("session %s: launching the healer in %s with the %s credential", session_id, slot,
             credential.kind)
    if watcher is not None:
        watcher.start()
    try:
        t0 = time.monotonic()
        with open(h / "prompt.md", "rb") as stdin, open(out / "stderr.log", "wb") as stderr:
            proc = subprocess.Popen(
                argv, env=env, cwd=ws, stdin=stdin, stdout=subprocess.PIPE, stderr=stderr,
                start_new_session=True,
            )
        with study._LIVE_LOCK:
            study._LIVE[proc.pid] = session_id
        try:
            assert proc.stdout is not None
            reader = threading.Thread(
                target=study._pump,
                args=(proc.stdout, stream_path, watch, check, abort_on_bad_init),
                name=f"fidelity-stream-{session_id[:8]}", daemon=True,
            )
            reader.start()
            stop_reason = study._wait(proc, watch, state_file, t0, config)
            stop_at = time.monotonic() if stop_reason else None
            sent = study._stop(proc, stop_grace_s, session_id, stop_reason) if stop_reason \
                else []
            proc.wait()
            wall_s = time.monotonic() - t0
            tool_s = study._tool_seconds(state_file, watch)
            agent_s = wall_s - tool_s
            reader.join(timeout=stop_grace_s)
            leftovers = study._end_group(proc.pid, stop_grace_s)
        finally:
            with study._LIVE_LOCK:
                study._LIVE.pop(proc.pid, None)
    finally:
        watcher_ended = watcher.stop(timeout=WATCHER_STOP_S) if watcher is not None else True
    reader.join(timeout=stop_grace_s)
    if reader.is_alive():
        log.warning("session %s: its output is still open; classifying what arrived", session_id)
    else:
        proc.stdout.close()

    # The search comes first, so that nothing read from the stream below can carry the value.
    leaks = study._redact_everywhere(credential.value, {"slot": slot, "session": session})

    raw = stream_path.read_bytes().splitlines() if stream_path.exists() else []
    events = study._parse(raw)
    init = events[0] if events and study._is_init(events[0]) else None
    final = events[-1] if events and events[-1].get("type") == "result" else None
    result_before_stop = (
        stop_at is not None and watch.result_at is not None and watch.result_at < stop_at
    )
    exit_for_table = None if result_before_stop else proc.returncode
    cls, outcome = classify(raw, exit_for_table)
    init_found = check(init)
    edited = study._tree_digest(ws) != before

    # As hoh.session.run_session: a bad init, then the two caps. A bad init is the environment's
    # only when the server did not come up; otherwise it fails decision 3's check.
    if init_found and _init_by_environment(init):
        # a CLI that wrote no stream at all may have said why on stderr
        said = None if any(line.strip() for line in raw) else _stderr_line(out / "stderr.log")
        cls, outcome = study._verdict("environment", "init: " + "; ".join(init_found) + (
            f"; the CLI wrote on stderr: {said}" if said else ""))
    elif init_found:
        cls, outcome = CHECK_FAILED, Outcome("error", f"{CHECK_FAILED}: init: "
                                                      + "; ".join(init_found))
    elif stop_reason == "wall_clock" and not result_before_stop and cls not in study._EXTERNAL:
        limit = (
            f"stopped at the wall clock: {agent_s:.1f} s of the agent's own time (cap "
            f"{config.wall_clock_s} s), besides {tool_s:.1f} s inside MCP calls"
        )
        working = study._working_at_the_stop(events)
        if final is not None:
            cls, outcome = study._verdict("censored", f"{limit}; a result line followed the stop")
        elif edited:
            cls, outcome = study._verdict("censored", f"{limit}; the workspace was edited")
        elif working is None:
            cls, outcome = study._verdict("censored", f"{limit}; the agent was still working, "
                                                      f"with no edits")
        else:
            cls, outcome = study._verdict("environment", f"{limit}; no result line and no "
                                                         f"edits, {working}")
    elif stop_reason == "hard_wall_clock" and not result_before_stop and \
            cls not in study._EXTERNAL:
        cls, outcome = study._verdict(
            "environment",
            f"stopped at the hard wall clock ({config.hard_wall_clock_s} s) with "
            f"{tool_s:.1f} s inside MCP calls and {agent_s:.1f} s of the agent's own time",
        )

    # Decision 9: what the environment did to the session. Records and resets count as the
    # study's failure markers do (hoh.worker._discard): they make a session that would be kept
    # an environment failure, and leave every other class as it is; a lost server counts as
    # its run_test problems do.
    records = environment_records(control)
    resets = watcher.failures if watcher is not None else []
    stopped = None
    if not watcher_ended:
        stopped = f"the reset watcher had not ended {WATCHER_STOP_S:g} s after the CLI"
    elif watcher is not None and watcher.error:
        stopped = f"the reset watcher ended: {watcher.error}"
    server = server_problems(events) if cls in ("completed", "censored") else []
    reasons = (
        [f"record {r['id']}: {r['env_error']}" for r in records]
        + [f"reset {r['id']}: {r['error']}" for r in resets]
        + ([stopped] if stopped else [])
        + server
    )
    if reasons and cls in ("completed", "censored"):
        cls, outcome = study._verdict("environment", "; ".join(reasons))

    # Decision 3: the checks on a session that would be labeled.
    model_found = model_problems(events, config.model)
    snapshot_found, snapshot = snapshot_problems(cfg, session_id)
    check_problems = model_found + snapshot_found
    if cls in ("completed", "censored") and check_problems:
        cls, outcome = CHECK_FAILED, Outcome("error", f"{CHECK_FAILED}: "
                                                      + "; ".join(check_problems))
    if leaks:
        cls, outcome = study._verdict(
            "credential", "the credential value was found, and redacted, in " + ", ".join(leaks)
        )
        log.error("session %s: the credential value was found on disk: %s", session_id,
                  ", ".join(leaks))

    counts, last = mcp_calls(events)
    offered = init.get("tools") if init is not None else None
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
            "init_problems": init_found,
            "check_problems": check_problems,
            "snapshot": snapshot,
            "tools_offered": offered,
            "tools_not_offered": [t for t in agent.tools if t not in offered]
            if isinstance(offered, list) else None,
            "agents": init.get("agents") if init is not None else None,
            "environment": {"records": records, "resets": resets, "watcher": stopped,
                            "server": server},
            "resets": watcher.resets if watcher is not None else [],
            "tests_as_given": study._tree_digest(ws / "tests")
            == study._tree_digest(session / study.ORIGINAL_TESTS),
            "last_mcp_call": last,
            "mcp_calls": counts,
            "result_line": final is not None,
            "result_before_stop": result_before_stop,
            "edited": edited,
            "api_retries": sum(1 for e in events if e.get("subtype") == "api_retry"),
            "compact_boundaries": sum(1 for e in events if e.get("subtype") == "compact_boundary"),
            "rate_limit_events": sum(1 for e in events if e.get("type") == "rate_limit_event"),
            "leftover_processes_ended": leftovers,
            "credential_found_in": leaks,
        },
    )
    log.info("session %s: %s (exit code %s, %.1f s)", session_id, outcome.detail,
             proc.returncode, wall_s)
    return Result(
        outcome=outcome,
        session_id=session_id,
        end_class=cls,
        stream_path=stream_path,
        exit_code=proc.returncode,
        wall_s=wall_s,
        init=init,
        result=final,
        check_problems=check_problems,
        mcp_calls=counts,
    )
