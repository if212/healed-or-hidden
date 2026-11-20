"""Assemble each arm's system prompt and user message from committed files.

This module holds no prompt text. Everything an agent under test is told comes
from two committed places:

- `third_party/playwright-healer/`: the upstream healer definition, pinned byte
  for byte (see SOURCE.md there).
- `prompts/`: the task lines, the operating note shared by every arm, and the
  edit files that derive each arm's system prompt.

An edit file is a YAML list of `{old, new, why}`. `old` must occur exactly once
in the text it is applied to, so an edit that no longer matches stops the build
instead of being skipped. See design doc sections 5.5 and 5.6.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from hoh.benchmark import REPO_ROOT

PROMPTS_DIR = REPO_ROOT / "prompts"
UPSTREAM_FILE = REPO_ROOT / "third_party" / "playwright-healer" / "playwright-test-healer.agent.md"

ARMS: tuple[str, ...] = ("N", "H", "G", "P", "H_noskip", "H_intent")

# The file that holds the task line of each arm's user message.
_TASK_FILE = {
    "N": "user_N.txt",
    "P": "user_P.txt",
    "H": "user_H.txt",
    "G": "user_H.txt",
    "H_noskip": "user_H.txt",
    "H_intent": "user_H.txt",
}

# The edit files that turn the upstream body into each arm's system prompt, in
# the order they are applied. An arm that is not listed has no system prompt.
# H_intent has H's prompt: it differs from H in the test file it is given.
_H_EDITS = ("h_adaptation.yaml",)
_SYSTEM_EDITS: dict[str, tuple[str, ...]] = {
    "H": _H_EDITS,
    "G": (*_H_EDITS, "g_from_h.yaml"),
    "H_noskip": (*_H_EDITS, "h_noskip_from_h.yaml"),
    "H_intent": _H_EDITS,
}

_NOTE_FILE = "operating_note.txt"
_MAX_RUNS_SLOT = "{max_runs}"


class EditMismatch(ValueError):
    """An edit's `old` text does not occur exactly once in the text it is applied to."""


def apply_edits(text: str, edits: list[dict]) -> str:
    """Replace each edit's `old` with its `new`, in order.

    Every `old` must occur exactly once in the text as it stands when its turn
    comes. Anything else raises EditMismatch: a silent skip would hand the agent
    a prompt that nobody reviewed.
    """
    for number, edit in enumerate(edits, start=1):
        old, new = edit["old"], edit["new"]
        if not old:
            raise EditMismatch(f"edit {number}: `old` is empty")
        found = text.count(old)
        if found != 1:
            raise EditMismatch(
                f"edit {number}: `old` must occur exactly once but occurs {found} times: {old!r}"
            )
        text = text.replace(old, new)
    return text


def upstream_body() -> str:
    """The pinned healer definition without its YAML front matter.

    The file is split the way Playwright's own `parseAgentSpec` splits it: the
    front matter runs from the first line, `---`, to the next `---` line, and
    the instructions are the rest with surrounding whitespace trimmed.
    """
    lines = UPSTREAM_FILE.read_text(encoding="utf-8").split("\n")
    if lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError(f"{UPSTREAM_FILE}: expected YAML front matter between two `---` lines")
    return "\n".join(lines[lines.index("---", 1) + 1 :]).strip()


def system_prompt(arm: str) -> str | None:
    """The text appended to the system prompt for `arm`, or None if it gets none."""
    _check_arm(arm)
    if arm not in _SYSTEM_EDITS:
        return None
    text = upstream_body()
    for name in _SYSTEM_EDITS[arm]:
        text = _apply(text, _load_edits(name), name)
    return text


def user_message(arm: str, max_runs: int) -> str:
    """The task line of `arm`, a blank line, and the operating note shared by every arm."""
    _check_arm(arm)
    if max_runs < 1:
        raise ValueError(f"max_runs must be at least 1, got {max_runs}")
    fill_in = [{"old": _MAX_RUNS_SLOT, "new": str(max_runs)}]
    note = _apply(_read(_NOTE_FILE), fill_in, _NOTE_FILE)
    return f"{_read(_TASK_FILE[arm])}\n\n{note}"


def write_prompts(out: Path, max_runs: int) -> list[Path]:
    """Write `<arm>.system.md` and `<arm>.user.txt` for every arm into `out`.

    Each file holds exactly the text the functions above return, with no newline
    added at the end. An arm without a system prompt gets no `.system.md`, and a
    leftover one from an earlier build is removed. Every arm is assembled before
    anything is written, so a failing edit leaves `out` as it was instead of
    half new and half old.
    """
    texts: dict[Path, str | None] = {}
    for arm in ARMS:
        texts[out / f"{arm}.system.md"] = system_prompt(arm)
        texts[out / f"{arm}.user.txt"] = user_message(arm, max_runs)

    out.mkdir(parents=True, exist_ok=True)
    written = []
    for path, text in texts.items():
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text, encoding="utf-8", newline="\n")
            written.append(path)
    return written


def _check_arm(arm: str) -> None:
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {', '.join(ARMS)}")


def _read(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def _load_edits(name: str) -> list[dict]:
    return yaml.safe_load((PROMPTS_DIR / name).read_text(encoding="utf-8"))


def _apply(text: str, edits: list[dict], source: str) -> str:
    try:
        return apply_edits(text, edits)
    except EditMismatch as mismatch:
        raise EditMismatch(f"{source}: {mismatch}") from None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-runs", type=int, required=True)
    args = parser.parse_args(argv)

    for path in write_prompts(args.out, args.max_runs):
        print(path)


if __name__ == "__main__":
    main()
