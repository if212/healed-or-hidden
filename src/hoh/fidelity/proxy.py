"""The relay between the Claude Code CLI and the healer's MCP server, which times its tool calls.

The CLI starts the server through it, as ``python -I -m hoh.fidelity.proxy --state FILE --
COMMAND...``, COMMAND being the ``docker run`` line of ``hoh.fidelity.slot.server_command``. The
proxy starts COMMAND and passes the two standard streams between the CLI and it: every byte, both
ways, unchanged and in order. The server's standard error is the proxy's own.

On the way it reads the messages (the MCP stdio transport: a JSON-RPC message, or a batch, per
line) to time the server's tool calls as the H arm's run_test server times its own, in a file that
``hoh.session._state_tool_time`` reads. A ``tools/call`` request from the CLI opens a call under
its id; the server's response with that id, a result or an error, ends it, and so does the CLI's
``notifications/cancelled`` for it. A call still open when the server ends is closed then. A call
is on file from the moment its request is read, before the server has it, and its end is on file
before its answer reaches the CLI. The file is written whole at each change::

    {"schema": 2,
     "server": {"command": [...], "pid": 4242, "started_at": 1763300000.0, "ended_at": null,
                "exit_code": null, "error": null},
     "calls": [{"started_at": 1763300012.3, "seconds": 2.5},
               {"started_at": 1763300016.0}],
     "tool_calls": [{"id": 5, "tool": "test_run", "started_at": 1763300012.3, "seconds": 2.5,
                     "end": "result"},
                    {"id": 6, "tool": "test_debug", "started_at": 1763300016.0}]}

``tool_calls`` holds the calls one by one: ``end`` is ``result``, ``error``, ``cancelled`` or
``no_response``, and a call under way has neither ``seconds`` nor ``end``. ``calls``, what the
session reads, holds the spans in which at least one call was under way: the CLI may run calls
side by side, and the session's cap counts the agent's own time, the time outside every call, so
time inside two calls at once is taken away once. A span is open while a call is. Lines that are
not JSON, and messages that are none of these, pass without a trace.

The CLI passes its own environment on to its servers, and the MCP configuration empties the
credential variables over it (``hoh.fidelity.session.mcp_config``). As the study's run_test server
does, a proxy that still finds one of them holding a value starts no server: it names the
variable, never the value, and exits 1.

The proxy ends with its server and exits with the server's code (128 + N when signal N ended it).
When its own standard input closes, it closes the server's; a server still running ``--grace``
seconds later gets SIGTERM, and SIGKILL as long after that. SIGTERM, SIGINT and SIGHUP to the
proxy are passed on to the server, with the same grace after them. A state file that cannot be
written at the start stops the proxy before it starts the server. Only the standard library is
imported, so that the server starts quickly.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: How long a server may take to end after its input has closed, or after a signal, before it
#: gets SIGTERM, and as long again before SIGKILL. Playwright's run-test-mcp-server closes within
#: 15 s (its exit watchdog).
GRACE_S = 20.0
#: How long the proxy waits for the last output of a server that has ended.
DRAIN_S = 5.0
CHUNK = 1 << 16
POLL_S = 0.1
SCHEMA = 2
#: Every variable the CLI reads a credential from: ``hoh.run_test_server.CREDENTIAL_VARIABLES``,
#: which a test keeps equal (that module is not imported, so that the server starts quickly).
CREDENTIAL_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
#: The exit codes when the server's command cannot be run at all, as a shell gives them.
NOT_FOUND, NOT_RUNNABLE = 127, 126
#: The signals the proxy passes on to its server.
PASSED_ON = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def _say(text: str) -> None:
    try:
        print(f"hoh.fidelity.proxy: {text}", file=sys.stderr, flush=True)
    except (OSError, ValueError):  # a note that cannot be written never stops the relay
        pass


class Calls:
    """The server and its tool calls, kept in the state file."""

    def __init__(self, path: Path, command: list[str]) -> None:
        self.path = path
        self.server: dict[str, Any] = {
            "command": list(command), "pid": None, "started_at": time.time(), "ended_at": None,
            "exit_code": None, "error": None,
        }
        self._calls: list[dict[str, Any]] = []
        self._open: dict[str, tuple[dict[str, Any], float]] = {}
        #: The spans in which a call was under way, and the open one with its start.
        self._periods: list[dict[str, Any]] = []
        self._period: tuple[dict[str, Any], float] | None = None
        self._lock = threading.Lock()
        self._warned = False

    @property
    def running(self) -> bool:
        """Whether a call is under way."""
        return bool(self._open)

    def save(self) -> None:
        """Write the file whole: a file beside it, then a rename. Raises OSError."""
        text = json.dumps({"schema": SCHEMA, "server": self.server, "calls": self._periods,
                           "tool_calls": self._calls}, indent=1)
        part = self.path.with_name(f".{self.path.name}.tmp")
        part.write_text(text + "\n", encoding="utf-8")
        os.replace(part, self.path)

    def started(self, pid: int) -> None:
        with self._lock:
            self.server["pid"] = pid
            self._record()

    def begin(self, key: str, call_id: Any, tool: str | None) -> None:
        with self._lock:
            if key in self._open:  # the id of a call under way, again: that call has no answer
                self._close(key, "no_response")
            began = time.monotonic()
            entry = {"id": call_id, "tool": tool, "started_at": time.time()}
            self._calls.append(entry)
            self._open[key] = (entry, began)
            if self._period is None:
                period = {"started_at": entry["started_at"]}
                self._periods.append(period)
                self._period = (period, began)
            self._record()

    def end(self, key: str, how: str) -> None:
        with self._lock:
            if key in self._open:
                self._close(key, how)
                self._record()

    def ended(self, exit_code: int | None, error: str | None = None) -> None:
        """The server has ended, or could not start: the calls still open are closed."""
        with self._lock:
            for key in list(self._open):
                self._close(key, "no_response")
            self.server.update(ended_at=time.time(), exit_code=exit_code, error=error)
            self._record()

    def _close(self, key: str, how: str) -> None:
        now = time.monotonic()
        entry, began = self._open.pop(key)
        entry["seconds"] = round(now - began, 3)
        entry["end"] = how
        if not self._open and self._period is not None:  # the last call under way has ended
            period, since = self._period
            period["seconds"] = round(now - since, 3)
            self._period = None

    def _record(self) -> None:
        """``save``, with the lock held. A failure is reported once, and the relay goes on."""
        try:
            self.save()
        except OSError as exc:
            if not self._warned:
                self._warned = True
                _say(f"the state file {self.path} could not be written: {exc}")


class _Lines:
    """The complete lines of a stream read in chunks, without their line feeds."""

    def __init__(self) -> None:
        self._part = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        lines, start = [], 0
        while (end := chunk.find(b"\n", start)) >= 0:
            self._part += chunk[start:end]
            lines.append(bytes(self._part))
            self._part.clear()
            start = end + 1
        self._part += chunk[start:]
        return lines


def _messages(line: bytes) -> list[dict[str, Any]]:
    """The JSON-RPC messages of a line: one, the members of a batch, or none."""
    try:
        data = json.loads(line)
    except (ValueError, RecursionError):
        return []
    return [item for item in (data if isinstance(data, list) else [data])
            if isinstance(item, dict)]


def _key(call_id: Any) -> str | None:
    """A request's id (a string or an integer) as a key: its JSON, so that "7" is not 7."""
    if isinstance(call_id, str) or (isinstance(call_id, int) and not isinstance(call_id, bool)):
        return json.dumps(call_id)
    return None


