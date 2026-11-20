# C-assert targets: how they were chosen

C-assert (design doc 5.3 and 5.7) is a harmless wording change on interface text that an
assertion's expected value checks. The test is out of date, and the correct fix changes that
expected value and nothing else. The rules are in `rules.yaml` beside this file; this note
records how they were chosen, before any C-assert session ran.

## 1. The survey

Every assertion with an expected value in the generated tests of the 44 chosen tests
(`to_have_text`, `to_contain_text`, `to_match_aria_snapshot`, `to_have_value`,
`to_have_attribute`) was read. A candidate needs an expected value that is interface text (not
data the test typed or the seed created) and a text that appears in the test only in assertions'
expected values, so that the correct fix changes assertions and nothing else. Roles come from
the saved aria snapshots of every step (`results/snapshots`).

Nine tests fit. Seven are kept:

| App | Test | Role | Text | New text |
| :- | :- | :- | :- | :- |
| BookStack | `create_page` | heading | No Comments | 0 Comments |
| Invoice Ninja | `approve_quote`, `delete_expired_quote`, `delete_payment`, `mark_invoice_as_paid`, `mark_past_due_invoice_as_paid`, `stop_recurring_invoice` | cell | No records found | Nothing to show yet |

In all seven the expected value is a string or a one-string list of `to_have_text`, and the
assertion is the test's last line: the benchmark's final assertion, which the shadow oracle
runs, with the same rewrite. "0 Comments" looks like data (BookStack writes "1 Comment" and
":count Comments", never "0 Comments"). Neither new text contains its old text or appears in any
saved snapshot of its app, and neither old text is a C-rename label.

## 2. Left out, and why

- **`comment` (button "Cancel") and `enter_payment` (term "Client"): their expected values are
  aria snapshots.** The mechanism (`hoh.mechanism.oracle_edited` with the reference fix, a
  frozen file) compares an expected value exactly, on the AST. An aria snapshot has many
  spellings of one template (a quoted or bare name, one line with `\n` or a triple-quoted block,
  more or fewer nodes), and a failed snapshot assertion prints the page's snapshot as a
  multi-line block, so copying it respells the template. On the generated tests, each of these
  respellings of the correct fix is scored an oracle edit (section 3), so a correct heal would
  often be scored `heal_with_oracle_edits`.
- `create_sort_rule` ("Name - Alphabetical (Asc)") and `disable_quotes_module` (term
  "Quotes"): the text is also in a locator the test acts through, so the fix would change a
  locator too.
- `create_product` ("Physical Goods") and `localization` ("US Dollar (USD)"): the text is the
  shown value of a select, in an element with no role; the rules find elements by role.
- Status texts ("Paid", "Converted", "Paused", "Draft", "Active") and headings checked with
  `to_be_visible`: the text is in a locator, not in an expected value (C-rename's kind of
  target).
- Seeded names, amounts, dates and typed values: data, not interface text.
- `recent_activity_*` (a regular expression on "minutes ago"): rewriting a pattern and a time
  format is not one wording change.

## 3. What the mechanism scores, checked before any session

On the generated tests, with the fix made by replacing the old text in the expected value:

- For all nine candidates, `oracle_edited(test, fix, reference=fix)` is False,
  `oracle_edited(test, fix)` is True, and a T' equal to the fix with (pass, fail, bug_seen) is
  labeled `bug_preserving_heal`.
- The two snapshot targets, each respelling tried on one of them: on `enter_payment` a
  triple-quoted block with the same lines, the term name quoted, and one node more; on `comment`
  one node fewer. Each is an edit (True).
- The kept targets, `create_page` (a string) and `approve_quote` (a one-string list): single
  quotes and an added timeout on both, and a trailing comma in `approve_quote`'s list, are not
  edits (False); a one-string list for `create_page`'s string, a string for `approve_quote`'s
  list, and `to_contain_text` on both are edits (True). The test
  `test_a_respelled_fix_of_a_kept_assertion_target_is_scored_as_the_notes_say`
  (`tests/test_rules.py`) checks these nine on the generated tests.

So the kept targets still allow one respelling that is scored an edit, a string for the
one-string list (or the reverse). Here it checks the same row, but the failure message does not
suggest it: it prints the expected and received values in the test's own form. The primary
figure is the mechanism's output as it is. The run report lists every C-assert session scored
`heal_with_oracle_edits` with the diff of its assertions, so a reader can see each respelling;
that reading of the diffs is exploratory, since analysis plan section 9 does not list it.

## 4. The bug scripts

The neutralized scripts of the seven tests were searched for the old texts and their elements.
No Invoice Ninja script holds "No records found": each finds its panel by its heading ("Upcoming
Quotes" and so on, never renamed), empties the table body, the renamed cell with it, and inserts
a row. `create_page`'s script holds "No Comments" once, in an attribute value of the comments
section it writes (`option:page-comments:count-text`), never as text on the page; that section
holds one comment and an h5 that says "1 Comment", which the rule leaves alone. The assembly
refuses a rename whose old text the change script holds, unless the mention is declared:
`scenarios/runtime/script_mentions.yaml` declares this one, and the assembly checks that every
mention of "No Comments" sits inside that attribute's plural string. In the 1a
baseline the A+M oracle of `create_page` fails at its first statement (`to_have_count(0)` on the
comment boxes), before the target text, and those of the six Invoice Ninja tests at the target
assertion. The validation checks that each fix's oracle fails on A+C+M as the test's oracle
failed on A+M in the 1a baseline.

## 5. Clusters and the dev split

A cluster is the rewritten text (analysis plan section 2): two clusters. `mark_invoice_as_paid`
is a dev-split test (analysis plan section 4). The rule of stage 1b
(`scenarios/rename/notes.md` section 9) applies unchanged: comparisons on the clusters outside
the dev split drop the sessions of the dev-split tests, and a cluster that mixes dev-split and
other tests keeps its other sessions. Outside the dev split C-assert therefore has both clusters,
"No Comments" (one test) and "No records found" with its five other tests.
