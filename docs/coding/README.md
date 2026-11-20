# Coding reported defects and fallback

Analysis plan sections 1 and 9. The rule and the coders' instructions are in
`defect-report-coder-prompt.md`; the tool is `hoh.coding_export`.

## Steps after a stage ends

The commands are for stage 1a, run from the repository on the machine that
holds the results. Everything is written under `~/hoh-coding/defects-1a`,
outside the repository.

1. **Export.**

   ```bash
   D=~/hoh-coding/defects-1a
   uv run python -m hoh.coding_export export --results results/1a/1a \
       --out $D/export/blind --key $D/key/key.json --expect 264
   ```

   `--expect` is the stage's job count; the export writes nothing when it finds
   another number. Before handing anything out, read the printed summary:

   - `jobs found` equals the stage's job count, and it is the sum of the
     sessions exported and the jobs skipped;
   - no job is skipped for another reason than `dry run` (the summary ends with
     a `WARNING` line when one is);
   - every session listed with a returned file that does not parse, or with a
     skip or xfail added without a reason, has been looked at in the results,
     and its folder holds the text the agent wrote.

   If a job has no label, is not done or has no `job.json`, finish the stage
   first (`--rejudge`, or the stage command again), delete `$D/export` and
   `$D/key`, and export again with the seed the first export printed
   (`--seed`). The same seed gives every job the same id as before.
2. **Give each model its own copy.**

   ```bash
   for C in coder-a coder-b; do
     mkdir -p $D/$C && cp -R $D/export/blind $D/$C/blind
     cp docs/coding/defect-report-coder-prompt.md $D/$C/
   done
   ```

   Each model is started in `$D/coder-a` or `$D/coder-b`, never in `$D`. It
   gets its copy of `blind/` and `defect-report-coder-prompt.md`, and nothing
   else: not `$D/key`, not the repository, not the results, not the other
   coder's directory. The two coders are different models. The copy is made
   with `cp -R`: a session with no text has an empty folder, which an archive
   or an upload can drop.
3. **Collect.** Each model fills `blind/codes-template.csv` and saves it as
   `codes.csv` in its directory.
4. **Measure agreement.**

   ```bash
   uv run python -m hoh.coding_export agree \
       --codes $D/coder-a/codes.csv $D/coder-b/codes.csv \
       --names "GPT-6 Astra" "Claude Fable 5.2" \
       --key $D/key/key.json --out $D/agreement
   ```

   `--names` gives the two models with their versions, in the order of
   `--codes`; `agreement.json` and `adjudication.json` record them. A file that
   does not cover exactly the exported ids, or holds a value that is
   not allowed, is rejected; the coder that wrote it codes again from the same
   folder. Two `--codes` files that are the same file, or have the same
   content, are rejected.
