"""A fidelity session's slot: the stack, the network and the healer's MCP server (design doc 5.6).

The official healer runs its tests inside its own MCP server (``playwright
run-test-mcp-server``), which runs in a container of the TypeScript runner image. Every stack of
the check lives in one of three slots, 7, 8 and 9 under ``/Users/Shared/hoh-fid``, as the Compose
project ``hohfid-<N>``. In a session, the stack is made again from nothing before each test (a
reset), while the server's container keeps running; so the stack's default network is the slot's
own network, made outside Compose, which ``docker compose down`` leaves in place:

- ``ensure_network``: the slot's network, made if missing.
- ``join_network``: an assembled stack with that network as its default network, declared
  external in the stack's override file. ``hoh.envs.compose`` gives Docker the stack's two
  compose files and no other, so the change goes into the override, as ``hoh.fidelity.stack``
  puts the TypeScript runner there; ``hoh.envs.check_intact`` reads containers and the browser's
  log, not the files. None of the apps' compose files names a network, so every service, the
  browser too, is on the default network, where the server's container reaches it by its
  service name.
- ``server_environment``: the whole environment of the server's container. Its ``HOH_``
  settings are read from the stack's ``runner`` service, which is what a judge's run gets from
  ``TsBackend``; the ``PWFIX_`` ones tell the fixture to ask for a reset before each test, through
  the control directory.
- ``server_command``: the ``docker run`` line that starts the server in the TypeScript runner
  image, on the slot's network, with the workspace (read-write, at the same absolute path, so
  the paths the server reports are the agent's) and the control directory (read-write, at
  ``CONTROL_TARGET``) as its only mounts, and that table as all it is given.
- ``ResetWatcher``: a thread in the session's process that answers the fixture's requests for a
  fresh stack. The fixture (src/pwfix/lib/control.js) writes ``requests/<id>.json`` into the
  control directory before each test and waits for ``answers/<id>.json``; the watcher makes the
  stack again (``recreate``: Compose ``down --volumes``, ``up``, the wait for the browser's
  ``[init] Ready``, as ``hoh.envs`` starts every run's stack) and answers ``{"ok": true}``, or
  ``{"ok": false, "error": RESET_FAILED}``. The answer reaches the agent's test output, so it
  says no more than that; what went wrong goes into the watcher's record of resets. A session
  with a failed reset is discarded: an environment failure, never a label. Given the
  workspace's ``tests/``, the watcher first copies it into the attempt's next snapshot: the test
  as the run that asked for the reset found it, for the Kept-red subtype. The study's
  ``run_test`` server keeps one at each call; a run that starts no test asks for no reset and
  leaves none (``hoh.fidelity.judge``, point 7).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from hoh import envs
from hoh.backend import EnvFailure
from hoh.fidelity import stack
from hoh.pagescripts import READY_KEY

Run = Callable[..., subprocess.CompletedProcess]

#: The fidelity check's slots; every stack of the check lives in one of them.
SLOTS = (7, 8, 9)
STACKS_ROOT = Path("/Users/Shared/hoh-fid")
PROJECT_PREFIX = "hohfid"
#: The label that marks a network as a slot's, with the slot's number.
NETWORK_LABEL = "hoh.fidelity.slot"
DOCKER_TIMEOUT_S = 60.0

#: How long a reset may take to bring the stack up and ready: what ``DockerBackend`` gives a
#: judge's run (its ``start_timeout_s``).
START_TIMEOUT_S = 900.0
#: ``PWFIX_RESET_TIMEOUT_S``, how long the fixture waits for a reset's answer: the start budget,
#: plus the ``down`` that ``hoh.envs.start`` runs first (up to 120 s) and a last readiness check
#: (up to 120 s), plus a minute, so that the watcher's answer, failed or not, comes first.
RESET_TIMEOUT_S = 1200
#: Where the server's container sees the control directory (``PWFIX_CONTROL_DIR``).
CONTROL_TARGET = "/control"
#: Where docker/runner-ts/Dockerfile puts the fixture package (``pwrun.entry.PWFIX_DIR``), its
#: Playwright Test, which the judge's runs use too, and the configuration it bakes in.
PWFIX_DIR = PurePosixPath("/opt/node/pwfix")
CLI = PWFIX_DIR / "node_modules" / "@playwright" / "test" / "cli.js"
CONFIG = PWFIX_DIR / "playwright.config.js"
#: What the stack's runner service gets (``hoh.envs``), and with it the server.
RUNNER_VARIABLES = ("HOH_CDP_URL", "HOH_START_URL", "HOH_READY_KEY", *envs.RUNNER_TIMING)
#: The server container's whole environment, in this order.
SERVER_VARIABLES = (*RUNNER_VARIABLES, "PWFIX_CONTROL_DIR", "PWFIX_RESET", "PWFIX_RESET_TIMEOUT_S")

#: What a failed reset's answer says. The fixture puts it in the test's error, which the agent
#: reads, so it names nothing of the stack.
RESET_FAILED = "the test environment could not be started"
POLL_S = 0.25

_NETWORK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_TELLTALE = re.compile(r"bug|inject|benign|scenario", re.I)
#: A request's id, as the fixture's control.newId makes it (the file is ``<id>.json``).
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_JOINED = "# hoh.fidelity.slot put the stack on its slot's network, made outside Compose.\n"


def network_name(number: int) -> str:
    """The name of slot ``number``'s network, ``hohfid-<N>-net``. Raises ``ValueError`` for a
    slot outside ``SLOTS``."""
    if isinstance(number, bool) or number not in SLOTS:
        raise ValueError(f"slot {number!r}: the fidelity check's slots are {SLOTS}")
    return f"{PROJECT_PREFIX}-{number}-net"


def ensure_network(number: int, *, run: Run = subprocess.run) -> str:
    """Slot ``number``'s network: kept when it is there, else made, outside Compose, as a bridge
    network with the slot's label. Returns its name. Raises ``EnvFailure`` when Docker cannot make
    it, and when a network of that name is there but was not made for the slot."""
    name = network_name(number)
    found = _inspect(name, run)
    if found is None:
        made = _docker(run, "network", "create", "--driver", "bridge",
                       "--label", f"{NETWORK_LABEL}={number}", name)
        if made.returncode != 0:
            found = _inspect(name, run)  # made meanwhile by another process?
            if found is None:
                raise EnvFailure(f"docker could not make the network {name}: "
                                 f"{(made.stderr or '').strip()}")
        else:
            return name
    labels = found.get("Labels") or {}
    if found.get("Driver") != "bridge" or labels.get(NETWORK_LABEL) != str(number):
        raise EnvFailure(
            f"a network named {name} is there, but not as slot {number}'s: driver "
            f"{found.get('Driver')!r}, labels {labels}; remove it, or give the slot another"
        )
    return name


def join_network(stack_dir: Path, network: str) -> None:
    """Give the stack ``network`` as its default network, external (module docstring). Joining
    the same network again changes nothing. Raises ``ValueError`` when the stack's compose files
    name networks of their own, or when the stack is on another network already."""
    if not _NETWORK.fullmatch(network or ""):
        raise ValueError(f"{network!r} is not a network name")
    stack_dir = Path(stack_dir)
    path = stack_dir / envs.OVERRIDE_FILE
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    wanted = {"default": {"name": network, "external": True}}
    if data.get("networks") == wanted:
        return
    base = yaml.safe_load((stack_dir / envs.COMPOSE_FILE).read_text(encoding="utf-8")) or {}
    for name, found in ((envs.COMPOSE_FILE, base), (envs.OVERRIDE_FILE, data)):
        if found.get("networks") is not None:
            raise ValueError(f"{stack_dir / name} names networks already: {found['networks']}")
        for service, spec in (found.get("services") or {}).items():
            named = sorted({"networks", "network_mode"} & set(spec or {}))
            if named:
                raise ValueError(f"{stack_dir / name}: {service} sets {', '.join(named)}; on the "
                                 "slot's network it would not be on the default network")
    lines = text.splitlines(keepends=True)
    header = 0
    while header < len(lines) and lines[header].startswith("#"):
        header += 1
    data["networks"] = wanted
    path.write_text(
        "".join(lines[:header]) + _JOINED
        + yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=1000),
        encoding="utf-8",
    )


def server_environment(stack_dir: Path) -> dict[str, str]:
    """The whole environment of the server's container (module docstring), in the order of
    ``SERVER_VARIABLES``. Raises ``ValueError`` when the stack's runner settings are not the ones
    ``hoh.envs`` writes, with the study's browser address, ready key and timing."""
    path = Path(stack_dir) / envs.OVERRIDE_FILE
    services = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("services") or {}
    runner = (services.get("runner") or {}).get("environment")
    if not isinstance(runner, dict) or set(runner) != set(RUNNER_VARIABLES):
        raise ValueError(f"{path}: the runner's settings are not the ones hoh.envs writes: "
                         f"{runner}")
    runner = {name: str(value) for name, value in runner.items()}
    study = {"HOH_CDP_URL": envs.CDP_URL, "HOH_READY_KEY": READY_KEY, **envs.RUNNER_TIMING}
    differ = {name: runner[name] for name, value in study.items() if runner[name] != value}
    if differ:
        raise ValueError(f"{path}: the runner's settings are not the study's: {differ}")
    return {
        **{name: runner[name] for name in RUNNER_VARIABLES},
        "PWFIX_CONTROL_DIR": CONTROL_TARGET,
        "PWFIX_RESET": "1",
        "PWFIX_RESET_TIMEOUT_S": str(RESET_TIMEOUT_S),
    }


