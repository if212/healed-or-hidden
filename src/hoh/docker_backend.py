"""Run tests against real environments: the ``Backend`` protocol over Docker Compose.

A ``DockerBackend`` owns one slot: the stack directory ``<stacks_root>/slot-<k>`` and the
Compose project ``<prefix>-<k>``. Both names are neutral, because the runner sees the
stack's path in its mount table and the project name in its network's DNS names, and
parallel slots never share either. One run:

1. Stops the slot's previous stack, if one is there (its containers are up after a crash),
   and removes its directory.
2. Assembles a fresh stack for the variant (``hoh.envs.assemble``; a rename or remove
   variant gets its rules from ``rules_for``, or from the rules file ``rules``, which a
   configuration can name: ``hoh.rules``) and copies the tests into its ``tests/``.
3. Starts it from nothing and waits until the browser is ready (``hoh.envs.start``,
   ``wait_ready``), both within ``start_timeout_s``.
4. Runs the tests: ``docker compose run --rm runner``. The runner writes ``out/result.json``
   and the run's files to ``test-results/``.
5. With ``oracle=True``, runs the shadow oracle on the page the tests left:
   ``docker compose run --rm oracle``, which checks the stack's ``check.py`` (the
   benchmark's final assertion, or ``oracle_snippet_for(variant)`` when that gives one; with
   a rules file, a renamed variant's is the reference fix's final assertion).
6. Whatever happened: copies what the containers wrote into ``out_dir``, saves the stack's
   logs and stops the stack.

Failures. What keeps the test from running raises ``EnvFailure``: the stack does not start
or get ready, the runner reports an unusable environment (exit code 3), does not finish
within ``run_timeout_s``, exits with another code, or leaves no usable ``result.json``.
A failure of the oracle after the test ran is not one: the agent's own test may have
closed the tab or cleared the session flag (design doc 5.4). The record then has
``oracle=None`` and ``oracle_error.txt`` says why. A variant that cannot be assembled, a
rename or remove variant without rules, and an oracle without a final assertion are
programming errors and raise as such. Trouble after a good run (the logs cannot be read,
the stack does not stop) does not lose the run: it is noted, and the slot's next run
stops the stack again before anything else, failing there if it still cannot.

``out_dir`` afterwards::

    result.json, pytest-report.json   what the runner wrote to out/
    oracle.json                       what the oracle wrote, with oracle=True
    results/                          the regular files of test-results/: output.log, and
                                      on a failure page-snapshot.yml and screenshot.png
    environment.log                   docker compose logs --no-color --timestamps
    runner-console.log                what `docker compose run` printed for the runner
    oracle-console.log                the same for the oracle
    oracle_error.txt                  why there is no oracle result, when the oracle failed
    backend.json                      slot, project, variant, seconds per phase, errors

Links are never followed. Links in test-results/ and out/ were made in the container and
point at whatever the host has at that path; followed here they could copy any host file
into the agent's test-results/. They are skipped. The tests go into the stack with their
links kept as links, which dangle inside the container.

One slot runs one stack at a time: a run holds an exclusive lock on
``<stacks_root>/slot-<k>.lock``, so a second run on the same slot, from this process or
another, waits instead of tearing down the first one's stack.

The runner image is built from docker/runner/Dockerfile with ``build_runner_image``, or
``python -m hoh.docker_backend build-runner``.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from hoh import envs
from hoh.backend import STATUSES, EnvFailure, RunRecord, Variant
from hoh.benchmark import REPO_ROOT
from hoh.pagescripts import RuleSpec
from hoh.rules import load_rules

#: The arguments of every `docker compose run`. No TTY and no standard input: the run_test
#: server's stdin is its MCP transport, and nothing a run starts may read from it. The
#: runner and the oracle have no dependencies; --no-deps makes sure none is started.
RUN = ("run", "--rm", "--no-deps", "-T", "--interactive=false")
#: The exit code of the runner and the oracle when the environment was unusable.
UNUSABLE = 3
ORACLE_STATUSES = ("pass", "fail", "error")
#: The files of out/ that are kept; the rest of out/ is not ours to copy.
OUT_FILES = ("result.json", "pytest-report.json", "oracle.json")
LOGS_TIMEOUT_S = 120.0
STOP_TIMEOUT_S = 120.0
DOCKERFILE = Path("docker") / "runner" / "Dockerfile"
RUNNER_PACKAGE = Path("src") / "hoh_runner"
RUNNER_LABEL = "hoh.runner.source"
"""Image label holding ``runner_source_digest`` of the code the runner image was built from."""
#: The runner image's build context, relative to the repository.
BUILD_CONTEXT = Path("src")


class DockerBackend:
    """Runs tests in fresh Compose stacks, one at a time, in one slot."""

    def __init__(
        self,
        stacks_root: Path | str,
        slot: int,
        *,
        project_prefix: str = "hoh",
        rules_for: Callable[[Variant], Sequence[RuleSpec]] | None = None,
        start_timeout_s: float = 900,
        run_timeout_s: float = 900,
        oracle_snippet_for: Callable[[Variant], str | None] | None = None,
        rules: Path | str | None = None,
    ) -> None:
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
            raise ValueError(f"slot must be a non-negative integer, got {slot!r}")
        if rules is not None:
            if rules_for is not None or oracle_snippet_for is not None:
                raise ValueError("give the rules file or rules_for and oracle_snippet_for, "
                                 "not both")
            ruleset = load_rules(rules)
            rules_for, oracle_snippet_for = ruleset.specs_for, ruleset.oracle_snippet_for
        self.stacks_root = Path(stacks_root).absolute()
        self.slot = slot
        self.project = f"{project_prefix}-{slot}"
        self.stack_dir = self.stacks_root / f"slot-{slot}"
        self.rules_for = rules_for
        self.oracle_snippet_for = oracle_snippet_for
        self.start_timeout_s = float(start_timeout_s)
        self.run_timeout_s = float(run_timeout_s)

    def run(
        self, tests_dir: Path, variant: Variant, out_dir: Path, *, oracle: bool = False
    ) -> RunRecord:
        """Run every test under ``tests_dir`` once against a fresh ``variant`` (module
        docstring). Raises ``EnvFailure`` when the environment could not be used."""
        tests_dir, out_dir = Path(tests_dir), Path(out_dir)
        rules = self._rules(variant)
        snippet = self._snippet(variant) if oracle else None
        out_dir.mkdir(parents=True, exist_ok=True)
        with self._slot_lock():
            return self._run(tests_dir, variant, out_dir, oracle, rules, snippet)

    def _run(
        self,
        tests_dir: Path,
        variant: Variant,
        out_dir: Path,
        oracle: bool,
        rules: tuple[RuleSpec, ...],
        snippet: str | None,
    ) -> RunRecord:
        note: dict[str, Any] = {
            "schema": 1,
            "slot": self.slot,
            "project": self.project,
            "stack": str(self.stack_dir),
            "app": variant.app,
            "task": variant.task,
            "variant": variant.code,
            "oracle": oracle,
            "seconds": {},
            "error": None,
            "stop_error": None,
        }
        seconds = note["seconds"]
        stack: Path | None = None
        started = False
        try:
            with _timed(seconds, "prepare"):
                self._clear_slot()
                stack = envs.assemble(variant, self.stack_dir, project=self.project, rules=rules)
                _copy_tests(tests_dir, stack / "tests")
                if oracle:
                    _set_check(stack, snippet)
            started = True
            with _timed(seconds, "start"):
                deadline = time.monotonic() + self.start_timeout_s
                envs.start(stack, timeout_s=self.start_timeout_s)
                envs.wait_ready(stack, timeout_s=max(deadline - time.monotonic(), 0.0))
            with _timed(seconds, "test"):
                result = self._run_tests(stack, out_dir)
            found = None
            if oracle:
                with _timed(seconds, "oracle"):
                    found = self._run_oracle(stack, out_dir)
            with _timed(seconds, "check"):
                envs.check_intact(stack)
            _collect(stack, out_dir)
        except BaseException as exc:
            note["error"] = f"{type(exc).__name__}: {exc}"
            if stack is not None:
                with contextlib.suppress(OSError):  # for the record; must not hide `exc`
                    _collect(stack, out_dir)
            raise
        finally:
            if started:
                with _timed(seconds, "stop"):
                    _save_logs(stack, out_dir)
                    note["stop_error"] = _stop(stack)
            with contextlib.suppress(OSError):
                (out_dir / "backend.json").write_text(
                    json.dumps(note, indent=2) + "\n", encoding="utf-8"
                )
        results = out_dir / "results"
        log = results / "output.log"
        return RunRecord(
            status=result["status"],
            result=result,
            oracle=found,
            log=log.read_text(encoding="utf-8", errors="replace") if log.is_file() else "",
            results_dir=results,
        )

    def _rules(self, variant: Variant) -> tuple[RuleSpec, ...]:
        if variant.rule is None:
            return ()
        rules = tuple(self.rules_for(variant)) if self.rules_for is not None else ()
        if not rules:
            raise ValueError(
                f"{variant.app}/{variant.task} ({variant.code}): a {variant.rule} variant "
                "needs its rules; give the DockerBackend rules_for"
            )
        return rules

    def _snippet(self, variant: Variant) -> str | None:
        """The oracle's snippet from ``oracle_snippet_for``, or None for the stack's own (also
        when ``oracle_snippet_for`` gives None for this variant)."""
        if self.oracle_snippet_for is None:
            return None
        snippet = self.oracle_snippet_for(variant)
        if snippet is None:
            return None
        _require_a_statement(snippet, f"oracle_snippet_for({variant.code})")
        return snippet

    @contextlib.contextmanager
    def _slot_lock(self) -> Iterator[None]:
        """Hold the Compose project, then the slot: a second run on either, from any process,
        waits here. Project names are global to the Docker daemon, so the project's lock lives
        in one directory for every stacks root (``HOH_LOCK_DIR``, else the temp directory)."""
        self.stacks_root.mkdir(parents=True, exist_ok=True)
        locks = Path(os.environ.get("HOH_LOCK_DIR") or tempfile.gettempdir()) / "hoh-compose"
        locks.mkdir(parents=True, exist_ok=True)
        with (
            open(locks / f"{self.project}.lock", "a") as project,
            open(self.stacks_root / f"slot-{self.slot}.lock", "a") as slot,
        ):
            fcntl.flock(project.fileno(), fcntl.LOCK_EX)
            fcntl.flock(slot.fileno(), fcntl.LOCK_EX)
            yield  # closing the files releases the locks

    def _clear_slot(self) -> None:
        """Stop the slot's previous stack, with its own settings, and remove its directory."""
        old = self.stack_dir
        if not (old.exists() or old.is_symlink()):
            return
        if (old / ".env").is_file():
            envs.stop(old, timeout_s=STOP_TIMEOUT_S)
        shutil.rmtree(old)

    def _run_tests(self, stack: Path, out_dir: Path) -> dict[str, Any]:
        proc = envs.compose(stack, *RUN, "runner", timeout_s=self.run_timeout_s)
        _write_console(out_dir / "runner-console.log", proc)
        result = _read_json(stack / "out" / "result.json")
        problem = _problem("runner", "result.json", proc.returncode, result, STATUSES)
        if problem:
            raise EnvFailure(problem)
        return result

    def _run_oracle(self, stack: Path, out_dir: Path) -> dict[str, Any] | None:
        """The shadow oracle's result, or None with the reason in oracle_error.txt."""
        try:
            proc = envs.compose(stack, *RUN, "oracle", timeout_s=self.run_timeout_s)
        except EnvFailure as exc:  # a timeout
            problem = str(exc)
        else:
            _write_console(out_dir / "oracle-console.log", proc)
            found = _read_json(stack / "out" / "oracle.json")
            problem = _problem("oracle", "oracle.json", proc.returncode, found, ORACLE_STATUSES)
            if problem is None:
                return found
        (out_dir / "oracle_error.txt").write_text(problem + "\n", encoding="utf-8")
        return None


