#!/usr/bin/env sh
# Run a console script from the project-level .venv (Windows or POSIX layout), so git hooks
# use exactly the locked tool versions and pre-commit never builds its own environments.
# Usage: scripts/hooks/venv-run.sh <tool> [args...]
set -eu

if [ -d .venv/Scripts ]; then
  bin_dir=.venv/Scripts
elif [ -d .venv/bin ]; then
  bin_dir=.venv/bin
else
  echo "No .venv found. Create it first (see README: Setup)." >&2
  exit 1
fi

tool=$1
shift
exec "$bin_dir/$tool" "$@"
