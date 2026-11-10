#!/usr/bin/env bash
# Usage: run-with-env.sh <env-file> <cmd> [args...]
set -euo pipefail

ENV_FILE="$1"
shift

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: env file missing: $ENV_FILE" >&2
  exit 1
fi

set -a
. "$ENV_FILE"
set +a

exec "$@"

