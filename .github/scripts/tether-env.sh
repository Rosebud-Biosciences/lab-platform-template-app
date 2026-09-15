# shellcheck shell=bash
#
# Sourced by the tether-*.sh scripts: where the dataset lives, and how to run
# tether and git against it.
#
# DATASET_ROOT is the directory holding tether.toml. Default: the in-repo
# packages/dataset. Point it at a git submodule (datasets/<name>) to consume a
# dataset that lives in its own repository -- README "Where the dataset
# lives". Every tether command runs with that directory as its cwd, and every
# git command runs against the repository that CONTAINS it (`git -C`), which
# is this repo for the in-repo package and the dataset repo for a submodule.
# tether's bookmarks are branches of that containing repository, so this is
# what keeps fork/down/sweep correct in both layouts.
#
# TETHER_BIN defaults to this workspace's synced venv so a submodule with a
# pyproject of its own never redirects `uv run` to the wrong environment.

DATASET_ROOT="${DATASET_ROOT:-packages/dataset}"
TETHER_BIN="${TETHER_BIN:-$PWD/.venv/bin/tether}"

[ -f "$DATASET_ROOT/tether.toml" ] || {
  echo "tether-env: no tether.toml under DATASET_ROOT=$DATASET_ROOT" >&2
  exit 1
}
[ -x "$TETHER_BIN" ] || {
  echo "tether-env: $TETHER_BIN not found -- run uv sync --frozen first" >&2
  exit 1
}

tether() { (cd "$DATASET_ROOT" && "$TETHER_BIN" "$@"); }
dgit() { git -C "$DATASET_ROOT" "$@"; }

# An identity for the commits CI makes (pull, the merged-PR pin). Tolerant so
# the helper also loads in a jj-only checkout, where there is no .git to configure.
dgit config user.name "tether-bot" 2>/dev/null || true
dgit config user.email "tether-bot@users.noreply.github.com" 2>/dev/null || true
