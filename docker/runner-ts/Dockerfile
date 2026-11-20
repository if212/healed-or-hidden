# The TypeScript runner image of the fidelity check (design doc 5.6): the Python runner image
# (docker/runner) with Node.js, Playwright Test 1.65.1 as src/pwfix/package-lock.json locks it,
# the fixture package pwfix (src/pwfix/) and the TypeScript entry (src/pwrun/). The fidelity
# check's stacks run it as their `runner` service (hoh.fidelity.stack); the shadow oracle stays
# the Python runner. The official healer's MCP server runs in it too.
#
# Build it from the repository root, after the Python runner image; the build context is src/,
# and docker/runner-ts/Dockerfile.dockerignore lets in only what this file copies:
#   uv run python -m hoh.fidelity.stack build-runner-ts
# The image's label holds the digest of the code it was built from (src/pwfix/, src/pwrun/ and
# the Python runner's), and a run refuses an image built from other code: rebuild after any
# change there.

ARG BASE=hoh/runner:pw1.60.0

# Node.js, and the locked packages without their install scripts: Playwright Test connects to
# the stack's browser and needs no browser of its own.
FROM node:22.22.1-bookworm-slim AS packages
WORKDIR /pwfix
COPY pwfix/package.json pwfix/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund

FROM ${BASE}

COPY --from=packages /usr/local/bin/node /usr/local/bin/node
COPY --from=packages /pwfix/node_modules/ /opt/node/pwfix/node_modules/
COPY pwfix/ /opt/node/pwfix/
# The page script of the wait, byte for byte the Python runner's: pwfix/lib/quiet.js reads it
# from the package's directory.
COPY hoh_runner/quiet.js /opt/node/pwfix/quiet.js
COPY pwrun/ /opt/runner/pwrun/

# `import ... from 'pwfix'` in a test resolves to the package, and the package's own
# Playwright Test is the one that runs the tests (pwrun.entry sets the same).
ENV NODE_PATH=/opt/node:/opt/node/pwfix/node_modules

WORKDIR /work
