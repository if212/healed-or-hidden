"""The ``run_test`` tool: an MCP server (stdio) with one tool and server-side counting.

All behaviour lives in ``RunTestService``; the MCP layer only forwards calls.
Stdout is the transport, so everything this process logs goes to a file under
the session directory.

Two empty marker files in the session directory tell the session worker not to
judge the session: ``env_failed`` (the environment never came up for a call)
and ``harness_error`` (a call failed for any other reason, a fault of the
harness rather than of the environment; it is not retried).

The first call of a session is free when it comes before any change: if
``session.json`` names ``original_tests`` (an untouched copy of the handed-out
``tests/``) and the first call finds the same files with the same bytes, the
test runs as usual but the call does not count. Its files go to ``runs/00``;
counted call N uses ``runs/NN``. Every entry in ``calls`` says whether it
``counted``.

``run_test_state.json`` is written when a call starts and when it ends. A call
in progress is an entry with ``started_at`` only; its end adds ``status``,
``seconds`` and the rest. A restarted server marks an entry that never ended as
``interrupted`` (not counted; ``seconds`` runs up to the restart, an upper
bound) and moves that call's directories to ``runs/NN-interruptedJ``.

The server never runs with a credential. The CLI passes its own environment on
to it, and the MCP configuration empties ``CREDENTIAL_VARIABLES`` over that; a
server that still finds one of them holding a value exits before it serves,
naming the variable, never the value.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, TextContent

from hoh.backend import EnvFailure, Variant
from hoh.fake_backend import load_backend

TOOL_NAME = "run_test"
SERVER_NAME = "runner"
TOOL_DESCRIPTION = (
    "Run the test in tests/ against a fresh copy of the application and refresh test-results/."
)
ENV_FAILED_TEXT = (
    "The test environment could not be started. This call did not count toward the limit."
)
HARNESS_ERROR_TEXT = "run_test could not be completed."
ENV_FAILED_MARKER = "env_failed"
HARNESS_ERROR_MARKER = "harness_error"
# Every variable the CLI reads a credential from. hoh.session empties them in the MCP
# configuration; the server refuses to start while one of them holds a value.
CREDENTIAL_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
_RUN_DIR = re.compile(r"(\d+)(?:-retry\d+)?")  # runs/NN or runs/NN-retryK of call NN

log = logging.getLogger("hoh.run_test_server")


def _write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _files(root: Path) -> dict[str, bytes]:
    """Every file under ``root``, by path relative to it, with its bytes."""
    if not root.is_dir():
        raise NotADirectoryError(f"{root} is not a directory")
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _replace_dir_contents(dest: Path, src: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for child in dest.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)


class SlotTakenOver(RuntimeError):
    """The slot's link no longer points at this server's session: another session owns it."""


class RunTestService:
    def __init__(self, session_dir: Path):
        # Started through the slot's link. Everything of this session is written to the
        # directory the link named at start; the workspace is the slot's, so before touching it
        # (or running anything) the server checks that the link still names its session.
        self.link = Path(session_dir)
        self.session_dir = self.link.resolve()
        cfg = json.loads((self.session_dir / "session.json").read_text())
        self.workspace = Path(cfg["workspace"])
        self.variant = Variant(**cfg["variant"])
        self.max_runs = int(cfg["max_runs"])
        self.env_retries = int(cfg.get("env_retries", 2))
        original = cfg.get("original_tests")
        self.original_tests = Path(original) if original else None
        self.backend = load_backend(cfg["backend"])
        self._lock = threading.Lock()
        self._state_path = self.session_dir / "run_test_state.json"
        if self._state_path.exists():
            self.state = json.loads(self._state_path.read_text())
            self._close_interrupted_calls()
        else:
            self.state = {"count": 0, "refused": 0, "env_retries": 0, "calls": []}

    def _save(self) -> None:
        _write_json_atomic(self._state_path, self.state)

    def _end(self, entry: dict[str, Any], **fields: Any) -> None:
        """Complete the entry of a call that has ended, and write the state."""
        entry.update(fields)
        entry["seconds"] = round(time.time() - entry["started_at"], 3)
        self._save()

    def _close_interrupted_calls(self) -> None:
        """After a restart: close the calls the previous server never finished.

        Their directories (runs/NN and runs/NN-retryK of a call that has no finished run) are
        moved aside, so the next call with that number cannot delete them.
        """
        calls = self.state["calls"]
        cut = [c for c in calls if "status" not in c]
        finished = {c["call"] for c in calls if c.get("call") is not None}
        runs = self.session_dir / "runs"
        leftovers: dict[int, list[Path]] = {}
        if runs.is_dir():
            for p in sorted(runs.iterdir()):
                m = _RUN_DIR.fullmatch(p.name)
                if m and int(m.group(1)) not in finished:
                    leftovers.setdefault(int(m.group(1)), []).append(p)
        if not cut and not leftovers:
            return
        for n, dirs in leftovers.items():
            self._set_aside(runs, n, "interrupted", dirs)
        now = time.time()
        for c in cut:
            c.update(
                call=None,
                status="interrupted",
                counted=False,
                seconds=round(now - c["started_at"], 3),
            )
        self._save()

    def _set_aside(self, runs: Path, n: int, status: str, dirs: list[Path]) -> None:
        """Move the directories of a call that did not count out of the next call's way.

        Call this before the call's entry gets its ``status``: the holder is numbered by the
        calls that already have ``status``.
        """
        j = sum(1 for c in self.state["calls"] if c.get("status") == status) + 1
        holder = runs / f"{n:02d}-{status.replace('_', '')}{j}"
        holder.mkdir(parents=True, exist_ok=True)
        for d in dirs:
            if d.exists():
                shutil.move(str(d), holder / d.name)

    def call(self) -> tuple[str, bool]:
        """One tool call. Returns the text for the agent and the is-error flag."""
        with self._lock:
            return self._call()

    def _check_slot(self) -> None:
        if self.link.resolve() != self.session_dir:
            raise SlotTakenOver(
                f"{self.link} now names {self.link.resolve()}, not this server's session "
                f"{self.session_dir}"
            )

    def _call(self) -> tuple[str, bool]:
        st = self.state
        first = not st["calls"]
        # On disk from the start: the launcher takes the time spent in run_test off the agent's.
        entry: dict[str, Any] = {"started_at": time.time()}
        st["calls"].append(entry)
        self._save()
        if st["count"] >= self.max_runs:
            st["refused"] += 1
            self._end(entry, call=None, status="refused", counted=False)
            return (
                f"run_test was not run: the limit of {self.max_runs} calls has been reached.",
                True,
            )

        counted = True
        n = st["count"] + 1
        runs = self.session_dir / "runs"
        run_dir = runs / f"{n:02d}"
        tried: list[Path] = []  # the output directory of every attempt that was started
        record = None
        try:
            self._check_slot()
            if run_dir.exists():  # left over from a call that was killed mid-run
                shutil.rmtree(run_dir)
            snapshot = run_dir / "snapshot"
            tests = self.workspace / "tests"
            if tests.is_dir():
                shutil.copytree(tests, snapshot)
            else:
                snapshot.mkdir(parents=True)
            # Decided on the snapshot, which is exactly what runs. A missing copy of the
            # handed-out tests raises: counting the call would quietly cost the agent a run.
            if (
                first
                and self.original_tests is not None
                and _files(snapshot) == _files(self.original_tests)
            ):
                counted, n = False, 0
                free_dir = runs / f"{n:02d}"
                if free_dir.exists():  # left over from a free call that was killed mid-run
                    shutil.rmtree(free_dir)
                run_dir = run_dir.rename(free_dir)
                snapshot = run_dir / "snapshot"

            for attempt in range(self.env_retries + 1):
                out = run_dir if attempt == 0 else runs / f"{n:02d}-retry{attempt}"
                tried.append(out)
                try:
                    record = self.backend.run(snapshot, self.variant, out)
                    break
                except EnvFailure as exc:
                    log.warning("call %d attempt %d: environment failure: %s", n, attempt, exc)
            if record is not None:
                self._check_slot()
                _replace_dir_contents(self.workspace / "test-results", record.results_dir)
        except Exception as exc:  # not the environment: a fault of the harness, never retried
            log.exception("call %d: run_test could not be completed", n)
            st["env_retries"] += max(len(tried) - 1, 0)
            (self.session_dir / HARNESS_ERROR_MARKER).write_text("")
            self._set_aside(runs, n, "harness_error", [run_dir, *tried[1:]])
            self._end(
                entry,
                call=None,
                status="harness_error",
                counted=False,
                attempts=len(tried),
                error=f"{type(exc).__name__}: {exc}",
            )
            return HARNESS_ERROR_TEXT, True
        retries_used = len(tried) - 1
        st["env_retries"] += retries_used

        if record is None:
            self._set_aside(runs, n, "env_failed", tried)
            (self.session_dir / ENV_FAILED_MARKER).write_text("")
            self._end(
                entry, call=None, status="env_failed", counted=False, attempts=retries_used + 1
            )
            return ENV_FAILED_TEXT, True

        if counted:
            st["count"] = n
        self._end(
            entry,
            call=n,  # the run directory's number; 0 is the free first call
            status=record.status,
            counted=counted,
            attempts=retries_used + 1,
        )
        text = record.log if record.log.endswith("\n") or not record.log else record.log + "\n"
        if not counted:
            used = f"{st['count']} of {self.max_runs} used"
            return f"{text}[run_test: not counted, no changes yet; {used}]", False
        return f"{text}[run_test call {n} of {self.max_runs}]", False


def build_server(session_dir: Path):
    from mcp_types import CallToolResult

    service = RunTestService(session_dir)
    server = MCPServer(SERVER_NAME)

    @server.tool(name=TOOL_NAME, description=TOOL_DESCRIPTION)
    async def run_test() -> CallToolResult:
        try:
            text, is_error = await anyio.to_thread.run_sync(service.call)
        except Exception:  # the service could not even keep its records
            log.exception("run_test crashed")
            text, is_error = HARNESS_ERROR_TEXT, True
        return CallToolResult(content=[TextContent(type="text", text=text)], is_error=is_error)

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(
        filename=args.session_dir / "run_test_server.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    found = [name for name in CREDENTIAL_VARIABLES if os.environ.get(name)]
    if found:
        # Names only: the value must not reach the log, stderr or the exit message.
        message = f"refusing to start: a credential variable holds a value ({', '.join(found)})"
        log.error(message)
        raise SystemExit(f"hoh.run_test_server: {message}")
    build_server(args.session_dir).run("stdio")


if __name__ == "__main__":
    main()