def server_command(
    *,
    network: str,
    workspace: Path,
    control_dir: Path,
    environment: Mapping[str, str],
    image_prefix: str = "hoh",
    name: str | None = None,
) -> list[str]:
    """The ``docker run`` line that starts the healer's MCP server (module docstring):
    ``playwright run-test-mcp-server -c <config>``, its standard input and output the MCP
    transport. ``environment`` is ``server_environment``'s table; ``name`` names the container.
    Raises ``ValueError`` for a path the container would show with a word it must not see, a
    path ``--mount`` would misread, a control directory not apart from the workspace, and an
    environment that is not the table."""
    workspace = _host_path(workspace, "workspace")
    control_dir = _host_path(control_dir, "control directory")
    real = Path(os.path.realpath(workspace)), Path(os.path.realpath(control_dir))
    if real[0] == real[1] or real[0] in real[1].parents or real[1] in real[0].parents:
        raise ValueError(f"the workspace {workspace} and the control directory {control_dir} "
                         "must be apart")
    if list(environment) != list(SERVER_VARIABLES):
        raise ValueError(f"the server's environment must be {SERVER_VARIABLES}, in that order; "
                         f"got {tuple(environment)}")
    if environment["PWFIX_CONTROL_DIR"] != CONTROL_TARGET:
        raise ValueError(f"the server's environment names the control directory "
                         f"{environment['PWFIX_CONTROL_DIR']!r}; it is mounted at {CONTROL_TARGET}")
    if not _NETWORK.fullmatch(network or "") or (name is not None and not _NETWORK.fullmatch(name)):
        raise ValueError(f"not a network and container name: {network!r}, {name!r}")
    cmd = ["docker", "run", "-i", "--rm", "--init", "--pull", "never", "--network", network]
    if name is not None:
        cmd += ["--name", name]
    cmd += [
        "--workdir", str(workspace),
        "--mount", f"type=bind,source={workspace},target={workspace}",
        "--mount", f"type=bind,source={control_dir},target={CONTROL_TARGET}",
    ]
    for variable in SERVER_VARIABLES:
        cmd += ["--env", f"{variable}={environment[variable]}"]
    return [*cmd, f"{image_prefix}/{stack.RUNNER_TS_IMAGE}",
            "node", str(CLI), "run-test-mcp-server", "-c", str(CONFIG)]