def _read(fd: int) -> bytes:
    try:
        return os.read(fd, CHUNK)
    except OSError:  # as if the stream had ended
        return b""


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class Relay:
    """The two streams between the CLI (the proxy's standard input and output) and the server."""

    def __init__(self, proc: subprocess.Popen, calls: Calls) -> None:
        self.proc, self.calls = proc, calls
        #: When the server began to be ended (``time.monotonic``), or None.
        self.ending_since: float | None = None
        self._from_server = threading.Thread(target=self._server_to_client, daemon=True)
        self._from_client = threading.Thread(target=self._client_to_server, daemon=True)

    def run(self, grace: float) -> int:
        """Relay until the server has ended; its exit code."""
        for fd in (0, 1):  # a parent may hand them over non-blocking; the relay waits on them
            try:
                os.set_blocking(fd, True)
            except OSError:
                pass
        for sig in PASSED_ON:
            signal.signal(sig, self._signalled)
        self._from_server.start()
        self._from_client.start()
        terminated = killed = False
        while True:
            try:
                code = self.proc.wait(timeout=POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            if self.ending_since is None:
                continue
            waited = time.monotonic() - self.ending_since
            if waited >= 2 * grace and not killed:
                self.proc.kill()
                killed = True
            elif waited >= grace and not terminated:
                self.proc.terminate()
                terminated = True
        self._from_server.join(DRAIN_S)  # what the server wrote last reaches the CLI too
        self.calls.ended(code)
        return code

    def end(self) -> None:
        """Begin to end the server (once): ``run`` gives it the grace, then signals."""
        if self.ending_since is None:
            self.ending_since = time.monotonic()

    def _signalled(self, signum: int, _frame: Any) -> None:
        self.end()
        self.proc.send_signal(signum)

    def _client_to_server(self) -> None:
        lines, sink = _Lines(), self.proc.stdin.fileno()
        server_reads = True
        while chunk := _read(0):
            self._observe(lines.feed(chunk), self._from_client_said)
            if server_reads:
                try:
                    _write_all(sink, chunk)
                except OSError:  # the server reads no more; what the CLI sends is dropped
                    server_reads = False
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.end()

    def _server_to_client(self) -> None:
        lines, source = _Lines(), self.proc.stdout.fileno()
        client_reads = True
        while chunk := _read(source):
            complete = lines.feed(chunk)
            if self.calls.running:  # no response can be one of a call's otherwise
                self._observe(complete, self._from_server_said)
            if client_reads:
                try:
                    _write_all(1, chunk)
                except OSError:  # the CLI is gone: the server is ended, its output drained
                    client_reads = False
                    self.end()

    def _observe(self, lines: list[bytes], said: Callable[[dict[str, Any]], None]) -> None:
        for line in lines:
            try:
                for message in _messages(line):
                    said(message)
            except Exception as exc:  # the timing never stops the relay
                _say(f"a message was passed on but not timed: {exc!r}")

    def _from_client_said(self, message: dict[str, Any]) -> None:
        method, params = message.get("method"), message.get("params")
        if method == "tools/call":
            key = _key(message.get("id"))  # without an id it is a notification: no answer comes
            if key is not None:
                tool = params.get("name") if isinstance(params, dict) else None
                self.calls.begin(key, message["id"], tool if isinstance(tool, str) else None)
        elif method == "notifications/cancelled" and isinstance(params, dict):
            key = _key(params.get("requestId"))
            if key is not None:
                self.calls.end(key, "cancelled")

    def _from_server_said(self, message: dict[str, Any]) -> None:
        if "method" in message:  # a request or a notification of the server's own
            return
        key = _key(message.get("id"))
        if key is not None and ("result" in message or "error" in message):
            self.calls.end(key, "error" if "error" in message else "result")


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {text!r}")
    return value


def _arguments(argv: list[str]) -> tuple[Path, float, list[str]]:
    parser = argparse.ArgumentParser(
        prog="python -I -m hoh.fidelity.proxy",
        usage="%(prog)s --state FILE [--grace SECONDS] -- COMMAND ...",
        description="Relay an MCP server's standard streams and time its tool calls.",
    )
    parser.add_argument("--state", type=Path, required=True,
                        help="the file of the server's tool calls, written whole at each change")
    parser.add_argument("--grace", type=_seconds, default=GRACE_S,
                        help="seconds a server may take to end before SIGTERM, then SIGKILL "
                             f"(default {GRACE_S:g})")
    # Everything after the first "--" is the command, whatever it holds.
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    command = argv[split + 1:]
    if not command:
        parser.error("the server's command is missing: give it after --")
    return args.state, args.grace, command


def main(argv: list[str] | None = None) -> int:
    state, grace, command = _arguments(sys.argv[1:] if argv is None else argv)
    calls = Calls(state, command)
    try:
        calls.save()
    except OSError as exc:
        _say(f"the state file {state} cannot be written, so the server is not started: {exc}")
        return 1
    found = [name for name in CREDENTIAL_VARIABLES if os.environ.get(name)]
    if found:  # names only: the value reaches neither the file nor stderr
        error = f"refusing to start: a credential variable holds a value ({', '.join(found)})"
        calls.ended(None, error)
        _say(error)
        return 1
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
    except (OSError, ValueError) as exc:
        error = f"the server could not be started: {exc}"
        calls.ended(None, error)
        _say(error)
        return NOT_FOUND if isinstance(exc, FileNotFoundError) else NOT_RUNNABLE
    calls.started(proc.pid)
    code = Relay(proc, calls).run(grace)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    # Not sys.exit: the thread that reads the CLI may wait on input that only the CLI can end,
    # and the proxy ends with its server all the same. What it says on stderr is flushed already.
    os._exit(main())
