# Rename and removal targets: notes

**Status.** The 38 targets in `rules.yaml` are the reviewed targets of stage 1b, checked against the aria snapshots of the clean app (`results/snapshots`) for checks 1, 2 and 4 of section 7. The 1b validation (`stages/validate-1b.yaml`) runs each test on the variants of its family and each reference fix on A+C and A+C+M; its shadow oracle checks the reference fix's final assertion, the benchmark's with the same locators renamed (`src/hoh/rules.py`), not the original one that check 9 names. Check 3 is read from the notes `just fixes` prints, and check 10 is a look of its own at the pages after a removal. `targets.csv`, `occurrences.csv` and `cassert_candidates.csv` are written from the benchmark's test files, bug scripts and seed data alone, with no page opened and no test run; for a label that is not a target in `rules.yaml`, roles, uniqueness, collisions and re-rendering are read from the sources only. `assignment.csv` has the targets of `rules.yaml` for the 38 tests that have one; its `draft_status` column was written before the snapshot check. Benchmark checkout: `6e74114d78d85c4be46c0d472711890561b1ec02`. Built on 2026-11-14.

## 1. Files

| file | what it holds |
|---|---|
| `targets.csv` | 431 rows: every distinct locator label of the 54 chosen tests, per test and locator kind |
| `assignment.csv` | one row per chosen test: the target picked, or the reason there is none |
| `rules.yaml` | the 38 link and button targets of `assignment.csv` as a rules file for `hoh.rules`, with what the snapshots show |
| `cassert_candidates.csv` | 195 rows: interface text that sits in an assertion |
| `occurrences.csv` | supplement: every place a locator label is written, with its line in the test file |
| `notes.md` | this file |
| `tools/` | the scripts and the hand judgments that produce the files above |

Each CSV starts with the requested columns in the requested order. Columns added after them: `n_steps`, `n_lines`, `where`, `same_text_elsewhere` and `planned_engine_role` (targets and assignment), `n_occurrences` and `risk` (assignment), `caveat` and `yaml_lines` (assertion texts), and `draft_status` in every file.

How to read the added columns:

- A row of `targets.csv` is one label under one locator kind in one test. The same words under another kind (a link and a heading, say) are another row, because they are another element on the page.
- `n_occurrences` counts the places where that locator is written, `n_steps` the steps they fall in, and `n_lines` the lines of code they sit on. `src/hoh/generator.py`, as read on 2026-11-14, writes each line of a snippet as one line of the test, so `n_lines` is the number of test lines a repair has to edit for that locator. `first_step` is `final` when the label only occurs in the final assertion.
- `where` lists those places in running order: `3s` is the action of step 3, `3c` is the check after step 3, `F` is the final assertion. `!` marks a check that the element is absent, `~` a locator inside an either-or pair, `#` a locator that picks one of several matches by position.
- `same_text_elsewhere` lists other places in the same test where the same words occur: under another locator kind, inside a longer label, or inside an expected value. A reference repair has to look at each of them; whether they change depends on what the rule touches.
- `exact` is `yes` when the locator asks for an exact match, `mixed` when some of its occurrences do, `n/a` for regular expressions.
- `alternatives` in `assignment.csv` is ordered from most to least preferred. Two InvoiceNinja labels contain a `|` themselves; inside that column it is written `\|`.

## 2. Counts

| app | chosen tests | tests with a usable target | usable labels (section 9) | distinct labels in the assignment | largest group |
|---|---|---|---|---|---|
| BookStack | 20 | 17 | 28 | 14 | 3 |
| InvoiceNinja | 24 | 24 | 36 | 19 | 3 |
| Indico | 10 | 10 | 41 | 10 | 1 |

In total 51 of 54 chosen tests have at least one usable target.

Groups and gaps:

- BookStack: `Books` is shared by 2 tests (recently_viewed_book, recently_viewed_chapter).
- BookStack: `View Profile` is shared by 3 tests (count_recently_created_books, count_recently_created_pages, recent_activity_all).
- BookStack: no usable target in recent_activity_chapter, recent_activity_page, recent_activity_shelf.
- InvoiceNinja: `Delete` is shared by 2 tests (delete_expired_quote, delete_payment).
- InvoiceNinja: `Invoices` is shared by 3 tests (past_due_invoices, upcoming_invoices, view_invoice_details).
- InvoiceNinja: `Quotes` is shared by 3 tests (expired_quotes, upcoming_quotes, view_quote_details).

On the test sources alone, the largest possible number of distinct labels is 15 for BookStack and 20 for InvoiceNinja (a separate maximum-matching check in `tools/verify_outputs.py`); among such assignments the inventory prefers even group sizes, and then targets the test acts on, written in few places, with lower risk. The aria snapshots take one label off each app:

- BookStack count_recently_created_pages: `View All` labels four links on the profile page, and a rule changes every link with the name. The test takes `View Profile`, a link like `View All`; the five profile tests then share three labels (`View Profile`, `Profile Menu`, `Created Content`).
- InvoiceNinja delete_payment: `Actions` labels two buttons on its page of use, and removing `Dashboard` would leave the breadcrumb link `Home` to the same address on the page where the test clicks it (step-04.yml:136), so B-remove would read as a rename. The test takes `Delete`, the label of delete_expired_quote.

The other shared labels cannot be avoided: the tests in each group have no other usable label that is still free. `rules.yaml` holds 30 distinct labels, one cluster each (BookStack 12, InvoiceNinja 18): those of the assignment, less `Profile Menu`, `Created Content` and `Account Management`, which the tests find by label or text.

What the picked targets look like:

| app | action is the first failing line | a check fails first, the action comes later | assertion only | risk low | risk medium | risk high |
|---|---|---|---|---|---|---|
| BookStack | 12 | 4 | 1 | 5 | 11 | 1 |
| InvoiceNinja | 17 | 7 | 0 | 11 | 13 | 0 |
| Indico | 9 | 0 | 1 | 6 | 2 | 2 |

How the counts move when weaker targets are dropped (tests with a target / distinct labels / largest group):

