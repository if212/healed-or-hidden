# Source of the pinned Playwright healer definition

`playwright-test-healer.agent.md` in this directory is a byte-for-byte copy of
one file from the Playwright repository. Arm H of the study starts from its
text.

| | |
|---|---|
| Repository | https://github.com/microsoft/playwright |
| Tag | `v1.65.1`, the latest `v1.*` release tag on the retrieval date |
| Commit | `bbd948344ea9a60bf1e6f125c02ca99b2b51593b` |
| Path | `packages/playwright/src/agents/playwright-test-healer.agent.md` |
| License | Apache-2.0 (full text in `LICENSE`, attribution in `NOTICE`) |
| Retrieved | 2026-11-14 |
| Size | 2891 bytes, 56 lines, ASCII, LF line endings |
| Git blob | `79d5a94b0580dcbcffb4e1ba72d528b0767a4bf2` |
| SHA-256 | `b3e1d85219df5ead7e3ef0dc69959b0263cc16e885f71d5b64bafdf97e6f9215` |

On the retrieval date the file was the same blob on `main` at commit
`b4d016aadc5cbd2b898a284307975fe73e05483d` (version `1.66.0-next`).

## How it was retrieved

In a clone of the repository:

```bash
git ls-remote --tags origin 'v1.*'    # highest tag without a suffix: v1.65.1
git fetch --depth 1 origin tag v1.65.1
git show v1.65.1:packages/playwright/src/agents/playwright-test-healer.agent.md > playwright-test-healer.agent.md
```

To check the copy, run
`git hash-object third_party/playwright-healer/playwright-test-healer.agent.md`
in this repository. It prints the Git blob id above. `tests/test_prompts.py`
checks both hashes against this file.

## Do not edit the copy

Every change this study makes to the upstream text is an entry in an edit file
under `prompts/`, with its reason:

- `prompts/h_adaptation.yaml`: upstream body to arm H.
- `prompts/g_from_h.yaml`: arm H to arm G.
- `prompts/h_noskip_from_h.yaml`: arm H to arm H_noskip.

`src/hoh/prompts.py` drops the YAML front matter, applies the edits and writes
the assembled prompts to `build/prompts/`, which is not committed.

This directory and `prompts/` are frozen together with the analysis plan
(`docs/analysis-plan.md`, section 5). To move to a newer upstream version
before that, replace the copy and update the table above. An edit whose `old`
text no longer occurs exactly once stops the build. Upstream changes outside
the edited text flow into the arms as they are, so read the upstream diff
before using the new prompts.