def _problem(
    who: str, name: str, code: int, data: dict[str, Any] | None, statuses: Sequence[str]
) -> str | None:
    """Why what the runner or the oracle left cannot be used, or None when it can."""
    if code == UNUSABLE:
        return f"the {who} found the environment unusable: {_detail(data)}"
    if data is None:
        return f"the {who} left no usable {name} (exit code {code})"
    if code != 0:
        return f"the {who} exited with code {code}"
    if data.get("env_ok") is not True:
        return f"{name} reports an unusable environment: {_detail(data)}"
    if data.get("status") not in statuses:
        return f"{name} has no valid status: {data.get('status')!r}"
    return None


def _detail(data: dict[str, Any] | None) -> str:
    return str((data or {}).get("env_error") or "no detail given")


def _read_json(path: Path) -> dict[str, Any] | None:
    """A JSON object from a regular file, or None. A link is not followed."""
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _require_a_statement(snippet: str, where: str) -> None:
    """Refuse a snippet without a statement: the oracle would pass it on any page."""
    try:
        body = ast.parse(textwrap.dedent(snippet)).body
    except SyntaxError:
        return  # the oracle reports it as an error
    if not body:
        raise ValueError(f"{where}: no final assertion to check")


def _set_check(stack: Path, snippet: str | None) -> None:
    """Put the oracle's snippet in the stack's check.py, or keep the one assembly wrote."""
    path = stack / envs.CHECK_FILE
    if snippet is not None:
        path.write_text(snippet, encoding="utf-8")
    _require_a_statement(path.read_text(encoding="utf-8"), f"{path.name} of {stack.name}")


