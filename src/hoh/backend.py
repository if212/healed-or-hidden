"""What one run of a test is, and the interface of whatever performs it.

A *variant* is one state of the application: clean (A), with the benchmark bug
(A+M), with a label renamed (A+C), with that control removed (A+R), or renamed
and with the bug (A+C+M). A *backend* brings up a fresh environment for a
variant, runs a directory of tests against it inside the runner, and tears the
environment down. The Docker backend does this with real containers; tests use
a scripted backend or a toy application.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

Rule = Literal["rename", "remove"]
RULES: tuple[str, ...] = ("rename", "remove")
STATUSES: tuple[str, ...] = ("pass", "fail", "not_executed", "broken")


@dataclass(frozen=True)
class Variant:
    """One state of one application for one task."""

    app: str
    task: str
    bug: bool = False
    rule: Rule | None = None

    def __post_init__(self) -> None:
        if self.rule is not None and self.rule not in RULES:
            raise ValueError(f"rule must be one of {RULES} or None, got {self.rule!r}")
        if self.rule == "remove" and self.bug:
            raise ValueError("the study has no variant with both a removed control and the bug")

    @property
    def code(self) -> str:
        """The design doc's name for the variant: A, A+M, A+C, A+R or A+C+M."""
        parts = ["A"]
        if self.rule == "rename":
            parts.append("C")
        if self.rule == "remove":
            parts.append("R")
        if self.bug:
            parts.append("M")
        return "+".join(parts)


class EnvFailure(RuntimeError):
    """The environment did not come up or was not usable.

    A run that ends this way says nothing about the test. It is retried and
    never judged (CLAUDE.md, hard rule 9).
    """


@dataclass(frozen=True)
class RunRecord:
    """The outcome of running a directory of tests once against one variant."""

    status: str
    """pass, fail, not_executed or broken (the values of ``hoh.labels.Run``)."""
    result: dict[str, Any]
    """The runner's ``result.json``."""
    oracle: dict[str, Any] | None
    """The shadow oracle's ``oracle.json``, when the run asked for it."""
    log: str
    """The test run's console output: what an agent is shown."""
    results_dir: Path
    """Directory with the files that go into the workspace's ``test-results/``."""

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {self.status!r}")


class Backend(Protocol):
    """Runs tests against a fresh environment."""

    def run(
        self, tests_dir: Path, variant: Variant, out_dir: Path, *, oracle: bool = False
    ) -> RunRecord:
        """Run every test under ``tests_dir`` once against a fresh ``variant``.

        Everything the run produces goes under ``out_dir``, which is outside
        the agent's workspace. With ``oracle=True`` the shadow oracle runs after
        the tests, on the page as the tests left it. Raises ``EnvFailure`` when
        the environment could not be used.
        """
        ...
