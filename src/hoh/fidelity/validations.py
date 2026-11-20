"""Stages 1a and 1b's validations, as the fidelity check reads them.

A validation directory (``results/validation`` for stage 1a, ``results/validation-1b`` for 1b)
holds two files. ``validation.json`` lists each scenario's variants with the check each ran
and, where the test turned red, the signature of the failure (its step, line, exception and
call). ``baseline.json`` holds what each admitted scenario rests on (the test's and the
reference fix's SHA-256, the rename target, the environment, the runner). The fidelity check
reads the line where each Python test turned red from the signatures, maps it through the
converter's line map, and ties the files it races to the baselines.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Validation:
    path: Path
    scenarios: dict[str, Any]
    """``validation.json``'s scenarios, by key (``b_oracle/bookstack/update_book``)."""
    baseline: dict[str, Any]
    """``baseline.json`` as written."""


def load(directory: Path | str) -> Validation:
    directory = Path(directory)
    data = json.loads((directory / "validation.json").read_text(encoding="utf-8"))
    baseline = json.loads((directory / "baseline.json").read_text(encoding="utf-8"))
    return Validation(directory, data["scenarios"], baseline)


def scenario(validations: Sequence[Validation],
             key: str) -> tuple[Validation, dict[str, Any], dict[str, Any]]:
    """The validation that holds ``key``, its entry there and its baseline. Raises ValueError
    when no validation or two hold it, or when its validation has no baseline for it."""
    holders = [v for v in validations if key in v.scenarios]
    if not holders:
        raise ValueError(f"no validation holds {key}")
    if len(holders) > 1:
        raise ValueError(f"two validations hold {key}: "
                         + ", ".join(str(v.path) for v in holders))
    [found] = holders
    baseline = found.baseline.get("scenarios", {}).get(key)
    if baseline is None:
        raise ValueError(f"{found.path} has no baseline for {key}")
    return found, found.scenarios[key], baseline


def red_line(validations: Sequence[Validation], key: str, check: str) -> int:
    """The Python line where ``key``'s test turned red on ``check`` (``A-M``, ``A-C``,
    ``fix.A-C-M``). Raises ValueError when the check did not run there, or was not red."""
    found, entry, _ = scenario(validations, key)
    ran = [v for v in entry.get("variants", {}).values() if v.get("check") == check]
    if len(ran) != 1:
        raise ValueError(f"{found.path}: {key} has no check {check!r}" if not ran else
                         f"{found.path}: {key} ran check {check!r} {len(ran)} times")
    signature = ran[0].get("signature") or {}
    if signature.get("lineno") is None:
        raise ValueError(f"{found.path}: {key}'s check {check!r} has no red line")
    return int(signature["lineno"])