def _copy_tests(tests_dir: Path, dest: Path) -> None:
    """The tests as handed over, links kept as links. A missing directory leaves tests/
    empty, and the runner then reports that nothing was collected."""
    if tests_dir.is_dir():
        shutil.copytree(tests_dir, dest, symlinks=True, dirs_exist_ok=True)


def _collect(stack: Path, out_dir: Path) -> None:
    """Copy what the containers wrote: the known files of out/ into out_dir, the regular
    files of test-results/ into out_dir/results."""
    for name in OUT_FILES:
        path = stack / "out" / name
        if path.is_file() and not path.is_symlink():
            shutil.copy2(path, out_dir / name, follow_symlinks=False)
    _copy_files(stack / "test-results", out_dir / "results")


def _copy_files(src: Path, dest: Path) -> None:
    """Copy the regular files under src to dest, subdirectories kept, links skipped."""
    dest.mkdir(parents=True, exist_ok=True)
    if src.is_symlink() or not src.is_dir():
        return
    for root, _dirs, names in os.walk(src):  # does not descend into linked directories
        for name in names:
            path = Path(root) / name
            if path.is_symlink() or not path.is_file():
                continue
            target = dest / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target, follow_symlinks=False)


def _write_console(path: Path, proc: subprocess.CompletedProcess) -> None:
    path.write_text((proc.stdout or "") + (proc.stderr or ""), encoding="utf-8")


