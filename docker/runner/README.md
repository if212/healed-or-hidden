# Runner image

`hoh/runner:pw1.60.0` runs a test, and after it the shadow oracle, inside every stack that
`hoh.envs` assembles: the stack's `runner` and `oracle` services use it and give it its
command. It holds:

- Playwright for Python 1.60.0, the version of the benchmark's browser image;
- pytest 9.2.0, the version in `uv.lock`;
- the runner package, `src/hoh_runner/`, at `/opt/runner/hoh_runner` with `/opt/runner` on
  `PYTHONPATH`. Its working directory is `/work`.

The build context is `src/`, so `.env`, `build/` and the benchmark never reach the builder.
The package is copied in at build time: **rebuild after any change under
`src/hoh_runner/`**. `tests/test_docker_backend.py` checks, without Docker, that the pins
agree with `uv.lock` and that the image name is the one the stacks use.

## Build and check it on the Mac

From the repository root, with Docker Desktop running (Rosetta on: BookStack's app image is
amd64 only), the submodule checked out and `uv sync` done:

```bash
uv run python -m hoh.docker_backend build-runner
docker run --rm hoh/runner:pw1.60.0 python3 -c "import importlib.metadata as m, hoh_runner.entry; print(m.version('playwright'), m.version('pytest'))"
uv run python -m hoh.envs build --app bookstack
uv run pytest -o addopts="" tests/test_docker_backend.py -k real -v -rs
```

1. Builds the image. The same as
   `docker build --tag hoh/runner:pw1.60.0 --file docker/runner/Dockerfile src`.
2. Prints `1.60.0 9.2.0`: both pins took, and the runner package imports.
3. Builds the browser and BookStack app images, once (skip it if they exist).
4. Runs the generated BookStack test `count_recently_created_books` through
   `hoh.docker_backend.DockerBackend` on two fresh stacks (project `hoh-check-1`). Expect
   `test_real_run_on_bookstack[A] PASSED` (green on the clean app) and
   `test_real_run_on_bookstack[A+M] PASSED` (red at the final assertion with the
   benchmark's change, and the shadow oracle failing at the same assertion). Each takes a
   few minutes, most of it BookStack starting under emulation. `SKIPPED` gives the reason:
   no daemon, or an image not built. A failure names the run's directory, which holds
   `environment.log`, `runner-console.log`, `results/output.log` and `backend.json`.
