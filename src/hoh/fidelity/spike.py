"""The fidelity spike's steps a and b (the fidelity plan's step 5) and port validation's run
check (step 6), by hand, with no model.

``python -m hoh.fidelity.spike --config <yaml> --slot 7 --app bookstack --task comment
--variant A+M --out build/fidelity/spike/ab``

In slot N's stack of the variant (``hoh.fidelity.runs.SlotStack``: the slot's lock, the slot's
network), with the reset watcher running, it starts the healer's MCP server with the line a
session gives the CLI (``hoh.fidelity.slot.server_command``) and speaks MCP to it over standard
input and output (``McpClient``):

1. ``initialize`` and ``tools/list``: the healer's nine tools are among those the server offers
   (it lists every tool it has, the planner's and the generator's too; what a session's init
   offers is checked in the session);
2. ``test_list``: the port's one test and its id;
3. ``test_run``: the fixture asks for a reset, the watcher makes the stack, the test runs;
4. ``test_debug`` of that test: the fixture asks for a second reset, and the test pauses (a red
   port pauses on its failure), with no error;
5. ``browser_snapshot`` and ``browser_evaluate`` (``() => location.href``, with the ``intent``
   the server asks of its action tools) reach the paused tab, which shows the app;
6. ``test_run`` again, which ends the paused test and asks for a third reset.

Step a holds when every reset worked (one for each of the three runs), the app's containers after
the last run are not the ones after the first (the stack was made again from nothing), and the
server's container is the same from start to end (it kept its network). Step b holds when steps
1 to 6 answered as said and the control directory holds no environment record. ``spike.json`` in
``--out`` records every call and answer, the resets, the containers and the records. The command
prints each check and exits 0 when all of them passed and 1 when one failed. It exits 4 when the
environment failed, so that the checks could not say anything: Docker or the stack failed, or the
server did not start, ended, or let a call run out of time (``spike.json`` holds the error, and
such a call is marked ``environment``). An error answer of the server is a check's, not the
environment's. It exits 2 when it did not start (a file it needs is missing, among other things)
and 3 when the guard stopped it, before the first run or at a reset of the slot (the overlap
``runs.SlotStack`` keeps). Exits 2, 3 and 4 decide nothing. A fault of its own code is not a
check's: it prints its traceback and a ``stopped:`` line, and exits 1.

With ``--steps run`` it is port validation's run check (the fidelity plan's step 6: each drawn
port once through the healer's own ``test_run``): steps 1 to 3 only, and its checks
(``run_checks``) are the tools, the one test listed, the ``test_run`` answered and red (``red``:
a ``1 failed`` summary line and no ``passed`` or ``flaky`` one; the races judge the line), every
reset worked (one or more), the server's container kept, no environment record and no error.
A failed reset or an environment record is the environment's here, as in a session (decision 9):
the run check decides nothing (``decided``) and exits 4. ``spike.json`` records the steps it ran,
and with an error whether it was the environment's (``error_environment``);
``hoh.fidelity.verdict`` reads the run checks' records, and a run check that ended in a fault of
this code stops port validation. A run that is not red is the port's
only when its answer is a summary of one test that ran and did not fail
(``not_red_by_the_port``).
The exit codes are the same.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hoh import docker_backend, envs
from hoh.backend import EnvFailure
from hoh.fidelity import guard as guard_module
from hoh.fidelity import race, runs
from hoh.fidelity import session as fid
from hoh.fidelity import slot as slot_module
from hoh.fidelity import stack as stack_module
from hoh.queue import atomic_write_json

PROTOCOL = "2025-06-18"
CALL_TIMEOUT_S = 60.0
RUN_TIMEOUT_S = float(slot_module.RESET_TIMEOUT_S + 600)
"""A test run waits for a reset (the fixture's timeout) and then runs a test (300 s), with 300 s
more for Playwright Test to start its worker and write its report."""
INTENT = "Read the URL of the paused test's page"
"""The ``intent`` Playwright Test's server asks of its action tools, ``browser_evaluate`` among
them (an argument it logs; it does not change the call)."""
_LISTED = re.compile(r"^\s*\[id=([^\]\s]+)\]\s.*›\s(.+?)\s*$")
_URL = re.compile(r"https?://[^\s\"'<>]+")
_DEFAULT_PORTS = {"http": 80, "https": 443}
STEPS = ("all", "run")
"""All the steps, or the run check of port validation (module docstring)."""
_ONE_FAILED = re.compile(r"^\s*1 failed\s*$", re.MULTILINE)
_PASSED_OR_FLAKY = re.compile(r"^\s*\d+ (?:passed|flaky)\b", re.MULTILINE)
_SUMMARY = re.compile(r"^\s*(\d+) (passed|failed|flaky|skipped|interrupted|did not run)\b",
                      re.MULTILINE)


class McpError(RuntimeError):
    """An MCP error answer, or a server that ended (``ServerEnded``)."""


class ServerEnded(McpError):
    """The server ended: its output closed, or it no longer reads what it is sent."""


ENVIRONMENT = (EnvFailure, McpError, TimeoutError, OSError, subprocess.SubprocessError)
"""What the environment raises before the checks can say anything: Docker or the stack failed,
or the server did not start or answer. A tool call's error is recorded, not raised: the
environment's when the server ended or the time ran out, else the check's."""


class McpClient:
    """A client of an MCP server over standard input and output: one JSON-RPC message a line.
    Lines that are not the answer awaited (notifications, anything else) are passed over."""

    def __init__(self, argv: Sequence[str], *, env: Mapping[str, str], stderr: Path,
                 timeout_s: float = CALL_TIMEOUT_S) -> None:
        self.timeout_s = float(timeout_s)
        self._stderr = open(stderr, "wb")  # noqa: SIM115 (closed in close)
        self.proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._stderr, env=dict(env), start_new_session=True)
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True, name="mcp-client")
        self._reader.start()
        self._next = 0

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _ended(self, method: str) -> ServerEnded:
        return ServerEnded(f"{method}: the server ended (exit code {self.proc.poll()})")

    def _send(self, method: str, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise self._ended(method) from None

    def request(self, method: str, params: Mapping[str, Any] | None = None, *,
                timeout_s: float | None = None) -> Any:
        self._next += 1
        rid = self._next
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            message["params"] = dict(params)
        self._send(method, message)
        wait = self.timeout_s if timeout_s is None else float(timeout_s)
        deadline = time.monotonic() + wait
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{method}: no answer in {wait:g} s")
            try:
                line = self._lines.get(timeout=left)
            except queue.Empty:
                raise TimeoutError(f"{method}: no answer in {wait:g} s") from None
            if line is None:
                self._lines.put(None)  # for the next request too
                raise self._ended(method)
            try:
                answer = json.loads(line)
            except ValueError:
                continue
            if not isinstance(answer, dict) or answer.get("id") != rid or "method" in answer:
                continue
            if "error" in answer:
                raise McpError(f"{method}: {answer['error']}")
            return answer.get("result")

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        self._send(method, message)

    def initialize(self) -> dict[str, Any]:
        answer = self.request("initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "hoh-fidelity-spike", "version": "1"}})
        self.notify("notifications/initialized")
        return answer

    def tools(self) -> list[str]:
        return [tool["name"] for tool in self.request("tools/list")["tools"]]

    def call(self, name: str, arguments: Mapping[str, Any] | None = None, *,
             timeout_s: float | None = None) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": dict(arguments or {})},
                            timeout_s=timeout_s)

    def close(self, grace_s: float = 10.0) -> None:
        with contextlib.suppress(OSError):
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        try:
            self.proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._reader.join(timeout=grace_s)
        self._stderr.close()


def text_of(answer: Mapping[str, Any]) -> str:
    return "\n".join(part.get("text", "") for part in answer.get("content") or []
                     if isinstance(part, Mapping) and part.get("type") == "text")


def test_ids(listed: str) -> list[tuple[str, str]]:
    """The tests ``test_list`` printed: their ids and their own titles."""
    return [(m.group(1), m.group(2).rpartition("› ")[2] or m.group(2))
            for m in map(_LISTED.match, listed.splitlines()) if m]


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return parts.scheme, parts.hostname, None
    return parts.scheme, parts.hostname, port or _DEFAULT_PORTS.get(parts.scheme)


def same_origin(start_url: str, seen: str) -> bool:
    """Whether a URL in ``seen`` (a tool's answer) has the scheme, host and port of
    ``start_url``, its default port written or not: a browser drops a default port."""
    wanted = _origin(start_url)
    return wanted[1] is not None and any(_origin(url) == wanted for url in _URL.findall(seen))


def red(text: str) -> bool:
    """A ``test_run`` answer of one red test: Playwright's summary has a ``1 failed`` line and
    no ``passed`` or ``flaky`` one."""
    return bool(_ONE_FAILED.search(text)) and not _PASSED_OR_FLAKY.search(text)


def summary(text: str) -> dict[str, int]:
    """Playwright's summary lines in a ``test_run`` answer: each outcome and its count."""
    counts: dict[str, int] = {}
    for number, outcome in _SUMMARY.findall(text):
        counts[outcome] = counts.get(outcome, 0) + int(number)
    return counts


def not_red_by_the_port(record: Mapping[str, Any]) -> bool:
    """Whether a run check's one ``test_run`` answered, with no error, a summary of one test
    that ran and did not fail (``1 passed`` or ``1 flaky``, nothing else): a port that is not
    red. Any other answer that is not red (no summary, another count or outcome, an error)
    says nothing of the port, and is the harness's (``hoh.fidelity.verdict``)."""
    runs_seen = [e for e in record.get("calls") or [] if e.get("tool") == "test_run"]
    if len(runs_seen) != 1 or runs_seen[0].get("is_error") or runs_seen[0].get("error"):
        return False
    return summary(runs_seen[0].get("text") or "") in ({"passed": 1}, {"flaky": 1})


def expected_tools() -> list[str]:
    """The healer's MCP tools, as the vendored agent names them."""
    return [tool for tool in fid.vendored_agent()[0] if tool.startswith(fid.MCP_PREFIX)]


# --- the run


class Containers:
    """Where the containers are: the stack's (``docker compose ps``) and the server's."""

    def __init__(self, run: slot_module.Run = subprocess.run) -> None:
        self._run = run

    def app(self, stack_dir: Path) -> list[str]:
        done = envs.compose(stack_dir, "ps", "--all", "--quiet", timeout_s=60)
        if done.returncode != 0:
            raise EnvFailure(f"docker compose ps failed: {(done.stderr or '').strip()}")
        return sorted(done.stdout.split())

    def server(self, name: str) -> str | None:
        done = self._run(["docker", "inspect", "--format", "{{.Id}}", name], capture_output=True,
                         text=True, timeout=60, check=False)
        return (done.stdout.strip() or None) if done.returncode == 0 else None


def run_spike(
    *,
    app: str,
    task: str,
    code: str,
    slot: int,
    config: runs.RunsConfig,
    out: Path,
    stack: runs.SlotStack,
    make_watcher: Callable[..., Any] = slot_module.ResetWatcher,
    containers: Containers | None = None,
    server_command: Callable[..., list[str]] = slot_module.server_command,
    provenance: Callable[..., dict[str, Any]] = race.provenance,
    steps: str = "all",
) -> dict[str, Any]:
    """Steps 1 to 6 of the module docstring, or 1 to 3 for ``steps="run"``; returns the record,
    also written to ``spike.json``."""
    if steps not in STEPS:
        raise ValueError(f"steps {steps!r}: one of {', '.join(STEPS)}")
    out = Path(out)
    out.mkdir(parents=True)
    containers = containers or Containers()
    variant = race.parse_variant(app, task, code)
    test_file = Path(config.tests_root) / app / f"{task}.spec.ts"
    workspace = runs.slot_dir(config, slot) / "ws"
    control = runs.control_dir(config, slot)
    record: dict[str, Any] = {
        "variant": {"app": app, "task": task, "code": code}, "slot": slot, "steps": steps,
        "started_at": time.time(), "start_url": None, "tools": None, "test_ids": [],
        "calls": [], "resets": [], "containers": {}, "environment_records": [], "error": None,
        "error_environment": None, "stops": {}, "provenance": None,
    }
    try:
        record["provenance"] = provenance(
            test_file, variant, ports=config.ports,
            rules=config.rules if variant.rule else None, image_prefix=config.image_prefix)
        with stack.lock():
            stack_dir = stack.assemble(variant)
            try:
                leftover = stack.remove_server()
                if leftover is not None:
                    raise EnvFailure(f"the server container {stack.container}: {leftover}")
                if workspace.exists():
                    shutil.rmtree(workspace)
                (workspace / "tests").mkdir(parents=True)
                shutil.copyfile(test_file, workspace / "tests" / test_file.name)
                if control.exists():
                    shutil.rmtree(control)
                control.mkdir(mode=0o777, parents=True)
                environment = slot_module.server_environment(stack_dir)
                record["start_url"] = environment["HOH_START_URL"]
                argv = server_command(network=stack.network, workspace=workspace,
                                      control_dir=control, environment=environment,
                                      image_prefix=config.image_prefix, name=stack.container)
                _talk(record, argv, stack_dir=stack_dir, control=control, out=out, stack=stack,
                      config=config, make_watcher=make_watcher, containers=containers,
                      steps=steps)
            finally:
                record["stops"] = {"server": stack.remove_server(),
                                   "stack": stack.stop(stack_dir)}
    except BaseException as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["error_environment"] = isinstance(exc, ENVIRONMENT)
        raise
    finally:
        record["ended_at"] = time.time()
        atomic_write_json(out / "spike.json", record)
    return record


def _talk(record: dict[str, Any], argv: list[str], *, stack_dir: Path, control: Path, out: Path,
          stack: runs.SlotStack, config: runs.RunsConfig, make_watcher: Callable[..., Any],
          containers: Containers, steps: str = "all") -> None:
    watcher = make_watcher(stack_dir, control, recreate=stack.recreate, log=out / "resets.json")
    watcher.start()
    client = McpClient(argv, env=dict(config.launch.server_env), stderr=out / "server.log")

    def call(name: str, arguments: Mapping[str, Any] | None = None,
             timeout_s: float = CALL_TIMEOUT_S) -> dict[str, Any]:
        began = time.monotonic()
        entry: dict[str, Any] = {"tool": name, "arguments": dict(arguments or {}), "error": None,
                                 "environment": False, "is_error": None, "text": None}
        try:
            answer = client.call(name, arguments, timeout_s=timeout_s)
            entry.update(is_error=bool(answer.get("isError")), text=text_of(answer))
        except (McpError, TimeoutError) as exc:
            answer = {}
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["environment"] = isinstance(exc, (ServerEnded, TimeoutError))
        entry["seconds"] = round(time.monotonic() - began, 3)
        record["calls"].append(entry)
        return answer

    try:
        record["initialize"] = client.initialize()
        record["tools"] = client.tools()
        record["test_ids"] = [list(found) for found in test_ids(text_of(call("test_list")))]
        record["containers"]["server_start"] = containers.server(stack.container)
        call("test_run", {}, timeout_s=RUN_TIMEOUT_S)
        if steps == "all":
            record["containers"]["after_first_run"] = containers.app(stack_dir)
            if record["test_ids"]:
                rid, title = record["test_ids"][0]
                call("test_debug", {"test": {"id": rid, "title": title}},
                     timeout_s=RUN_TIMEOUT_S)
            call("browser_snapshot", {})
            call("browser_evaluate", {"function": "() => location.href", "intent": INTENT})
            call("test_run", {}, timeout_s=RUN_TIMEOUT_S)
            record["containers"]["after_second_run"] = containers.app(stack_dir)
        record["containers"]["server_end"] = containers.server(stack.container)
    finally:
        client.close()
        watcher.stop(timeout=slot_module.RESET_TIMEOUT_S)
        record["resets"] = watcher.resets
        record["environment_records"] = fid.environment_records(control)


def _answered(record: Mapping[str, Any], tool: str, index: int = 0, *,
              error_ok: bool = True) -> tuple[bool, str]:
    """Whether the ``index``-th call of ``tool`` was answered (and, unless ``error_ok``, not with
    an error answer), and what it said."""
    by_tool: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for entry in record.get("calls") or []:
        by_tool[entry["tool"]].append(entry)
    found = by_tool.get(tool, [])
    if len(found) <= index:
        return False, "not called"
    entry = found[index]
    if entry.get("error"):
        return False, entry["error"]
    if not error_ok and entry.get("is_error"):
        return False, f"an error answer: {(entry.get('text') or '')[:300]}"
    return True, (entry.get("text") or "")[:300]


def _shared(record: Mapping[str, Any]) -> dict[str, tuple[str, bool, str]]:
    """The checks both kinds of run make, by name."""
    wanted = {t.removeprefix(fid.MCP_PREFIX) for t in expected_tools()}
    offered = set(record.get("tools") or [])
    missing = sorted(wanted - offered)
    boxes = record.get("containers") or {}
    found = [
        ("tools", bool(wanted) and not missing,
         f"{len(offered)} offered; of the healer's {len(wanted)}, missing: {missing or 'none'}"),
        ("test_list", len(record.get("test_ids") or []) == 1, str(record.get("test_ids"))),
        ("test_run", *_answered(record, "test_run")),
        ("server container", bool(boxes.get("server_start"))
         and boxes.get("server_start") == boxes.get("server_end"),
         f"{boxes.get('server_start')} then {boxes.get('server_end')}"),
        ("environment records", not record.get("environment_records"),
         str(record.get("environment_records"))),
        ("no error", record.get("error") is None, str(record.get("error"))),
    ]
    return {check[0]: check for check in found}


def decided(record: Mapping[str, Any]) -> bool:
    """Whether a run check's record decides anything: it ended with no error and no call the
    environment cut, every reset it made worked, and the control directory holds no environment
    record. A failed reset (an overlap the guard found among them) and an environment record are
    the environment's, as in a session (decision 9): the check runs again. A record with no reset
    at all decides, and fails its ``resets`` check: the watcher's, not the environment's."""
    return (record.get("error") is None
            and not any(c.get("environment") for c in record.get("calls") or [])
            and all(r.get("ok") for r in record.get("resets") or [])
            and not record.get("environment_records"))


def run_checks(record: Mapping[str, Any]) -> list[tuple[str, bool, str]]:
    """The run check's conditions (``--steps run``, module docstring): its name, whether it
    held, and what was seen."""
    shared = _shared(record)
    runs_seen = [e for e in record.get("calls") or [] if e.get("tool") == "test_run"]
    text = (runs_seen[0].get("text") or "") if runs_seen else ""
    summary = [line.strip() for line in text.splitlines()
               if _ONE_FAILED.match(line) or _PASSED_OR_FLAKY.match(line)
               or line.lstrip().startswith("at ")]
    resets = record.get("resets") or []
    return [
        shared["tools"], shared["test_list"], shared["test_run"],
        ("red", len(runs_seen) == 1 and red(text), "; ".join(summary) or text[-300:]),
        ("resets", bool(resets) and all(r.get("ok") for r in resets), str(resets)),
        shared["server container"], shared["environment records"], shared["no error"],
    ]


def checks(record: Mapping[str, Any], *, start_url: str) -> list[tuple[str, bool, str]]:
    """Each condition of the module docstring: its name, whether it held, and what was seen."""
    shared = _shared(record)
    evaluate_ok, evaluate_seen = _answered(record, "browser_evaluate", error_ok=False)
    snapshot_ok, snapshot_seen = _answered(record, "browser_snapshot", error_ok=False)
    resets = record.get("resets") or []
    boxes = record.get("containers") or {}
    first, second = boxes.get("after_first_run"), boxes.get("after_second_run")
    return [
        shared["tools"], shared["test_list"], shared["test_run"],
        ("test_debug", *_answered(record, "test_debug", error_ok=False)),
        ("browser_snapshot", snapshot_ok and bool(snapshot_seen.strip()), snapshot_seen),
        ("browser_evaluate", evaluate_ok and same_origin(start_url, evaluate_seen),
         evaluate_seen),
        ("second test_run", *_answered(record, "test_run", 1)),
        ("resets", len(resets) >= 3 and all(r.get("ok") for r in resets), str(resets)),
        ("fresh stack", bool(first) and bool(second) and not set(first) & set(second),
         f"{first} then {second}"),
        shared["server container"], shared["environment records"], shared["no error"],
    ]


def _overlapped(overlap: Exception, out: Path) -> int:
    """The guard found a study stack up when the watcher reset the slot (runbook section 3)."""
    print(f"stopped: {overlap}; nothing was decided (the record: {out / 'spike.json'})",
          file=sys.stderr)
    return 3


def _stopped(exc: BaseException) -> int:
    """A fault of this code, not a check's or the environment's: its traceback and reason."""
    traceback.print_exc()
    print(f"stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hoh.fidelity.spike", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--slot", required=True, type=int, choices=slot_module.SLOTS)
    parser.add_argument("--app", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--variant", required=True, choices=sorted(runs.FAMILIES))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--steps", choices=STEPS, default="all",
                        help="all the steps, or port validation's run check")
    args = parser.parse_args(argv)
    try:
        config = runs.load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"not started: {exc}", file=sys.stderr)
        return 2
    problems = [f"{args.out} exists"] if args.out.exists() else []
    port = Path(config.tests_root) / args.app / f"{args.task}.spec.ts"
    missing = [str(path) for path in (port, config.ports, config.rules) if not Path(path).is_file()]
    if missing:
        problems.append(f"missing: {', '.join(missing)}")
    changed = race.uncommitted()
    if changed:
        problems.append(f"changes the commit does not hold: {', '.join(changed)}")
    problems += [p for p in (docker_backend.runner_image_problem(config.image_prefix),
                             stack_module.image_problem(config.image_prefix)) if p]
    if problems:
        for problem in problems:
            print(f"not started: {problem}", file=sys.stderr)
        return 2
    command = f"fidelity spike slot {args.slot}"
    stack = None
    try:
        with guard_module.hold(command) as held:
            stack = runs.SlotStack(args.slot, stacks_root=config.stacks_root,
                                   project_prefix=config.project_prefix, rules=config.rules,
                                   guard=held)
            held.before_run([command])
            record = run_spike(app=args.app, task=args.task, code=args.variant, slot=args.slot,
                               config=config, out=args.out, stack=stack, steps=args.steps)
    except (guard_module.Refused, guard_module.Overlap) as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return 3
    except ENVIRONMENT as exc:
        if stack is not None and stack.overlap is not None:  # a call that waited on that reset
            return _overlapped(stack.overlap, args.out)
        print(f"environment: {type(exc).__name__}: {exc}; nothing was decided (the record: "
              f"{args.out / 'spike.json'})", file=sys.stderr)
        return 4
    except Exception as exc:  # noqa: BLE001 - a fault of this code: its traceback, for a look
        return _stopped(exc)
    if stack.overlap is not None:
        return _overlapped(stack.overlap, args.out)
    try:
        found = (checks(record, start_url=record["start_url"] or "") if args.steps == "all"
                 else run_checks(record))
    except Exception as exc:  # noqa: BLE001 - as above
        return _stopped(exc)
    for name, ok, seen in found:
        print(f"{'pass' if ok else 'FAIL'}  {name}: {seen[:200]}")
    print(f"record: {args.out / 'spike.json'}")
    unanswered = [entry for entry in record["calls"] if entry.get("environment")]
    if unanswered:
        print(f"environment: {unanswered[0]['tool']}: {unanswered[0]['error']}; nothing was "
              f"decided", file=sys.stderr)
        return 4
    if args.steps == "run" and not decided(record):
        print("environment: a reset failed or the control directory holds an environment "
              "record; nothing was decided", file=sys.stderr)
        return 4
    return 0 if all(ok for _, ok, _ in found) else 1


if __name__ == "__main__":
    sys.exit(main())