def _save_logs(stack: Path, out_dir: Path) -> None:
    """The stack's logs, for the record. A run is not lost for want of its logs."""
    try:
        proc = envs.compose(stack, "logs", "--no-color", "--timestamps", timeout_s=LOGS_TIMEOUT_S)
        text = proc.stdout or ""
        if proc.returncode != 0:
            text += f"\n[docker compose logs exited with code {proc.returncode}]\n{proc.stderr}"
    except Exception as exc:  # the logs are a record, not a result
        text = f"[docker compose logs failed: {exc}]\n"
    with contextlib.suppress(OSError):
        (out_dir / "environment.log").write_text(text, encoding="utf-8")


def _stop(stack: Path) -> str | None:
    """Stop the stack. Returns the error instead of raising it (module docstring)."""
    try:
        envs.stop(stack, timeout_s=STOP_TIMEOUT_S)
    except Exception as exc:  # noted; the slot's next run stops it again
        return f"{type(exc).__name__}: {exc}"
    return None


@contextlib.contextmanager
def _timed(seconds: dict[str, float], phase: str) -> Iterator[None]:
    begun = time.monotonic()
    try:
        yield
    finally:
        seconds[phase] = round(time.monotonic() - begun, 3)


# --- the runner image -----------------------------------------------------------------


