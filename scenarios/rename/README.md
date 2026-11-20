# Rename and removal targets

The targets of stage 1b: per test, the control that C-rename renames and B-remove removes.
They were chosen from the test sources and checked against page snapshots of every step of
every test (checklist and criteria in `notes.md`, section 7). `rules.yaml` holds the reviewed
targets that `stages/validate-1b.yaml` and `just fixes` read.

| File | What it holds |
|---|---|
| `targets.csv` | Every locator label in the 54 chosen tests, whether it is interface copy or user data, and whether it can be a rename target |
| `occurrences.csv` | Every place each label is used, by step and field |
| `assignment.csv` | One proposed target per test, chosen to maximise the number of distinct labels, less the labels the snapshots rule out (`notes.md`, section 2) |
| `rules.yaml` | The reviewed targets as a rules file `hoh.rules` reads (link and button targets only), for `stages/validate-1b.yaml` and `just fixes` |
| `cassert_candidates.csv` | Interface text inside expected values, for the later C-assert family |
| `notes.md` | Coverage, judgment calls to double-check, and the validation checklist |

The rules in design doc section 5.3 apply: interface copy only, meaning kept,
the new wording must not contain the old one (Playwright's `name=` is a
case-insensitive substring match), no collision with other labels, and one
replacement per label across all tests of an app.