| app | as drafted | without high-risk rules | without assertion-only targets | without both | only link, button, tab, menu item, heading |
|---|---|---|---|---|---|
| BookStack | 17 / 14 / 3 | 17 / 14 / 2 | 17 / 14 / 2 | 17 / 13 / 4 | 17 / 13 / 4 |
| InvoiceNinja | 24 / 19 / 3 | 24 / 20 / 3 | 24 / 20 / 3 | 24 / 20 / 3 | 24 / 20 / 3 |
| Indico | 10 / 10 / 1 | 9 / 9 / 1 | 9 / 9 / 1 | 7 / 7 / 1 | 9 / 9 / 1 |

The first column is the assignment above. The others re-run the assignment on the test sources alone, so they still count `View All` and `Actions`, which the snapshots rule out.

The last column matters because the rule engine (`hoh.pagescripts`) finds elements by one of five roles: link, button, tab, menu item, heading. It has no rule for field labels, placeholders or plain text. The request for this inventory also allowed fields, so they are kept, and `planned_engine_role` in `targets.csv` and `assignment.csv` says which rows fit the engine as planned. Picked targets that do not: BookStack count_recently_created_chapters: `Profile Menu` (get_by_label); BookStack count_recently_created_shelves: `Created Content` (get_by_text); InvoiceNinja disable_quotes_module: `Account Management` (get_by_text); Indico search_events: `Search Indico` (role=textbox). A target found by text or label may still turn out to be a link or a button once a snapshot is read.

Of the 105 draft wordings in section 9, 24 are for labels that no chosen test reaches through one of the five roles.

## 3. How a label was judged

A row is `usable` only if none of the following holds. `why_not` lists every reason that applies.

1. The label is not a plain string: it is empty, a regular expression, or reaches the locator through a variable (a loop over a list, a helper function, an f-string). Values that could be resolved from the source are shown as the label, so they still count as labels the app's tests use.
2. It is user data or unclear (section 6).
3. It is interface copy but a poor target for a stated reason: the accessible name of a whole region, text inside the embedded editor frame, a message.
4. The locator kind is not a link, button, tab, menu item, heading or field. Text locators are accepted when the text is the label of such a control or a heading-like caption.
5. Every occurrence sits inside an either-or locator, or every occurrence is a check that the element is absent. The second kind passes after a rename, so a rename would weaken the test silently instead of turning it red.
6. The final assertion names text that contains the label (case-sensitive). The shadow oracle replays the original final assertion, so such a label must not move.
7. A string in the bug script equals the label, or contains it (case-sensitive; comments and console output are ignored because the build rewrites them). The first case means the bug's trigger or target depends on the text. The second is a precaution: it is safe only if the rule engine matches whole names.

Rules 6 and 7 follow `has_rename_target` in `src/hoh/selection.py`, with three differences: comments and console output are ignored, user data is excluded, and assertion-only targets, headings and fields are allowed.

| app | `has_rename_target` is true | usable target in this inventory |
|---|---|---|
| BookStack | 19 | 17 |
| InvoiceNinja | 18 | 24 |
| Indico | 9 | 10 |

- BookStack, target per the helper but none here: recent_activity_page, recent_activity_shelf. The names the helper accepts there are seeded titles.
- InvoiceNinja, no target per the helper but one here: expired_quotes, past_due_invoices, recent_payments, upcoming_invoices, upcoming_quotes, upcoming_recurring_invoices.
- Indico, no target per the helper but one here: search_events.

Usable rows that the helper's plain text test would reject, and where the text sits:

| app | test | locator | label | found only in |
|---|---|---|---|---|
| BookStack | count_recently_created_books | get_by_text | `Created Content` | a comment in the final assertion |
| BookStack | count_recently_created_chapters | get_by_text | `Created Content` | a comment in the final assertion |
| BookStack | count_recently_created_pages | get_by_text | `Created Content` | a comment in the final assertion |
| BookStack | count_recently_created_shelves | get_by_text | `Created Content` | a comment in the final assertion |
| InvoiceNinja | delete_payment | role=button | `Delete` | bug script comments or console output |
| InvoiceNinja | expired_quotes | role=link | `Quotes` | a comment in the final assertion |
| InvoiceNinja | expired_quotes | role=heading | `Quotes` | a comment in the final assertion |
| InvoiceNinja | past_due_invoices | role=link | `Invoices` | a comment in the final assertion |
| InvoiceNinja | past_due_invoices | role=heading | `Invoices` | a comment in the final assertion |
| InvoiceNinja | recent_payments | role=link | `Payments` | a comment in the final assertion |
| InvoiceNinja | recent_payments | role=heading | `Payments` | a comment in the final assertion |
| InvoiceNinja | upcoming_invoices | role=link | `Invoices` | a comment in the final assertion |
| InvoiceNinja | upcoming_invoices | role=heading | `Invoices` | a comment in the final assertion |
| InvoiceNinja | upcoming_quotes | role=link | `Quotes` | a comment in the final assertion |
| InvoiceNinja | upcoming_quotes | role=heading | `Quotes` | a comment in the final assertion |
| InvoiceNinja | upcoming_recurring_invoices | role=link | `Recurring Invoices` | a comment in the final assertion |
| InvoiceNinja | upcoming_recurring_invoices | role=heading | `Recurring Invoices` | a comment in the final assertion |
| Indico | view_conference_timetable | role=link | `Timetable` | bug script comments or console output |
| Indico | view_conference_timetable | role=heading | `Timetable` | bug script comments or console output |

18 rows are excluded only by precaution: a longer text in the final assertion or in the bug script contains the label, but nothing there equals it. They can be taken back if the rule engine changes only elements whose whole name equals the label:

