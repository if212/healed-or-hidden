"""The TypeScript runner inside the study's stacks, for the fidelity check (design doc 5.6).

A stack that ``hoh.envs`` assembles runs the Python runner as its ``runner`` service.
``TsBackend`` is the study's ``DockerBackend`` with that service switched to the TypeScript
runner: the image ``hoh/runner-ts:pw1.65.1``, ``python3 -m pwrun.entry`` in place of
``hoh_runner.entry``, and the target named as the TypeScript reporter names it
(``tests/<task>.spec.ts::test_<task>``). The mounts and the settings stay as ``hoh.envs``
wrote them, and so does the shadow oracle, the study's own Python service. ``pwrun.entry``
writes the same ``result.json`` as the Python runner, so the backend reads a TypeScript run
as it reads a Python one; it also keeps the reporter's ``pw-report.json``.

The image is built from docker/runner-ts/Dockerfile on the Python runner image:

    uv run python -m hoh.fidelity.stack build-runner-ts

Its label holds ``source_digest()``, and ``image_problem()`` refuses an image built from
other code, as ``hoh.docker_backend.runner_image_problem`` does for the Python runner. The build
starts only when no stage or validation of the study runs (``hoh.fidelity.guard``).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from hoh import docker_backend, envs
from hoh.backend import EnvFailure
from hoh.benchmark import REPO_ROOT
from hoh.docker_backend import DockerBackend
from hoh.fidelity import guard

RUNNER_TS_IMAGE = "runner-ts:pw1.65.1"
IMAGE_DIR = Path("docker") / "runner-ts"
DOCKERFILE = IMAGE_DIR / "Dockerfile"
IGNORE_FILE = IMAGE_DIR / "Dockerfile.dockerignore"
LABEL = "hoh.runner-ts.source"
"""Image label holding ``source_digest`` of the code the image was built from."""
REPORT_NAME = "pw-report.json"
"""What the TypeScript reporter writes next to ``result.json`` (``pwrun.entry.REPORT_NAME``)."""
#: The packages the image copies, relative to the repository. The rest of the code in it is
#: the Python runner's, which ``hoh.docker_backend.runner_source_digest`` covers.
PACKAGES = (Path("src") / "pwfix", Path("src") / "pwrun")
#: What the image leaves out of them (docker/runner-ts/Dockerfile.dockerignore).
# Besides caches and hidden files, the image's ignore file keeps out the fixture's local install
# and its unit tests, there only. The digest leaves out the same files, and nothing else.
LEFT_OUT = (Path("src") / "pwfix" / "node_modules", Path("src") / "pwfix" / "test")
REBUILD = "rebuild it: uv run python -m hoh.fidelity.stack build-runner-ts"

_PYTHON_TARGET = re.compile(r"tests/test_([A-Za-z0-9_]+)\.py::test_\1")


def target_for(task: str) -> str:
    """The node id of a port's test: the file ``<task>.spec.ts``, the title ``test_<task>``."""
    return f"tests/{task}.spec.ts::test_{task}"


def use_ts_runner(stack_dir: Path) -> None:
    """Make the stack's ``runner`` service the TypeScript runner (module docstring).

    Raises ``ValueError`` when the service is not the one ``hoh.envs`` writes.
    """
    path = Path(stack_dir) / envs.OVERRIDE_FILE
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    header = "".join(line for line in lines[: _comment_lines(lines)])
    data = yaml.safe_load(text)
    runner = data["services"]["runner"]
    command = list(runner.get("command") or [])
    target_at = command.index("--target") + 1 if "--target" in command else None
    found = _PYTHON_TARGET.fullmatch(command[target_at]) if target_at is not None else None
    if command[:3] != ["python3", "-m", "hoh_runner.entry"] or found is None:
        raise ValueError(f"{path}: the runner service is not the runner hoh.envs writes")
    command[2] = "pwrun.entry"
    command[target_at] = target_for(found.group(1))
    prefix = str(runner["image"]).rpartition("/")[0]
    runner["image"] = f"{prefix}/{RUNNER_TS_IMAGE}" if prefix else RUNNER_TS_IMAGE
    runner["command"] = command
    note = "# hoh.fidelity.stack made `runner` the TypeScript runner (pwrun.entry).\n"
    path.write_text(
        header + note + yaml.safe_dump(data, sort_keys=False, default_flow_style=False,
                                       width=1000),
        encoding="utf-8",
    )


def _comment_lines(lines: list[str]) -> int:
    count = 0
    while count < len(lines) and lines[count].startswith("#"):
        count += 1
    return count


