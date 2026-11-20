"""A scripted backend for tests and dry runs, and the loader for real ones."""

from __future__ import annotations

import importlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hoh.backend import Backend, EnvFailure, RunRecord, Variant


class FakeBackend:
    """Plays back a script, one entry per ``run``.

    An entry is ``{"status": "fail", "log": "...", "files": {"output.log": "..."},
    "oracle": {...}}`` or ``{"env_failure": "message"}``. Every call is recorded in
    ``calls`` together with the text of the test files at that moment.
    """

    def __init__(self, script: Sequence[dict] | Path | str, rules: Path | str | None = None):
        if isinstance(script, (str, Path)):
            script = json.loads(Path(script).read_text())
        self.script: list[dict] = list(script)
        self.rules = rules
        """The rules file a configuration gives the backend of a family with rules; kept, not
        applied: the script says what each run shows."""
        self.calls: list[dict[str, Any]] = []
        self._next = 0

    def run(
        self, tests_dir: Path, variant: Variant, out_dir: Path, *, oracle: bool = False
    ) -> RunRecord:
        tests_dir, out_dir = Path(tests_dir), Path(out_dir)
        tests = {
            str(p.relative_to(tests_dir)): p.read_text(errors="replace")
            for p in sorted(tests_dir.rglob("*"))
            if p.is_file()
        }
        self.calls.append(
            {
                "tests_dir": tests_dir,
                "variant": variant,
                "out_dir": out_dir,
                "oracle": oracle,
                "tests": tests,
            }
        )
        if self._next >= len(self.script):
            raise IndexError(
                f"the backend script has {len(self.script)} entries and run() was called "
                f"{self._next + 1} times"
            )
        entry = self.script[self._next]
        self._next += 1
        if "env_failure" in entry:
            raise EnvFailure(entry["env_failure"])
        results = out_dir / "results"
        results.mkdir(parents=True, exist_ok=True)
        for name, text in entry.get("files", {}).items():
            target = results / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        return RunRecord(
            status=entry.get("status", "pass"),
            result=entry.get("result", {}),
            oracle=entry.get("oracle") if oracle else None,
            log=entry.get("log", ""),
            results_dir=results,
        )


def load_backend(spec: dict) -> Backend:
    """Build a backend from ``{"factory": "package.module:callable", "options": {...}}``."""
    module_name, _, attr = spec["factory"].partition(":")
    factory = getattr(importlib.import_module(module_name), attr)
    return factory(**spec.get("options", {}))