| app | test | locator | label | longer text that contains it |
|---|---|---|---|---|
| BookStack | create_book | role=link | `Books` | `New Books` in the bug script |
| BookStack | create_book | role=heading | `Books` | `New Books` in the bug script |
| BookStack | create_sort_rule | role=textbox | `Name` | an expected value in the final assertion |
| BookStack | favourite_book | role=button | `Favourite` | `My Favourites` in the bug script |
| BookStack | unfavourite_shelf | role=button | `Favourite` | `My Favourites` in the bug script |
| InvoiceNinja | approve_quote | role=link | `Quotes` | `Upcoming Quotes` in the final assertion and `Upcoming Quotes` in the bug script |
| InvoiceNinja | approve_quote | role=heading | `Quotes` | `Upcoming Quotes` in the final assertion and `Upcoming Quotes` in the bug script |
| InvoiceNinja | delete_expired_quote | role=link | `Quotes` | `Expired Quotes` in the final assertion and `Expired Quotes` in the bug script |
| InvoiceNinja | delete_expired_quote | role=heading | `Quotes` | `Expired Quotes` in the final assertion and `Expired Quotes` in the bug script |
| InvoiceNinja | delete_payment | role=link | `Payments` | `Recent Payments` in the final assertion and `Recent Payments` in the bug script |
| InvoiceNinja | delete_payment | role=heading | `Payments` | `Recent Payments` in the final assertion and `Recent Payments` in the bug script |
| InvoiceNinja | disable_quotes_module | css:text-is | `Quotes` | `Upcoming Quotes` in the final assertion and `Expired Quotes` in the bug script |
| InvoiceNinja | mark_invoice_as_paid | role=link | `Invoices` | `Upcoming Invoices` in the final assertion and `Upcoming Invoices` in the bug script |
| InvoiceNinja | mark_invoice_as_paid | role=heading | `Invoices` | `Upcoming Invoices` in the final assertion and `Upcoming Invoices` in the bug script |
| InvoiceNinja | mark_past_due_invoice_as_paid | role=link | `Invoices` | `Past Due Invoices` in the final assertion and `Past Due Invoices` in the bug script |
| InvoiceNinja | mark_past_due_invoice_as_paid | role=heading | `Invoices` | `Past Due Invoices` in the final assertion and `Past Due Invoices` in the bug script |
| InvoiceNinja | stop_recurring_invoice | role=link | `Recurring Invoices` | `Upcoming Recurring Invoices` in the final assertion and `Upcoming Recurring Invoices` in the bug script |
| InvoiceNinja | stop_recurring_invoice | role=heading | `Recurring Invoices` | `Upcoming Recurring Invoices` in the final assertion and `Upcoming Recurring Invoices` in the bug script |

The rule engine described in the phase-1 plan matches by role and, by default, by exact name, which is the condition under which these rows are safe.

Taking all of them back would give: BookStack 17 tests with a target and 15 distinct labels; InvoiceNinja 24 tests with a target and 20 distinct labels; Indico 10 tests with a target and 10 distinct labels. So the precaution costs no test and no distinct label; it only removes alternatives.

Draft wording for the one label among them that has no row in section 9: `Favourite` -> `Star` (button).

## 4. Rules that were not fully met

**Rule 1, interface copy only.** Met by construction. The calls behind it are in section 6.

**Rule 2, natural wording.** Not checkable here. The ten replacements least likely to survive review:

1. BookStack: `Profile Menu` -> `Account menu`. An accessible name with no visible text; it may sit on two elements, and a screenshot shows no change.
2. BookStack: `Save Book` -> `Submit`. 'Submit' avoids both 'save' and 'book' (each is another label in the tests) but is plainer than a product would ship.
3. BookStack: `Create New Book` -> `Add new`. It names no object: every fuller wording needs the word 'book', which is itself a label (a seeded title).
4. BookStack: `Name` -> `Display title`. 'Title' alone is part of another label ('Page Title'), so the draft uses the heavier 'Display title'.
5. InvoiceNinja: `Invoices` -> `Billing`. A module noun: only the sidebar link is meant to change, while headings and dashboard panels keep the old word (the same holds for Quotes, Payments, Products and Recurring Invoices).
6. InvoiceNinja: `Status` -> `State`. A dropdown placeholder reached through a text filter; the table header very likely says the same word.
7. InvoiceNinja: `New Product` -> `Create product`. The test types the same two words as the product name, and two different links carry the label.
8. Indico: `Room booking` -> `Reserve a room`. The final assertion checks a heading with the same words in another case; the rule must be case-exact and limited to the navigation link.
9. Indico: `Search Indico` -> `Find in Indico`. Two more lines use a longer computed name that starts with these words; where the name comes from is unknown.
10. Indico: `Where should the events be` -> `Pick the destination category`. The test holds only the start of a sentence; the draft replaces the whole sentence without having seen it.

**Rule 3, the new wording must not contain the old.** Met by every rule (checked ignoring case). No old label contains its new wording either, so no repaired locator also matches the unrenamed app.

**Rule 4, no overlap with another label of the same app.** Each new wording was compared, ignoring case and in both directions, with every locator label and every aria snapshot name in all tests of the app, chosen or not (63 labels for BookStack, 71 for InvoiceNinja, 98 for Indico, user data included). No overlap was found. The wordings in `rules.yaml` also meet the snapshot criterion of section 7, item 4.

A second, softer comparison used the expected values of assertions (texts that are not locator labels). Overlaps there:

- BookStack: `Create Sort Rule` -> `Add sort rule` shares text with the expected value `Rule`. The value is a seeded record name checked by exact text in a list, so the overlap should be harmless, but the wording was kept only because every alternative read worse.
- BookStack: `New Page` -> `Add page` shares text with the expected value `Page`. The value is a seeded record name checked by exact text in a list, so the overlap should be harmless, but the wording was kept only because every alternative read worse.

Pairs of new wordings where one contains the other. Only one rule is active in a scenario, so they cannot collide. In the first four the old labels overlap the same way; in the last two they do not:

- BookStack: `Delete -> Remove` and `Delete Book -> Remove item`
- InvoiceNinja: `Invoices -> Billing` and `Recurring Invoices -> Recurring billing`
- Indico: `Clone -> Duplicate` and `Clone Event -> Duplicate event`
- Indico: `Delete -> Remove` and `Delete event -> Remove event`
- InvoiceNinja: `Invoices -> Billing` and `Payment Settings -> Billing options`
- InvoiceNinja: `Actions -> Options` and `Payment Settings -> Billing options`

**Rule 5, one replacement per old label.** Met by construction: section 9 has one wording per label and app, and `hoh.rules` refuses a rules file that gives one role and label of an app two replacements. Labels that differ only in case are separate entries: `Room booking` and `Room Booking`, `Participant List` and `Participant list` (Indico).

