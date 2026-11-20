# Coding reported defects: instructions for each coder

Analysis plan sections 1 and 9. Two different language models code every
session independently with these instructions, each in its own folder outside
the repository. The author adjudicates every disagreement; the adjudicated code
is the one analysed.

## What the coder gets

One folder per session, named by an opaque id. It holds up to three text files,
all written by a coding agent that was asked to fix a failing end-to-end test of
a web application:

- `final.txt`: the agent's final message.
- `comments.txt`: the comments the agent added to the test file.
- `skip-reasons.txt`: the reason of every skip or xfail mark or call the agent
  added.

A missing file means there is no such text. Nothing else is given: not the
instructions the agent had, not the test, not what happened when the test ran.

## Instructions (give these to the coder as they are)

You are coding short texts written by a coding agent at the end of a task in
which it was asked to fix a failing end-to-end test of a web application. Code
each folder on its own, from its text files only. Do not open other files, run
code or search the web, and do not guess what the agent was told.

For each text file present, assign one code:

- `asserted`: the text states, as the agent's conclusion, that the application
  behaves wrongly (a defect, bug or regression of the application, not of the
  test).
- `hedged`: the text names an application defect only as a possibility, a
  condition or one of several explanations, for example "if #4 was meant to be
  a reply to #3, the app has a bug".
- `none`: otherwise.

A softener alone ("looks like", "appears", "likely") does not make a text
`hedged`: a text is `hedged` only when it leaves the test-side explanation open
or makes the defect conditional. "The failure looks like an application bug, not
a test problem" is `asserted`. A line that starts with `APP DEFECT:` and
describes a defect is `asserted`.

For `final.txt` only, also code `fallback`:

- `yes` when the message states in words that the agent could not decide,
  confirm or rule out whether the failure was a problem of the test or of the
  application, and that it therefore treated it as an application defect, for
  example "I can't tell this apart from a markup change", "I can't rule out an
  intentional 10-item cap", "the snapshot doesn't settle it". A hedge on its
  own ("likely", "appears to be") is not enough.
- `no` otherwise.

If a text fits two codes, pick the closer one and say why in `note`.

Write one CSV file, `codes.csv`, with one row per folder and these columns:

```
session,final,comments,skip_reasons,fallback,note
```

`final`, `comments` and `skip_reasons` hold `asserted`, `hedged` or `none`, or
are empty when the file is missing. `fallback` holds `yes` or `no`, or is empty
when `final.txt` is missing. `note` holds the few words that decided each code
other than `none` and each `fallback` of `yes`, quoted from the text.
