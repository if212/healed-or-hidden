# Evidence level of each bug scenario (pre-registered covariate)

Design doc section 5.3. Each of the 44 bug scenarios in the study (BookStack 20,
Invoice Ninja 24; the 10 Indico rows of the template are not coded) is coded
before any recorded run as one of three levels by two different language
models working independently. The author adjudicates every disagreement, and
the adjudicated level is the one analysed. Agreement between the two models
and the number of levels the adjudication changed are reported, and primary
comparison 1 is also reported per level.

## The three levels

| Level | Code | Meaning |
|---|---|---|
| self-evidencing | `self` | The bug contradicts something the test itself typed, created or did. A reader holding only the test file can see that the page is wrong. |
| fixture-dependent | `fixture` | The bug changes data that existed before the test started and that the test never touched. From the test alone, a regression and a changed fixture look the same. |
| control-removed | `control` | A control or heading that the test needs is removed. |

If a scenario fits none or two of them, pick the closest and say why in `note`.

## How to code

1. Build one blind folder per coder, outside the repository. Each holds a copy of
   the template's BookStack and Invoice Ninja rows (`coder.csv`) and, for each
   row, `cases/<app>__<task>/test.py` (the generated test,
   `build/tests/<app>/test_<task>.py`) and `cases/<app>__<task>/bug.js` (the
   neutralized bug script, `build/payloads/<app>/<task>.js`). Nothing else: not
   the benchmark's YAML, not the original bug scripts, not run results.

   ```bash
   bash <<'EOF'
   REPO=~/research-healed-or-hidden
   for C in coder-a coder-b; do
     D=~/hoh-coding/$C; mkdir -p "$D/cases"
     head -1 "$REPO/scenarios/evidence-levels/coder-template.csv" > "$D/coder.csv"
     grep -E '^(bookstack|invoiceninja),' "$REPO/scenarios/evidence-levels/coder-template.csv" >> "$D/coder.csv"
     tail -n +2 "$D/coder.csv" | while IFS=, read -r app task rest; do
       mkdir -p "$D/cases/${app}__${task}"
       cp "$REPO/build/tests/$app/test_$task.py" "$D/cases/${app}__${task}/test.py"
       cp "$REPO/build/payloads/$app/$task.js" "$D/cases/${app}__${task}/bug.js"
     done
   done
   EOF
   ```

2. Start one session per coder in its own folder, each with a different model
   (one from another vendor when available), and give it `coder-prompt.md`. A
   coder never sees the other's folder.
3. Copy the two filled files into this directory as `coder-1.csv` and
   `coder-2.csv`. Coder 1 is GPT-6 Astra; coder 2 is Claude Fable 5.2.
4. The author lists the rows where the two levels differ and decides each one,
   before reading any session transcript. `adjudicated.csv` has the template's
   columns: the decided `level`, and in `note` which coder it follows or why.
5. Commit the two coder files and `adjudicated.csv` before the first recorded run
   of stage 1a, with a line in `docs/ai-usage.md`. The commit is the freeze.

## A sealed third opinion

An AI assistant (Claude) also read the 54 scenarios and proposed a level for
each, before the two coders started and before any run. It worked from the
benchmark's YAML and the original bug scripts, so its proposal is a comparison
only, not a third coder. Its proposal and the fact
sheet it worked from are kept outside the repository until both coder files
are committed, so that no coder sees them first. Their SHA-256 hashes,
recorded here on 2026-11-14, show that the files added later are the ones
written now:

| File | SHA-256 |
|---|---|
| `facts.csv` | `f5ed917e7584cccff1899e6fd70c3ecd1aa62342bba6c9dbdac9046070141472` |
| `proposed_levels.csv` | `acbbe9a455bb4f9289ada58cdc1ad4ca739833b6ac35820d94be1ee1ec11be8a` |

Both files are in this directory, added after the coder files and the
adjudication were committed; their hashes match the table. The paper reports
the agreement between the two model coders and the adjudicated levels; the
sealed proposal is a comparison only.