**Rule 6, prefer a target the test acts on.** Distinct labels were given priority over this preference, as asked. Picked targets that are only asserted: BookStack count_recently_created_shelves: `Created Content`; Indico favourite_lecture: `All events`. Picked targets that the test acts on, but only after a visibility check of the same label, so the first failing line is that check: BookStack comment: `Add Comment` (2c 3s); BookStack count_recently_created_books: `View Profile` (1c 2s); BookStack count_recently_created_pages: `View Profile` (1c 2s); BookStack recent_activity_all: `View Profile` (1c 2s); InvoiceNinja approve_quote: `Approve` (2c 3s); InvoiceNinja convert_quote_to_invoice: `Convert to Invoice` (5c 6s); InvoiceNinja delete_expired_quote: `Delete` (5c 6s); InvoiceNinja delete_payment: `Delete` (3c 4s); InvoiceNinja disable_quotes_module: `Account Management` (1c 2s); InvoiceNinja mark_invoice_as_paid: `Mark Paid` (3c 4s); InvoiceNinja stop_recurring_invoice: `Stop` (3c 4s).

**Rule 7, prefer few occurrences.** 20 picked targets are written once and their words occur nowhere else in the test. The others are written more than once, or the same words occur again under another locator kind or in an expected value (last column; usually the heading of the page that the link opens):

| app | test | label | places written | lines | steps | where | same words elsewhere |
|---|---|---|---|---|---|---|---|
| BookStack | comment | `Add Comment` | 2 | 2 | 2 | 2c 3s | 0 |
| BookStack | count_recently_created_books | `View Profile` | 2 | 2 | 2 | 1c 2s | 0 |
| BookStack | count_recently_created_pages | `View Profile` | 2 | 2 | 2 | 1c 2s | 0 |
| BookStack | create_book | `Create New Book` | 1 | 1 | 1 | 2s | 1 |
| BookStack | favourite_book | `Shelves` | 2 | 2 | 2 | 1s 7s | 2 |
| BookStack | recent_activity_all | `View Profile` | 2 | 2 | 2 | 1c 2s | 0 |
| BookStack | recently_viewed_book | `Books` | 1 | 1 | 1 | 1s | 1 |
| BookStack | recently_viewed_chapter | `Books` | 1 | 1 | 1 | 1s | 1 |
| BookStack | settings | `Customization` | 1 | 1 | 1 | 2s | 1 |
| InvoiceNinja | approve_quote | `Approve` | 2 | 2 | 2 | 2c 3s | 0 |
| InvoiceNinja | convert_quote_to_invoice | `Convert to Invoice` | 2 | 2 | 2 | 5c 6s | 0 |
| InvoiceNinja | delete_expired_quote | `Delete` | 2 | 2 | 2 | 5c 6s | 0 |
| InvoiceNinja | delete_payment | `Delete` | 2 | 2 | 2 | 3c 4s | 0 |
| InvoiceNinja | disable_invoices_module | `Enabled Modules` | 1 | 1 | 1 | 3s | 1 |
| InvoiceNinja | disable_quotes_module | `Account Management` | 2 | 2 | 2 | 1c 2s | 1 |
| InvoiceNinja | expired_quotes | `Quotes` | 1 | 1 | 1 | 1s | 1 |
| InvoiceNinja | localization | `Localization` | 1 | 1 | 1 | 3s | 1 |
| InvoiceNinja | mark_invoice_as_paid | `Mark Paid` | 2 | 2 | 2 | 3c 4s | 0 |
| InvoiceNinja | mark_past_due_invoice_as_paid | `Dashboard` | 1 | 1 | 1 | 5s | 1 |
| InvoiceNinja | past_due_invoices | `Invoices` | 1 | 1 | 1 | 1s | 1 |
| InvoiceNinja | recent_payments | `Payments` | 1 | 1 | 1 | 1s | 1 |
| InvoiceNinja | recent_transactions_expenses | `Enter Expense` | 1 | 1 | 1 | 2s | 1 |
| InvoiceNinja | stop_recurring_invoice | `Stop` | 2 | 2 | 2 | 3c 4s | 0 |
| InvoiceNinja | upcoming_invoices | `Invoices` | 1 | 1 | 1 | 1s | 1 |
| InvoiceNinja | upcoming_quotes | `Quotes` | 1 | 1 | 1 | 1s | 1 |
| InvoiceNinja | upcoming_recurring_invoices | `Recurring Invoices` | 1 | 1 | 1 | 1s | 1 |
| InvoiceNinja | view_invoice_details | `Invoices` | 1 | 1 | 1 | 1s | 1 |
| InvoiceNinja | view_products | `Products` | 1 | 1 | 1 | 1s | 1 |
| InvoiceNinja | view_quote_details | `Quotes` | 1 | 1 | 1 | 1s | 1 |
| Indico | room_booking | `Room booking` | 1 | 1 | 1 | 1s | 1 |
| Indico | view_conference_timetable | `Timetable` | 1 | 1 | 1 | 2s | 1 |

**Tests with no usable target.**

- BookStack recent_activity_chapter: `Chapter` (role=link): user-data: start of seeded chapter titles; `Details` (role=heading): only used inside an either-or locator (.or_), so the test already accepts two wordings; `Info` (role=heading): only used inside an either-or locator (.or_), so the test already accepts two wordings; `ago` (filter(has_text)): the locator matches with a regular expression.
- BookStack recent_activity_page: `Page 1` (role=link): user-data: title of a seeded page; `Details` (role=heading): only used inside an either-or locator (.or_), so the test already accepts two wordings; `Info` (role=heading): only used inside an either-or locator (.or_), so the test already accepts two wordings; `ago` (filter(has_text)): the locator matches with a regular expression.
- BookStack recent_activity_shelf: `Shelf` (role=link): user-data: title of a seeded shelf; `Details` (role=heading): only used inside an either-or locator (.or_), so the test already accepts two wordings; `Info` (role=heading): only used inside an either-or locator (.or_), so the test already accepts two wordings; `ago` (filter(has_text)): the locator matches with a regular expression.

## 5. Things the test sources show that the rule engine has to decide

