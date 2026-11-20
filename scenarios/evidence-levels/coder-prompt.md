You are coding a covariate for a research study, independently of any other coder. Work only inside this folder: do not open files outside it, do not search the web, do not run code.

coder.csv has one row per scenario (app, task, payload, level, confidence, note). For each row, cases/<app>__<task>/ holds two files:
- test.py: a scripted end-to-end web test, exactly as a coding agent will see it.
- bug.js: a script that injects a bug into the web application while the test runs.

Read both files and decide what evidence of the bug the test itself provides. Pick one level:
- self: the bug contradicts something the test itself typed, created or did. A reader holding only the test can see that the page is wrong.
- fixture: the bug changes data that existed before the test started and that the test never touched. From the test alone, a regression and a changed fixture look the same.
- control: a control or heading that the test needs is removed.
If a scenario fits none or two of them, pick the closest and say why in note.

Fill level (self, fixture or control), confidence (high, medium or low), and note (one sentence) when the choice was hard. Leave app, task and payload unchanged. Judge each row on its own; do not balance the counts. When done, check that every row has a level and a confidence, and report the count per level.
