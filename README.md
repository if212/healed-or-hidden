# Healed or Hidden?

[![DOI](https://zenodo.org/badge/1465156449.svg)](https://doi.org/10.5281/zenodo.24391761)

Data and code for the paper *Healed or Hidden? Coding Agents Mask Bugs When Told to
Fix Failing End-to-End Tests*.

A coding agent (Claude Code with Claude Sonnet 5.5) was handed a red Playwright test
and asked to fix it. Sometimes the test was stale (a harmless change to the
application) and sometimes the application had a bug. Each session ran under one of
three instructions: a naive request (N), Playwright's official healer prompt (H), and
a guarded healer prompt (G). Every returned test was labeled by running it again.
This repository holds one record per session of the study's five stages, the analysis
outputs behind the paper's numbers, the prompts, the scenarios and the code that ran
the study.

## What is here

| Path | What it holds |
|---|---|
| `data/sessions.csv` | One row per session (1078: 990 main, 88 exploratory) |
| `data/sessions.jsonl` | Per session: the agent's texts and the lines its returned test changed |
| `analysis/` | The analysis outputs; `numbers.tex` holds the numbers the analysis computes for the paper |
| `docs/analysis-plan.md` | The analysis plan, frozen before the first recorded session |
| `docs/coding/` | The two coders' instructions and codes, their agreement and the adjudication |
| `docs/review/` | The decision on each false heal, read by hand before the data were frozen |
| `docs/neutralization-report.md` | Every rewrite of a benchmark bug script, with the rule that made it |
| `prompts/final/` | The prompts each instruction used, as the agent saw them |
| `prompts/` | The rules that build them from Playwright's healer prompt |
| `scenarios/` | The selected tests, the rename and removal targets and the reworded texts |
| `src/`, `docker/`, `stages/`, `justfile` | The harness, the runner containers and the stage configuration format |
| `third_party/playwright-healer/` | Playwright's healer prompt (Apache-2.0), the source of H and G |

The analysis plan was frozen on 2026-11-15, before the first recorded session, at commit
`10e74daa47a60fade68f80c2f49409f5d6695666` of the study's working repository, which is not
public. `docs/analysis-plan.md` is that file unchanged. Its SHA-256 is
`19f3eb9ae7a9902dff6a33a4a21e7546592c5384194c477e3e8f0f524fb700fa`.

## `data/sessions.csv`

| Column | Meaning |
|---|---|
| `session` | The session, `stage/family/app/test/arm/rep` |
| `stage` | `1a`, `1b`, `2-mixed`, `2-c-assert`, or `explore` (exploratory) |
| `analysis` | `main` for the 990 sessions of the four main stages, which give the paper's results, or `exploratory` for the 88 sessions of `explore` |
| `app`, `test` | The application and the test |
| `family` | `b_oracle` (benchmark bug), `b_remove` (element removed), `c_rename` (label renamed), `c_assert` (text reworded), `mixed` (rename and bug) |
| `arm` | `N`, `H`, `G`; in `explore`, `P` (pressure) and `H_noskip` (H without its skip sentence) |
| `rep` | The repetition, 1 or 2 |
| `split` | `dev` (used to pilot the instructions) or `held-out` (where the decisions are taken) |
| `outcome` | The outcome as the paper names it: false heal, kept red, broken, skipped, correct heal, relaxed, not repaired |
| `label` | The harness's finer label. A false heal is `false_heal_bug_encoded` (the returned test now fails on the clean application), `false_heal_blind` (it passes there too), `false_heal_workaround` (it goes around a removed element) or, in Mixed scenarios, `false_heal` |
| `edit` | How the returned test changed: `unchanged`, `locator_only`, `assertion_changed`, `flow_changed`, `skipped_or_deleted`, `environment` |
| `run_test_calls` | Calls to the `run_test` tool: four counted calls were allowed, and a first call before any edit was not counted |
| `turns`, `cost_usd` | The session's turns and API cost |
| `defect_final`, `defect_comments`, `defect_skip_reasons` | Whether the final message, the added comments, the added skip reasons state an application defect: `asserted`, `hedged` or `none`; empty when there is no such text |
| `could_not_tell` | Whether the final message says the agent could not tell a bug from a stale test (`yes`, `no`) |

The codes are the adjudicated codes of the two coders (`docs/coding/`); the
exploratory sessions were not coded.

## `data/sessions.jsonl`

One JSON object per line, in the order of `sessions.csv`:

- `session`: as in `sessions.csv`;
- `final`: the agent's final message;
- `comments`, `skip_reasons`: the comments and skip reasons the agent added to the test;
- `files`: per test file, `removed` (lines of the original test that the returned test
  no longer has) and `added` (lines the returned test added), each as
  `[line number, text]`. No other line of the test is given: the tests were generated
  from the benchmark's, whose files are not distributed here. A test appears in
  several sessions, so the removed lines of all its sessions together can show much
  of a short test.

## What is not here

- The benchmark ([WebTestPilot-benchmark](https://github.com/code-philia/WebTestPilot-benchmark))
  has no license file, so none of its files is included: not its tests, its bug
  scripts, or the tests generated from them. `just setup` fetches it at the pinned
  commit `6e74114d78d85c4be46c0d472711890561b1ec02`; `just gen` and `just neutralize`
  rebuild the generated tests and the rewritten bug scripts from it.
- The raw session records (transcripts, page snapshots, the full returned tests): they
  contain the generated tests and the applications' pages.
- The fidelity check of the paper's Section IV-E, in which Playwright's own healer
  agent ran 20 sessions (the last row of Table IV): the paper reports its labels, and
  its sessions are not included.
- The study's design document, runbooks, run reports and unit tests. Comments in the
  code and some files under `docs/` refer to them.

## Running it again

The study ran on macOS with Docker, Claude Code 2.1.325 and `claude-sonnet-5-5`
through the API. With [uv](https://docs.astral.sh/uv/) and
[just](https://github.com/casey/just):

```bash
just setup       # the pinned benchmark and the Python dependencies
just gen         # build/tests: the selected tests
just neutralize  # build/payloads: the rewritten bug scripts
just prompts     # build/prompts: the prompts of prompts/final/
```

A stage runs with `python -m hoh.run_stage --stage <name> --config <file>`;
`stages/dry-run.yaml` shows the configuration format in full. `just analyze` and
`just export` read the stages' raw records, which are not public. From them,
`just analyze` writes the files of `analysis/` to `build/analysis/` and
`paper/generated/`, and `just export` writes `data/`.

## Citing

`CITATION.cff` gives the citation.

## License

The code, the prompts, the scenarios and the stage configurations are under the MIT
License (`LICENSE`). `third_party/playwright-healer/` is Playwright's, under the Apache
License 2.0 (its `LICENSE` and `NOTICE`), and so are the parts of `prompts/final/` and
of the edit files in `prompts/` taken from it; the `NOTICE` names the modified files.
The data in `data/`, `analysis/` and `docs/` are under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The fragments of the
benchmark's bug scripts that `docs/neutralization-report.md` quotes are the
benchmark's, and this license does not cover them.