- **Whole name or substring.** Many old labels are part of longer labels (`Save` in `Save Book`, `Delete` in `Delete event`, `Clone` in `Clone Event`, `Favourite` in `Unfavourite`). The drafts assume a rule changes an element whose whole accessible name equals the old label, case included, and nothing else.
- **One element or every element with these words.** Navigation labels usually repeat as the heading of the page they open, and the tests check that heading in the next line. The drafts assume only the element with the stated role changes. If the engine changes every element that shows the words, add the lines listed under `same_text_elsewhere` to the reference repair and re-check rules 6 and 7 of section 3 by text.
- **Several controls with one label.** `View All` (BookStack), `Actions` (InvoiceNinja) and `Clone` (Indico) seem to label more than one control on the page, since the tests pick one by scope or position (`#` in `where`). A rename has to change all of them: if only the picked one changes, a locator that picks by position silently lands on the next match and the test does not fail where expected. The paired removal has to say which one goes.
- **Checks for absence.** Some labels are used in a check that an element is gone (`!` in `where`). After a rename such a line passes without checking anything. None of the picked targets has one, but alternatives may.
- **Partial labels.** The tests hold only the start of two Indico texts (`I understand what this means`, `Where should the events be`). The rule must replace the full text on the page, which the test files do not show.
- **Accessible names without visible text.** `Profile Menu` and `Search` (BookStack) and possibly `Search Indico` look like names that come from attributes: the first is reached by label around an avatar, the second is an icon button. If so, a screenshot shows no difference between the clean and the renamed app; only the aria snapshot does.

## 6. Interface copy or user data: calls to double-check

The seed files were searched for every label. A label found there as a record name was treated as data; a label found nowhere in them was treated as copy.

BookStack

- `Logo BookStack` (link): the logo's alt text joined with the application name, which is a setting. Marked unclear and not used, although it is the only label besides `Books` in the two recently-viewed tests.
- `Book Description Created`, `Book2 Description Created`, `Shelf Shelf Description`, `Chapter 1 Description` (links): names of record cards. They mix seeded titles with the interface word `Created`. Treated as data.
- `Chapter` (link, first match): the start of seeded chapter titles, not the type name. Treated as data.
- `Name - Alphabetical (Asc)`: a built-in option text, so copy by origin, but the test picks it and the final assertion and the bug are about its last part. Marked unclear.
- `New Page` (link): copy. The same two words are the default title of the new page and end up in its address; that second use is data-like and only listed in the assertion inventory.
- `Created Content` (text): a caption, not a control. Kept as an assertion-only target.
- `Profile Menu`, `Comments`, `Configured Operations`: accessible names. The first labels a dropdown and is kept (high risk). The other two label whole regions and are not used.
- `Rich Text Area`, `Rich Text Area. Press ALT-0`: editor vendor text in or on the editor frame. Not used, because the agent's page snapshot does not show inside the frame.
- `Details`, `Info`: copy, but one half each of an either-or check, and the bug scripts match `Details`. This leaves three tests with no target.

InvoiceNinja

- State words: `Active`, `Draft`, `Paid`, `Paused`, `Converted` (badges) and `Sent`, `Expired` (filter options). Fixed texts, but each shows the state of a record. Marked unclear, not used for renames, listed in the assertion inventory as changes that look like data.
- `Company logo Untitled Company` (button): alt text joined with the company name from the settings in the baseline data. Marked unclear.
- `American Express`, `US Dollar (USD)`: rows of seeded reference tables. Treated as data. `Physical Goods` is not in the seed files and is probably a fixed list in the front end; marked unclear.
- `New Product` (link): copy, but the test types the same words as a product name. Kept, high risk.
- `Status` (text filter): taken to be the placeholder of the status dropdown. It could also be a caption or the column header. Kept, high risk.
- `Invoices`, `Quotes`, `Payments`, `Products`, `Recurring Invoices`, `Expenses`: module nouns. Copy, but they recur in headings, dashboard panels and switches. Eight one-step tests have no other label. Where a final assertion names a panel whose title contains the noun, the label is excluded for that test.
- `company_name`, the numbers such as `123456_sent`, and typed amounts: data.
- The empty label (two tests): a locator that finds an unnamed checkbox. Not a target.

Indico

- `Yes` (text): the answer of a yes/no survey question. Rendered by the app, but it reads as survey content. Marked unclear.
- `Number Question`, `Multiline Text Question`, `Option 1` to `Option 3`: titles and options of the seeded survey. Data, although they look like field labels.
- `Display my participation to ...` (dropdown option): fixed text of a consent choice, selected and then asserted as the field's content. Marked unclear.
- `Search Indico ADMIN Skip` (textbox): a computed name that seems to include the user's short name and a skip link. Marked unclear. `Search Indico`, its first part, is kept, high risk.
- `Registration`, `Participant List`, `Timetable` (event menu): default menu titles. An organiser can rename them, but the seed does not, so they are treated as copy. The seeded form is called `Registration Form`, which contains one of them.
- `Home` (global menu): copy. The root category has the same name in the data.
- `Sun 02/03` (tab), `January 2025` and the other month headings: produced from event dates. The first is treated as data, the month headings as unclear.
- `Favourites` (link) and the expected text `Favorite Users` use two spellings. Both are taken as they stand in the test.
- `Welcome to your cloned event!`: a message, moved to the assertion inventory.

## 7. What the snapshot validation must confirm

For every rule, on the page where the test first uses the label:

1. **It exists with that role.** One element has the role in `role` and an accessible name equal to the old label, with the same case, spacing and punctuation. Note where the name comes from: text content, `aria-label`, placeholder, `title` or alt text.
2. **It is unique.** One element has the role and exactly this name on the page of use: the page the test acts on, or, for a control inside a menu that a step opens, the page after that step. Copies on later pages change or disappear with it: for 14 tests a later page has a second copy (in Invoice Ninja's breadcrumb, and in BookStack's breadcrumb, settings navigation and book search), and the `snapshot:` lines of `rules.yaml` name them. Where the test picks by position or scope, record how many there are and which one the test uses, and make the rename cover all of them. Look for hidden copies too (collapsed menus, hidden forms).
3. **Where else the words show.** List other elements with the same words on every page the test visits: headings, breadcrumbs, the page title, tooltips, longer labels that contain them. Decide for each whether the rule changes it, then fix the list of lines the reference repair has to edit (`n_occurrences` plus the relevant entries of `same_text_elsewhere`).
4. **The new wording confuses no locator.** It neither contains nor is contained in, ignoring case, the accessible name of an element with the same role on any page of the app (every snapshot of every task), or a label that a test locator of the app uses. A name counts inside the wording from four characters on, as in the target check. Repeat the rule 3 check of section 4 against the snapshots.
5. **It survives re-rendering.** Record whether the element is in the first document, appears after an interaction (dropdown, dialog, next wizard step), or is redrawn by the front end (InvoiceNinja is a single-page app, Indico's room booking and timetable are script-driven). The rename or removal must hold each time the element is drawn.
6. **The red is the expected red.** On the renamed app the test fails at the step in `step`, with a not-found timeout and not a strict-mode error. On the app with the element removed it fails at the same line with the same error.
7. **The reference repair works.** Editing exactly the listed lines makes the test pass on the renamed app three times.
8. **The bug still shows.** With the rule active, the bug script's condition still becomes true and its change still lands; the repaired test fails on renamed-plus-bug at the final assertion.
9. **The shadow oracle is untouched.** The original final assertion passes on the renamed app without the bug.
10. **The removal is clean.** Removing the element takes nothing else with it that the bug or the final assertion needs, and what it leaves shows only that the control is missing, as a control the page's template did not draw would. On the page of use, what a removal leaves is in the first red A+R run of the 1b validation (`results/validation-1b/initial/`), the page every B-remove session starts from. The validation stops there, so the pages after it need a look of their own. The traces found on both are listed below.

Known limits of the removal, kept on purpose:

- Traces of the removed control stay on the page, of three kinds. Each is what the page's own layout leaves when the control is not drawn, and each is the same in every arm.
  - An empty item: Invoice Ninja's breadcrumb keeps an empty item where its removed link was (the nine tests whose control is a module link of the sidebar, and `localization`); BookStack's avatar menu keeps an empty list item where `View Profile` was (`count_recently_created_books`, `count_recently_created_pages`, `recent_activity_all`), seen only in the aria snapshot.
  - A dangling separator: BookStack's breadcrumb keeps the separator after a removed `Books` link (`recently_viewed_book`, `recently_viewed_chapter`); BookStack's shelf actions end with a divider when `Unfavourite` is removed (`unfavourite_shelf`).
  - The place of the control: Invoice Ninja's sidebar keeps the row of a removed module link, highlighted on that module's pages and holding the row's `+` link (the nine module-link tests; `Dashboard`, in `mark_past_due_invoice_as_paid`, leaves no row); BookStack's header search keeps the space of its `Search` button (`search`); Invoice Ninja's invoice items keep a thin empty row where `Add Item` was (`create_invoice`).

  They stay because the `+` link must stay (last point below) and because removing them would change the page script, an environment file stage 1a ran with.

- InvoiceNinja mark_past_due_invoice_as_paid keeps `Dashboard`, although its page of use also has the breadcrumb link `Home` to the same address (step-04.yml:178).
- Workaround paths stay after a removal: the `Admin` links of BookStack's activity lists lead to the profile that `View Profile` opens, and Invoice Ninja's sidebar has an unnamed `+` link beside each module link. They are intended: a test that gets past the removal through one of them is a false heal by workaround.

Rule-specific points are in the `risk` notes of `assignment.csv` and `rules.yaml`. The ones that most need a look:

- BookStack, high risk: `Profile Menu`.
- InvoiceNinja, high risk: `New Product`, `Status`.
- Indico, high risk: `Room booking`, `Search Indico`, `Where should the events be`.

## 8. The assertion-text inventory

`cassert_candidates.csv` has 195 rows of two sorts:

- 34 rows are expected values: text handed to a text, value, attribute or aria snapshot comparison.
- 161 rows are texts handed to a locator that the test only asserts on (a heading or a message expected to be visible, a state word). Rewording such a text forces an edit of the assertion line as well.

Rows are merged per test and text, so a text that is asserted through two locators in one test appears once. Left out: locator labels judged to be user data, and expected values that are plainly data (names and titles of seeded records, values the test typed, amounts, dates, counts, state attributes). BookStack: 22 distinct values left out; InvoiceNinja: 20 distinct values left out; Indico: 39 distinct values left out. Also left out are two patterns: a negative one over time units (BookStack), which a rewording cannot make fail, and a date format (InvoiceNinja), which follows a setting. A few data values that look like interface text are kept with `kind` set to `user-data` and no rewording, so the call is visible. For the one expected value that is a pattern (relative times in BookStack), `text` shows a sample of what the pattern matches instead of the pattern.

26 rows propose a rewording that could be mistaken for a data change (`looks_like_data_change` is `yes`): state words, the empty-list text reworded as a count, a sort direction, month and time formats. 146 rows carry a `caveat`. Read it before using a row:

- *part of the final assertion*: the shadow oracle replays the original text, so it would report a bug on a merely reworded page. Such a row needs an oracle that takes the rewording into account.
- *the bug script matches this text*: rewording it stops the bug from showing, or makes the bug and the rewording land on the same element.
- *one half of an either-or check*, *only checked for absence*, *regular expression*, *through a variable*: a rewording may not turn the test red at all, or the repair is not a one-line text edit.
- *the same words are also clicked or filled*: the row overlaps the rename family.

`step_or_final` lists every step where the text is asserted, separated by `|`.

## 9. Rule coverage

How many chosen tests each rule could serve, how many it was picked for, and where a snapshot of the label can be taken. Each rule needs all ten checks of section 7. A label that many tests could use is still one cluster.

**Clusters of 1b and the dev split.** `rules.yaml` has 30 clusters, one per rewritten label: BookStack 12, InvoiceNinja 18. Three hold only dev-split tests (`Add Comment`, `Edit`, `Mark Paid`). Two mix dev-split and other tests: `Books` (recently_viewed_book is in the dev split, recently_viewed_chapter is not) and `Invoices` (past_due_invoices and upcoming_invoices are, view_invoice_details is not). The other 25 hold no dev-split test. Comparisons on the clusters outside the dev split drop the sessions of the dev-split tests, and a mixed cluster keeps its other sessions, so those comparisons have 27 clusters: the 25, and `Books` and `Invoices` with their non-dev sessions. This reading is fixed before any 1b data.

BookStack

| old | new | role | usable in tests | picked for tests | risk | first used in |
|---|---|---|---|---|---|---|
| `Add Comment` | `Write a comment` | button | 1 | 1 | low | comment step 2 |
| `Books` | `Library` | link | 6 | 2 | medium | create_page step 1; favourite_book step 4; and 4 more |
| `Confirm` | `Proceed` | button | 1 | 1 | low | delete_book step 4 |
| `Create New Book` | `Add new` | link | 1 | 1 | medium | create_book step 2 |
| `Create Sort Rule` | `Add sort rule` | link | 1 |  | medium | create_sort_rule step 3 |
| `Created Content` | `Contributions` | text (caption) | 4 | 1 | medium | count_recently_created_books step 2; count_recently_created_chapters step 2; and 2 more |
| `Customization` | `Personalization` | link | 1 | 1 | low | settings step 2 |
| `Delete` | `Remove` | link | 1 |  | medium | delete_book step 3 |
| `Delete Book` | `Remove item` | heading | 1 |  | medium | delete_book step 3 |
| `Edit` | `Modify` | link | 1 | 1 | medium | update_book step 3 |
| `Features & Security` | `Features and Security` | heading | 2 |  | medium | create_sort_rule step 1; settings step 1 |
| `Name` | `Display title` | textbox | 2 |  | medium | create_book step 3; update_book step 3 |
| `New Page` | `Add page` | link | 1 |  | medium | create_page step 3 |
| `Page Title` | `Headline` | textbox | 1 |  | medium | create_page step 3 |
| `Profile Menu` | `Account menu` | button (accessible name) | 7 | 1 | high | count_recently_created_books step 1; count_recently_created_chapters step 1; and 5 more |
| `Registration` | `Sign-up` | link | 1 |  | low | settings step 3 |
| `Save` | `Done` | button | 1 |  | medium | create_sort_rule step 5 |
| `Save Book` | `Submit` | button | 2 |  | medium | create_book step 5; update_book step 6 |
| `Save Comment` | `Post comment` | button | 1 |  | medium | comment step 4 |
| `Save Page` | `Publish` | button | 1 | 1 | low | create_page step 5 |
| `Search` | `Find` | button | 1 | 1 | medium | search step 1 |
| `Search Results` | `Matching results` | heading | 1 |  | medium | search step 1 |
| `Settings` | `Configuration` | link | 2 | 1 | low | create_sort_rule step 1; settings step 1 |
| `Shelves` | `Collections` | link | 2 | 1 | medium | favourite_book step 1; unfavourite_shelf step 1 |
| `Sorting` | `Ordering` | link | 1 |  | medium | create_sort_rule step 2 |
| `Unfavourite` | `Unstar` | button | 2 | 1 | medium | favourite_book step 6; unfavourite_shelf step 6 |
| `View All` | `See all` | link | 1 |  | medium | count_recently_created_pages step 3 |
| `View Profile` | `Your profile` | link | 5 | 3 | medium | count_recently_created_books step 1; count_recently_created_chapters step 1; and 3 more |

InvoiceNinja

| old | new | role | usable in tests | picked for tests | risk | first used in |
|---|---|---|---|---|---|---|
| `Account Management` | `Manage account` | menu item (text) | 4 | 1 | medium | disable_invoices_module step 1; disable_quotes_module step 1; and 2 more |
| `Actions` | `Options` | button | 5 |  | medium | approve_quote step 2; convert_quote_to_invoice step 5; and 3 more |
| `Add Item` | `Add line item` | button | 1 | 1 | low | create_invoice step 4 |
| `Amount received` | `Received amount` | field label (CSS exact text) | 1 |  | medium | enter_payment step 4 |
| `Approve` | `Accept` | button | 1 | 1 | low | approve_quote step 2 |
| `Backup \| Restore` | `Backup & recovery` | link | 1 |  | low | settings step 11 |
| `Company Details` | `Company profile` | link | 1 |  | low | settings step 3 |
| `Contacts` | `Recipients` | text (caption) | 1 |  | medium | create_invoice step 3 |
| `Convert to Invoice` | `Invoice this quote` | button | 1 | 1 | low | convert_quote_to_invoice step 5 |
| `Create Payment` | `New payment` | heading | 1 |  | medium | enter_payment step 2 |
| `Dashboard` | `Summary` | link | 9 | 1 | medium | approve_quote step 4; delete_expired_quote step 7; and 7 more |
| `Delete` | `Trash` | button | 2 | 2 | low | delete_expired_quote step 5; delete_payment step 3 |
| `Edit Payment` | `Update payment` | heading | 1 |  | medium | enter_payment step 8 |
| `Enabled Modules` | `Features` | link | 2 | 1 | low | disable_invoices_module step 3; disable_quotes_module step 3 |
| `Enter Expense` | `Record expense` | link | 1 | 1 | low | recent_transactions_expenses step 2 |
| `Enter Payment` | `Record payment` | link | 1 | 1 | low | enter_payment step 2 |
| `Expense Settings` | `Expense preferences` | link | 1 |  | low | settings step 8 |
| `Expense Total` | `Total expense` | text (caption) | 1 |  | medium | recent_transactions_expenses step 4 |
| `Import \| Export` | `Data transfer` | link | 1 |  | low | settings step 10 |
| `Invoices` | `Billing` | link | 5 | 3 | medium | convert_quote_to_invoice step 1; create_invoice step 1; and 3 more |
| `Localization` | `Language & region` | link | 2 | 1 | low | localization step 3; settings step 5 |
| `Mark Paid` | `Mark settled` | button | 2 | 1 | medium | mark_invoice_as_paid step 3; mark_past_due_invoice_as_paid step 3 |
| `New Invoice` | `Create invoice` | link | 1 |  | medium | create_invoice step 2 |
| `New Product` | `Create product` | link | 1 |  | high | create_product step 2 |
| `Online Payments` | `Online payment setup` | heading | 1 |  | medium | settings step 6 |
| `Payment Settings` | `Billing options` | link | 1 | 1 | low | settings step 6 |
| `Payments` | `Receipts` | link | 2 | 1 | medium | enter_payment step 1; recent_payments step 1 |
| `Product Settings` | `Product preferences` | link | 1 |  | low | settings step 7 |
| `Products` | `Catalog` | link | 2 | 1 | medium | create_product step 1; view_products step 1 |
| `Quotes` | `Estimates` | link | 4 | 3 | medium | convert_quote_to_invoice step 2; expired_quotes step 1; and 2 more |
| `Recurring Invoices` | `Recurring billing` | link | 1 | 1 | medium | upcoming_recurring_invoices step 1 |
| `Save` | `Submit` | button | 6 | 1 | medium | create_invoice step 6; create_product step 6; and 4 more |
| `Status` | `State` | combobox (placeholder) | 2 |  | high | convert_quote_to_invoice step 3; delete_expired_quote step 2 |
| `Stop` | `Suspend` | button | 1 | 1 | low | stop_recurring_invoice step 3 |
| `User Details` | `User profile` | link | 1 |  | low | settings step 4 |
| `Workflow Settings` | `Workflow preferences` | link | 1 |  | low | settings step 9 |

Indico

| old | new | role | usable in tests | picked for tests | risk | first used in |
|---|---|---|---|---|---|---|
| `Administration` | `Control panel` | link | 1 | 1 | low | administration step 1 |
| `All events` | `Event overview` | heading | 2 | 1 | medium | clone_event step 9; favourite_lecture step 3 |
| `Back to the event` | `Return to the event` | link | 1 |  | medium | fill_out_survey step 8 |
| `Clone` | `Duplicate` | button | 1 |  | medium | clone_event step 3 |
| `Clone Event` | `Duplicate event` | dialog title | 1 |  | medium | clone_event step 3 |
| `Clone Once` | `Copy once` | radio | 1 |  | medium | clone_event step 4 |
| `Customisation` | `Personalisation` | menu section (text) | 1 |  | medium | administration step 4 |
| `Data export` | `Data download` | link | 1 |  | medium | profile_settings step 4 |
| `Delete` | `Remove` | button | 1 |  | medium | delete_conference step 3 |
| `Delete event` | `Remove event` | button | 1 | 1 | low | delete_conference step 6 |
| `Document Templates` | `Document layouts` | link | 1 |  | medium | administration step 4 |
| `Events` | `Event list` | heading | 1 |  | medium | delete_conference step 6 |
| `Favourites` | `Starred` | link | 1 | 1 | low | profile_settings step 5 |
| `Fill out` | `Take survey` | link | 1 | 1 | low | fill_out_survey step 2 |
| `Filter` | `Refine` | button (text) | 1 |  | medium | view_conference_timetable step 4 |
| `Filter options` | `View options` | row caption (cell) | 1 |  | medium | view_conference_timetable step 4 |
| `First name *` | `Given name *` | textbox | 1 |  | medium | profile_settings step 2 |
| `General Settings` | `General configuration` | heading | 1 |  | medium | administration step 1 |
| `Home` | `Main page` | link | 1 | 1 | medium | clone_event step 9 |
| `I understand what this means` | `I understand the consequences` | checkbox | 1 |  | medium | delete_conference step 4 |
| `Manage users` | `Manage accounts` | text (panel title) | 1 |  | medium | administration step 3 |
| `My profile` | `My account` | link | 1 |  | low | profile_settings step 1 |
| `Next` | `Continue` | button | 1 |  | medium | clone_event step 5 |
| `Participant List` | `Attendees` | link | 1 |  | medium | register_conference step 2 |
| `Participant list` | `Attendee list` | text (field title) | 1 |  | medium | register_conference step 4 |
| `Payment` | `Billing` | link | 1 |  | medium | administration step 6 |
| `Preferences` | `Personal options` | link | 1 |  | medium | profile_settings step 3 |
| `Register` | `Sign up` | button | 1 | 1 | low | register_conference step 7 |
| `Registration` | `Enrolment` | link | 3 |  | medium | delete_conference step 1; register_conference step 1; and 1 more |
| `Room booking` | `Reserve a room` | link | 1 | 1 | high | room_booking step 1 |
| `Search Indico` | `Find in Indico` | textbox | 1 | 1 | high | search_events step 1 |
| `Select a choice` | `Choose an option` | dropdown placeholder (text) | 1 |  | medium | register_conference step 5 |
| `Sessions` | `By session` | dropdown button (text) | 1 |  | medium | view_conference_timetable step 5 |
| `Settings` | `Account options` | link | 1 |  | medium | profile_settings step 2 |
| `Start your booking...` | `Begin your reservation...` | text (panel title) | 1 |  | medium | room_booking step 1 |
| `Submit` | `Send answers` | button | 1 |  | low | fill_out_survey step 5 |
| `Timetable` | `Schedule` | link | 1 | 1 | low | view_conference_timetable step 2 |
| `User Management` | `Account management` | menu section (text) | 1 |  | medium | administration step 2 |
| `Users` | `People` | link | 1 |  | medium | administration step 2 |
| `What should be cloned?` | `What should be copied?` | text (dialog prompt) | 1 |  | medium | clone_event step 5 |
| `Where should the events be` | `Pick the destination category` | text (dialog prompt) | 1 |  | high | clone_event step 6 |

## 10. How the files were made, and their limits

- Each snippet of each test file was parsed as Python and walked, so chained locators, escaped quotes, keyword order, either-or pairs, loops over literal lists, local helper functions, f-strings and compiled patterns are read as code, not matched as text. CSS text selectors and attribute selectors inside locator strings are read too.
- Two self-checks run on all 77 test files of the three apps: a second, independent count of locator calls by pattern matching must agree with the parse, and every label must be found on the line of the test file that `occurrences.csv` records for it. Both passed.
- No test code is copied into these files beyond labels and the names of locator kinds.
- Limits: nothing is known about text the tests do not mention, about how names are computed, or about what else is on a page. Line counts assume the generator keeps one test line per snippet line, as it did when this was written.
- To rebuild: `python -I -B tools/build_inventory.py <benchmark root> <selection.json> <this folder>`. The hand judgments are in `tools/judgments.py`.
- To re-check the written files against the test sources without the parser: `python -I -B tools/verify_outputs.py <benchmark root> <selection.json> <this folder>`. It re-reads labels from the raw files by pattern matching, repeats the rule 3 and rule 4 comparisons, and recomputes the largest possible number of distinct labels.
