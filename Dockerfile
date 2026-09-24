# Per-package image, parameterized by TARGET_PACKAGE (a name from
# deployables.json) so each deployable gets its own slim artifact from the one
# workspace/lockfile. A package needing a different recipe can override this
# file with its own packages/<name>/Dockerfile (CI picks it up). Named
# Dockerfile (not Containerfile) so Dependabot's docker ecosystem sees it;
# podman builds either name.
#
#   app        podman build --build-arg TARGET_PACKAGE=app .
#              -> default CMD: uvicorn on :8080 (the workloads module contract)
#   workflows  podman build --build-arg TARGET_PACKAGE=workflows .
#              -> no meaningful CMD: the Dagster chart injects `dagster api grpc
#                 --python-file /opt/dagster/app/repo.py`, and migrations run it
#                 with `alembic -c packages/db/alembic.ini upgrade head`
#                 (workflows carries db[migrate])
#   GPU        podman build --build-arg TARGET_PACKAGE=workflows \
#                --build-arg BASE_IMAGE=nvidia/cuda:12.8.1-runtime-ubuntu24.04 \
#                --build-arg EXTRA_DEPENDENCIES="--extra gpu" \
#                --build-arg GPU_DEPENDENCIES="torch" .
#              -> any base works: uv provisions the pinned Python itself
ARG BASE_IMAGE=python:3.14-slim
ARG UV_VERSION=0.11.3

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# --- Stage: resolve pinned GPU wheel versions from the lockfile ---
# Extract the exact pins (from uv.lock) for the packages named in
# GPU_DEPENDENCIES into a tiny requirements file. The heavy GPU install layer in
# the main stage is cached on the *content* of this file, so it only rebuilds
# when the torch pin actually changes -- not on every uv.lock edit. Reading
# versions from the lock avoids drift and any double-install.
FROM ${BASE_IMAGE} AS gpu-lock
ARG GPU_DEPENDENCIES=""
COPY uv.lock /tmp/uv.lock
RUN set -eu; \
    : > /tmp/gpu-requirements.txt; \
    for pkg in ${GPU_DEPENDENCIES}; do \
        awk -v pkg="$pkg" -F' = ' ' \
            /^\[\[package\]\]/ {name=""; ver=""} \
            $1=="name" {name=$2; gsub(/"/, "", name)} \
            $1=="version" {ver=$2; gsub(/"/, "", ver); if (name==pkg) print pkg "==" ver} \
        ' /tmp/uv.lock | head -n 1 >> /tmp/gpu-requirements.txt; \
    done; \
    cat /tmp/gpu-requirements.txt

# --- Stage: collect every package manifest for the dependency layer ---
# The dependency layer below needs every workspace package's pyproject.toml, so
# gather them all instead of bind-mounting a hardcoded list. This stage re-runs
# on any source change, but the downstream mount is keyed on the *content* of
# the surviving pyprojects, so the dependency layer only rebuilds when one
# changes.
FROM ${BASE_IMAGE} AS manifests
WORKDIR /manifests
COPY packages ./packages
RUN find packages -mindepth 2 -maxdepth 2 ! -name pyproject.toml -exec rm -rf {} +

FROM ${BASE_IMAGE}

COPY --from=uv /uv /uvx /usr/local/bin/

# Compile during build instead of runtime; copy out of the cache mount.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN useradd --create-home --uid 1000 appuser

WORKDIR /app

# Install bulky GPU dependencies (torch + bundled CUDA wheels) in a dedicated
# layer, using the exact versions resolved from uv.lock by the gpu-lock stage.
# This layer only rebuilds/re-pushes when those pins change, not on every
# dependency edit. The file is empty for CPU builds (no-op), and the later
# `uv sync` finds these already satisfied for GPU builds.
COPY --from=gpu-lock /tmp/gpu-requirements.txt /tmp/gpu-requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=.python-version,target=.python-version \
    if [ -s /tmp/gpu-requirements.txt ]; then \
        uv venv && uv pip install -r /tmp/gpu-requirements.txt; \
    fi

# Third-party dependency layer (cached until the lockfile or a manifest
# changes). Only TARGET_PACKAGE's subtree of the workspace is installed.
ARG TARGET_PACKAGE=app
ARG EXTRA_DEPENDENCIES=""
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=.python-version,target=.python-version \
    --mount=type=bind,from=manifests,source=/manifests/packages,target=packages \
    uv sync --frozen --no-editable --no-dev --no-install-workspace \
      --package ${TARGET_PACKAGE} ${EXTRA_DEPENDENCIES}

# Copy the source code
COPY --chown=1000:1000 . /app

# Dagster user-code entry point (see header); harmless in the app image.
COPY packages/workflows/repo.py /opt/dagster/app/repo.py
# The chart's DAGSTER_HOME, where run pods keep Dagster's telemetry id and
# local artifact storage: it must be writable by appuser, not root's.
RUN install -d -o 1000 -g 1000 /opt/dagster/dagster_home

# Install the workspace packages themselves.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable --no-dev \
      --package ${TARGET_PACKAGE} ${EXTRA_DEPENDENCIES}

ENV PATH="/app/.venv/bin:$PATH"

USER appuser
EXPOSE 8080

# Meaningful for the app image only; every other consumer injects its command.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