class TsBackend(DockerBackend):
    """``DockerBackend`` with the TypeScript runner as the stack's ``runner`` service."""

    def _run_tests(self, stack: Path, out_dir: Path) -> dict[str, Any]:
        use_ts_runner(stack)
        try:
            return super()._run_tests(stack, out_dir)
        finally:
            report = stack / "out" / REPORT_NAME
            if report.is_file() and not report.is_symlink():
                shutil.copy2(report, out_dir / REPORT_NAME, follow_symlinks=False)


# --- the image --------------------------------------------------------------------------


def _left_out(relative: Path) -> bool:
    """``relative`` to the repository: whether the image's ignore file keeps it out."""
    return (any(p == "__pycache__" or p.startswith(".") for p in relative.parts)
            or any(relative.is_relative_to(prefix) for prefix in LEFT_OUT))


def source_digest(repo_root: Path = REPO_ROOT) -> str:
    """SHA-256 over the Python runner's digest (the base image), every file of
    ``docker/runner-ts/`` (the Dockerfile, its ignore file and anything added there), and every
    file of the packages the image copies (``src/pwfix`` with its lock file, ``src/pwrun``), path
    by path; left out is what the image's ignore file keeps out (``LEFT_OUT``, caches and hidden
    files)."""
    repo_root = Path(repo_root)
    digest = hashlib.sha256(docker_backend.runner_source_digest(repo_root).encode())
    files: list[Path] = []
    for root in (repo_root / IMAGE_DIR, *(repo_root / package for package in PACKAGES)):
        files += sorted(
            p for p in root.rglob("*")
            if p.is_file() and not _left_out(p.relative_to(repo_root))
        )
    for path in files:
        digest.update(path.relative_to(repo_root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def image_problem(image_prefix: str = "hoh", repo_root: Path = REPO_ROOT) -> str | None:
    """Why the TypeScript runner image cannot be used with this checkout, or None."""
    image = f"{image_prefix}/{RUNNER_TS_IMAGE}"
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", "--format",
             '{{ index .Config.Labels "' + LABEL + '" }}', image],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"the runner image {image} could not be inspected ({exc}); is Docker running?"
    if proc.returncode != 0:
        return f"the runner image {image} is not there; {REBUILD}"
    label = proc.stdout.strip()
    if label != source_digest(repo_root):
        built = f"code {label[:12]}" if label and label != "<no value>" else "unlabelled code"
        return f"the runner image {image} was built from {built}, not from this checkout; {REBUILD}"
    return None


def build_image(
    repo_root: Path = REPO_ROOT, image_prefix: str = "hoh", *, timeout_s: float = 3600.0
) -> subprocess.CompletedProcess:
    """Build docker/runner-ts/Dockerfile as ``<prefix>/runner-ts:pw1.65.1`` on the Python
    runner image, which must be current. Output goes to the terminal."""
    repo_root = Path(repo_root)
    problem = docker_backend.runner_image_problem(image_prefix, repo_root)
    if problem:
        raise EnvFailure(f"the TypeScript runner is built on the Python runner image: {problem}")
    cmd = [
        "docker",
        "build",
        "--tag",
        f"{image_prefix}/{RUNNER_TS_IMAGE}",
        "--label",
        f"{LABEL}={source_digest(repo_root)}",
        "--build-arg",
        f"BASE={image_prefix}/{envs.RUNNER_IMAGE}",
        "--file",
        str(repo_root / DOCKERFILE),
        str(repo_root / docker_backend.BUILD_CONTEXT),
    ]
    # The ignore file next to the Dockerfile is BuildKit's.
    env = {**os.environ, "DOCKER_BUILDKIT": "1"}
    try:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, timeout=timeout_s, check=False,
                              env=env)
    except subprocess.TimeoutExpired:
        raise EnvFailure(f"docker build did not finish within {timeout_s:g} s") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hoh.fidelity.stack",
        description="Build the TypeScript runner image of the fidelity check.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser(
        "build-runner-ts", help=f"build {DOCKERFILE} as <prefix>/{RUNNER_TS_IMAGE}"
    )
    build.add_argument("--image-prefix", default="hoh")
    build.add_argument("--timeout", type=float, default=3600.0, help="seconds")
    args = parser.parse_args(argv)
    # Only when no stage or validation of the study runs (hoh.fidelity.guard).
    try:
        with guard.hold(args.command):
            return build_image(REPO_ROOT, args.image_prefix, timeout_s=args.timeout).returncode
    except guard.Refused as exc:
        print(f"not started: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