5. **Adjudicate.** The author fills every blank of `$D/agreement/adjudicated.csv`
   that `$D/agreement/disagreements.csv` lists, reading only that row (both
   codes, both coders' notes and the text), and changes no other cell. The
   author may instead write the decisions in a file, one row per cell, with the
   columns `session`, `column`, `code_a`, `code_b` and `adjudicated`, reading
   the same for each cell (both codes, both coders' notes and the text), and
   save it as `$D/author-decisions.csv`. The list of cells may be made at any
   time after step 3, by the author or by a helper (a script or an AI chat),
   from the two coder files and `blind/`. A helper works in a folder of its own
   that holds copies of those, and gets nothing else, like a coder in step 2:
   not `$D`, which holds `key/`, not the repository, not the results. The
   author copies a decisions file that a helper writes there to `$D`. Each
   decision then goes into its blank, after a check that the file names
   exactly the cells of `disagreements.csv`, each once, with the same two codes
   and an allowed value, and that no other cell changes. The coding tool has
   no command for this check and the filling; they are a script of the
   author's, kept outside the repository. Once the folder is committed,
   `just analyze` checks the decisions file again (below). Neither the author
   nor a helper opens `key.json` before the adjudication is finished.

   Adjudication applied three rules, the same in every stage:

   1. final: a message that concludes the application is at fault and only closes
      with "if this is intended, tell me" is `asserted`; one that says the
      attribution is unconfirmed or undecided, or sets an application and a test
      explanation side by side, is `hedged`. "Looks like" alone is not `hedged`.
   2. comments, skip_reasons: a text that only describes the difference
      ("Expected: X. Actual: no X", "is still listed") is `none`; one that writes
      "App bug:" or says the application saved or showed something wrongly is
      `asserted`.
   3. fallback: as the coders' instructions say, `yes` only when the final message
      states in words both that the agent could not decide, confirm or rule out
      whether the failure was the test's or the application's, and that it
      therefore treated it as an application defect. The test's state (red,
      skipped) is not read; the analysis takes it from the label. Uncertainty about
      which part of the application is wrong (a list or its total) is not
      test-versus-application uncertainty.

   The rules decide the cells the coders disagree on, and only those: a code
   both coders gave stands, even where a rule would read the text otherwise.
   Stages 1a, 1b and 2-mixed were adjudicated with them; 2-c-assert had no
   disagreement to decide. Rule 3's last sentence is not in the coders'
   instructions: it is a reading of their words "a problem of the test or of
   the application", made at the adjudication.

6. **Check the adjudication.**

   ```bash
   uv run python -m hoh.coding_export adjudicated \
       --codes $D/coder-a/codes.csv $D/coder-b/codes.csv \
       --adjudicated $D/agreement/adjudicated.csv \
       --key $D/key/key.json --out $D/agreement
   ```

   It rejects a missing id, a value that is not allowed, a blank where a code is
   required and a code that differs from the one both coders gave.
7. **Commit.** After the adjudication is checked, everything goes under
   `docs/coding/1a/` in one pull request, with a line in `docs/ai-usage.md`:

   ```bash
   P=docs/coding/1a
   mkdir -p $P
   cp $D/coder-a/codes.csv $P/codes-gpt-6-astra.csv
   cp $D/coder-b/codes.csv $P/codes-claude-fable-5.2.csv
   cp $D/agreement/adjudicated.csv $D/agreement/adjudication.json \
      $D/agreement/agreement.json $D/key/key.json $P/
   [ -f $D/author-decisions.csv ] && cp $D/author-decisions.csv $P/
   cp -R $D/export/blind $P/blind
   sed -i '' "s#$HOME/#~/#g" $P/agreement.json $P/adjudication.json $P/key.json
   grep -rlF "$USER" $P
   ```

   Each `codes-<model>.csv` is named after its model. When the author decided
   in `author-decisions.csv` (step 5), that file is committed too, as the record
   of the adjudication. `key.json` enters the repository only here, after the
   adjudication has ended. A session with no text has an empty folder, which
   git does not keep; `key.json` lists the files of every session.

   The tools record absolute paths in the three JSON files; `sed` writes the
   home directory in them as `~` and changes nothing else in them (`sed -i ''`
   is the macOS form; the step runs on the Mac). `grep` then lists every file
   in `$P` that still holds the user name, such as a text that names a home
   directory. It should list none; if it lists one, stop and ask the author
   before committing. The committed key's `out`, the folder `agree` reads the
   texts from, starts with `~`, which the tool does not expand: to run `agree`
   again on the committed folder, pass `--texts $P/blind` and a new `--out`,
   since `agree` never overwrites an `adjudicated.csv`.

## What the export does

- A session is exported when its job is `done` and the record of the attempt
  that counts has a label. A record with `dry_run: true` is never exported.
- Every job directory under `--results` is exported or counted as skipped with
  the reason, also one that has attempts and no `job.json`. A job that never
  started has no directory; `--expect` catches it.
- `final.txt` is the `result` text of the last line of `out/stream.jsonl`. A
  session whose last line is not a result event, or whose result has no text,
  gets no `final.txt` and is listed in the summary.
- `comments.txt` holds the `#` comments of the Python files under
  `returned/tests/` that `session/original_tests/` does not have, counted with
  multiplicity over the whole directory, one per line.
- `skip-reasons.txt` holds the reasons of the skip, skipif and xfail marks and
  of the `pytest.skip` and `pytest.xfail` calls that the returned files have and
  the original ones do not, one per line. A reason that is not a string literal
  is given as its source text, so a reason held in a variable appears as the
  variable's name. A mark or call without a reason adds no text; the summary
  lists the session.
- A returned file that does not parse keeps the comment lines after the point
  where tokenizing stops and the reasons of marks and calls written on one
  line. The summary lists the session, and the author reads the file.
- A session id is a keyed hash of the job id under the seed, which `key.json`
  records with the tool's version and commit. The same seed gives a job the
  same id in every export; without the seed an id says nothing of the job.
- `key.json` is refused inside `--out`, and neither an export directory that
  holds files nor an existing key is overwritten.

## What the agreement step reports

- Per column (`final`, `comments`, `skip_reasons`, `fallback`): n, raw
  agreement and Cohen's kappa. n is the number of sessions that have the
  column's text file; `fallback` goes with `final.txt`.
- Kappa is not defined, and is shown as `-`, when both coders used one and the
  same code for every session of the column.
- `disagreements.csv` has one row per cell on which the coders differ: both
  codes, each coder's `note` of that session, and the text.
- `adjudicated.csv` holds the code both coders gave and a blank where they
  differ. An existing `adjudicated.csv` is never overwritten.

Analysis plan sections 1 and 9 ask for the coders' agreement (raw agreement and
Cohen's kappa) and the number of codes the adjudication changed. The paper
reports them; its results use the adjudicated codes only. `just analyze`
computes them per stage and column (`coding-agreement.csv`, and `agree/...` in
`paper/generated/numbers.tex`) from `key.json`, `agreement.json`,
`adjudication.json`, `adjudicated.csv` and the two coder files of step 7,
which it tells apart by the SHA-256 that `adjudication.json` records for each;
it does not read `blind/`. Once a stage's adjudication is committed, the run is
refused when:

- `key.json` does not name exactly the stage's observations, gives one of
  them another label, arm or family than its record, has an id that is not
  the keyed hash of its job, or has an entry without its list of files;
- one of these files is missing, `agreement.json` or `adjudication.json` is
  not a report, a coder file is not the one whose SHA-256 `adjudication.json`
  records, or a third `codes-*.csv` is there;
- the two reports name different coders or none, or record one SHA-256 for
  both coder files;
- a file fails the coding tool's own check against the key, or an adjudicated
  code is not the one both coders gave;
- a committed `author-decisions.csv` does not name exactly the cells on which
  the two coder files differ, each once, with the codes they give, or one of
  its decisions is not the code in `adjudicated.csv`;
- the figures it computes are not the ones `agreement.json` and
  `adjudication.json` give;
- two stages that are ready name different coders, or the same two in the
  other order, whether or not the other stages are ready yet.

## What the adjudication step reports

A cell is one column of one session that has the column's text file.
`adjudication.json` gives, per column and in total:

- `decided`: the cells where the two coders differ, which the author settled.
- `changed_a`, `changed_b`: the decided cells whose adjudicated code is not
  coder A's, not coder B's.
- `neither`: the decided cells whose adjudicated code is neither coder's
  (`changed_a + changed_b = decided + neither`).

The pair `changed_a`, `changed_b`, with `decided`, is the number of codes the
adjudication changed that analysis plan section 1 asks for; the paper reports
it beside the agreement. The file also holds the SHA-256 of the two coder files
and of `adjudicated.csv`; the analysis tells coder A's file from coder B's by
it.