def runner_source_digest(repo_root: Path = REPO_ROOT) -> str:
    """SHA-256 over the runner's Dockerfile and every file of the runner package (caches and
    hidden files such as Finder's .DS_Store left out), path by path: what decides how a test
    is run and judged inside a stack."""
    repo_root = Path(repo_root)
    digest = hashlib.sha256()
    package = repo_root / RUNNER_PACKAGE
    files = [repo_root / DOCKERFILE] + sorted(
        p for p in package.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        and not any(part.startswith(".") for part in p.relative_to(package).parts)
    )
    for path in files:
        digest.update(path.relative_to(repo_root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def runner_image_problem(image_prefix: str = "hoh", repo_root: Path = REPO_ROOT) -> str | None:
    """Why the runner image cannot be used with this checkout, or None when it can.

    The image must carry the label ``build_runner_image`` gives it, with the digest of the
    runner code as it is now: an image built before a change to the runner would run and
    judge tests with the old code.
    """
    image = f"{image_prefix}/{envs.RUNNER_IMAGE}"
    rebuild = "rebuild it: uv run python -m hoh.docker_backend build-runner"
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", "--format",
             '{{ index .Config.Labels "' + RUNNER_LABEL + '" }}', image],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"the runner image {image} could not be inspected ({exc}); is Docker running?"
    if proc.returncode != 0:
        return f"the runner image {image} is not there; {rebuild}"
    label = proc.stdout.strip()
    if label != runner_source_digest(repo_root):
        built = f"code {label[:12]}" if label and label != "<no value>" else "unlabelled code"
        return f"the runner image {image} was built from {built}, not from this checkout; {rebuild}"
    return None


def build_runner_image(
    repo_root: Path = REPO_ROOT, image_prefix: str = "hoh", *, timeout_s: float = 3600.0
) -> subprocess.CompletedProcess:
    """Build docker/runner/Dockerfile under the name every stack gives the runner image
    (``<prefix>/runner:pw<version>``, as ``hoh.envs`` names it). The build context is src/,
    so the repository's .env, build/ and the benchmark never reach the builder. Output
    goes to the terminal. A timeout is an ``EnvFailure``, as for ``hoh.envs.build_images``.
    """
    repo_root = Path(repo_root)
    cmd = [
        "docker",
        "build",
        "--tag",
        f"{image_prefix}/{envs.RUNNER_IMAGE}",
        "--label",
        f"{RUNNER_LABEL}={runner_source_digest(repo_root)}",
        "--file",
        str(repo_root / DOCKERFILE),
        str(repo_root / BUILD_CONTEXT),
    ]
    try:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        raise EnvFailure(f"docker build did not finish within {timeout_s:g} s") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hoh.docker_backend",
        description="Build the image the stacks' runner and oracle services run.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser(
        "build-runner", help=f"build {DOCKERFILE} as <prefix>/{envs.RUNNER_IMAGE}"
    )
    build.add_argument("--image-prefix", default="hoh")
    build.add_argument("--timeout", type=float, default=3600.0, help="seconds")
    args = parser.parse_args(argv)
    return build_runner_image(REPO_ROOT, args.image_prefix, timeout_s=args.timeout).returncode


if __name__ == "__main__":
    sys.exit(main())