def _host_path(path: Path, what: str) -> Path:
    """An absolute host path for a bind mount, checked as hoh.envs checks a stack's path: the
    container shows it in its mount table."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError(f"the {what} must be an absolute path, not {path}")
    path = Path(os.path.normpath(path))
    for shown in dict.fromkeys((str(path), os.path.realpath(path))):
        if any(mark in shown for mark in ',"\n'):
            raise ValueError(f"the {what}'s path {shown} has a character docker's --mount "
                             "would misread")
        found = _TELLTALE.search(shown)
        if found:
            raise ValueError(f"the {what}'s path {shown} contains {found.group()!r}; the "
                             "server's container shows it in its mount table")
    return path


def recreate(stack_dir: Path, *, timeout_s: float = START_TIMEOUT_S) -> None:
    """Make the stack again from nothing, as ``DockerBackend`` starts a run's stack: Compose
    ``down --volumes`` and ``up`` (``hoh.envs.start``), then the wait for the browser's
    ``[init] Ready`` (``hoh.envs.wait_ready``), both within ``timeout_s``. Raises
    ``EnvFailure``."""
    deadline = time.monotonic() + timeout_s
    envs.start(stack_dir, timeout_s=timeout_s)
    envs.wait_ready(stack_dir, timeout_s=max(deadline - time.monotonic(), 0.0))


class ResetWatcher:
    """Answers the fixture's requests for a fresh stack (module docstring), one at a time, in the
    order they came, each once.

    ``recreate(stack_dir, timeout_s=...)`` makes the stack again (``recreate`` by default; the
    tests give a stand-in). Every reset is recorded (``resets``): its request's id, when it began
    (epoch seconds), how long it took, and whether it worked or why not; with ``log``, the
    records are also written whole to that file after each reset, as ``{"resets": [...],
    "error": ...}``. Keep ``log`` out of the control directory, which the agent's tests can
    write to. ``error`` says what ended the watcher when anything but ``stop`` did.

    With ``tests_dir`` and ``snapshot_root``, each reset first copies ``tests_dir`` as it is into
    ``snapshot_root/NN`` (01, 02, ... in the order of the resets; empty when ``tests_dir`` is
    gone), and its record names it (``snapshot``). A copy that fails fails the reset, so that no
    label loses its subtype unseen. Keep ``snapshot_root`` out of the agent's slot.
    """

    def __init__(
        self,
        stack_dir: Path,
        control_dir: Path,
        *,
        recreate: Callable[..., None] = recreate,
        timeout_s: float = START_TIMEOUT_S,
        poll_s: float = POLL_S,
        log: Path | None = None,
        tests_dir: Path | None = None,
        snapshot_root: Path | None = None,
    ) -> None:
        self.stack_dir = Path(stack_dir)
        self.control_dir = Path(control_dir)
        self.timeout_s = float(timeout_s)
        self.poll_s = float(poll_s)
        self.log = Path(log) if log is not None else None
        self.tests_dir = Path(tests_dir) if tests_dir is not None else None
        self.snapshot_root = Path(snapshot_root) if snapshot_root is not None else None
        self._taken = 0
        self.error: str | None = None
        self._recreate = recreate
        self._resets: list[dict[str, Any]] = []
        self._handled: set[str] = set()
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True,
                                        name=f"reset watcher ({self.stack_dir.name})")

    def start(self) -> ResetWatcher:
        self._thread.start()
        return self

    def stop(self, timeout: float | None = None) -> bool:
        """Stop looking for requests. A reset under way is finished and answered first, within
        ``timeout`` seconds (None: however long it takes). True once the watcher has ended."""
        self._stopping.set()
        if self._thread.ident is not None:
            self._thread.join(timeout)
        return not self._thread.is_alive()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def __enter__(self) -> ResetWatcher:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def resets(self) -> list[dict[str, Any]]:
        """Every reset so far, in order."""
        with self._lock:
            return [dict(record) for record in self._resets]

    @property
    def failures(self) -> list[dict[str, Any]]:
        """The resets that did not give a fresh stack, or whose answer could not be written."""
        return [record for record in self.resets if not record["ok"]]

    def _watch(self) -> None:
        try:
            while not self._stopping.is_set():
                for rid in self._waiting():
                    if self._stopping.is_set():
                        break
                    self._reset(rid)
                self._stopping.wait(self.poll_s)
        except Exception as exc:  # the session reads it: a watcher that fails discards it
            self.error = f"{type(exc).__name__}: {exc}"
            with contextlib.suppress(OSError):
                self._save()

    def _waiting(self) -> list[str]:
        """The ids of the requests not yet answered, oldest first. Anything in ``requests/``
        that is not ``<id>.json``, a file, is left alone, as is a request with an answer."""
        folder = self.control_dir / "requests"
        if not folder.exists():
            return []
        found = []
        for path in folder.iterdir():
            rid = path.name.removesuffix(".json")
            if rid == path.name or rid in self._handled or not _REQUEST_ID.fullmatch(rid):
                continue
            if (self.control_dir / "answers" / path.name).exists():
                self._handled.add(rid)
                continue
            try:
                if path.is_file():
                    found.append((path.stat().st_mtime_ns, rid))
            except FileNotFoundError:
                continue
        return [rid for _, rid in sorted(found)]

    def _snapshot(self) -> str | None:
        """Copy ``tests_dir`` into the next snapshot; its name, or None without one to take."""
        if self.tests_dir is None or self.snapshot_root is None:
            return None
        self._taken += 1
        name = f"{self._taken:02d}"
        dest = self.snapshot_root / name
        if self.tests_dir.is_dir():
            shutil.copytree(self.tests_dir, dest, symlinks=True)
        else:
            dest.mkdir(parents=True)
        return name

    def _reset(self, rid: str) -> None:
        self._handled.add(rid)
        started_at, begun = time.time(), time.monotonic()
        error = snapshot = None
        try:
            snapshot = self._snapshot()
            self._recreate(self.stack_dir, timeout_s=self.timeout_s)
        except Exception as exc:  # an environment failure, whatever raised it
            error = f"{type(exc).__name__}: {exc}"
        seconds = round(time.monotonic() - begun, 3)
        answer = {"ok": True} if error is None else {"ok": False, "error": RESET_FAILED}
        try:
            _write_whole(self.control_dir / "answers" / f"{rid}.json", answer)
        except OSError as exc:
            unwritten = f"the answer could not be written: {type(exc).__name__}: {exc}"
            error = unwritten if error is None else f"{error}; {unwritten}"
        record = {"id": rid, "started_at": started_at, "seconds": seconds,
                  "ok": error is None, "error": error, "snapshot": snapshot}
        with self._lock:
            self._resets.append(record)
        self._save()

    def _save(self) -> None:
        if self.log is not None:
            _write_whole(self.log, {"resets": self.resets, "error": self.error})


def _write_whole(path: Path, data: Any) -> None:
    """Write a JSON file whole, as the fixture's writeWhole does: ``<file>.part``, renamed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.part")
    partial.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, path)


def _inspect(name: str, run: Run) -> dict | None:
    """What ``docker network inspect`` says of the network, or None when it is not there."""
    done = _docker(run, "network", "inspect", name)
    if done.returncode != 0:
        return None
    try:
        found = json.loads(done.stdout)
    except ValueError:
        found = None
    if not (isinstance(found, list) and len(found) == 1 and isinstance(found[0], dict)):
        raise EnvFailure(f"docker network inspect {name} gave no network: {done.stdout!r:.200}")
    return found[0]


def _docker(run: Run, *args: str) -> subprocess.CompletedProcess:
    try:
        return run(["docker", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                   timeout=DOCKER_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        raise EnvFailure(f"docker {' '.join(args)} did not answer within "
                         f"{DOCKER_TIMEOUT_S:g} s") from None
    except OSError as exc:
        raise EnvFailure(f"docker {' '.join(args)} could not run: {exc}") from None
