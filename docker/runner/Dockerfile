# The runner image: Playwright for Python, pytest and the runner package (src/hoh_runner/),
# the only code in the containers that run a test or the shadow oracle. Every assembled
# stack's `runner` and `oracle` services use it under the name hoh/runner:pw1.60.0
# (hoh.envs) and give it its command, so it has none of its own.
#
# Build it from the repository root; the build context is src/:
#   uv run python -m hoh.docker_backend build-runner
#   (the same as: docker build --tag hoh/runner:pw1.60.0 --file docker/runner/Dockerfile src)
# The package is copied in at build time: rebuild after any change under src/hoh_runner/.

FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

# The base image has the browsers and their libraries, but not the Python package.
# Playwright as in the benchmark's browser image; pytest, and every package either of them
# pulls in, at the versions uv.lock holds (tests/test_docker_backend.py checks the list), so
# that a rebuild later installs the same code.
RUN python3 -m pip install --no-cache-dir --root-user-action=ignore \
        playwright==1.60.0 \
        greenlet==3.5.6 \
        pyee==13.0.1 \
        typing-extensions==4.16.0 \
        pytest==9.2.0 \
        iniconfig==2.3.1 \
        packaging==26.3 \
        pluggy==1.7.0 \
        pygments==2.21.0

COPY hoh_runner/ /opt/runner/hoh_runner/

ENV PYTHONPATH=/opt/runner \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /work
