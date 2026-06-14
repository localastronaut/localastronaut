#!/usr/bin/env bash
# Generate, optionally commit, and optionally push the profile token stats.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROFILE_REPO="$(cd -- "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="${HOME}/.cache/localastronaut-token-stats"
LOCK_DIR="${LOG_DIR}/update.lock"
COMMIT=0
PUSH=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --commit) COMMIT=1 ;;
    --push) PUSH=1 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

mkdir -p "$LOG_DIR"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[token-stats] update already running"
  exit 0
fi
trap 'rmdir "$LOCK_DIR"' EXIT

cd "$PROFILE_REPO"
python3 scripts/generate-token-stats.py \
  --output ai-token-stats.svg \
  --summary data/token-stats.json \
  --manual data/manual-usage.json

if [ "$COMMIT" -eq 0 ]; then
  exit 0
fi

if [ -z "$(git status --porcelain -- README.md ai-token-stats.svg data/token-stats.json)" ]; then
  echo "[token-stats] no profile stats changes"
  exit 0
fi

git add README.md ai-token-stats.svg data/token-stats.json
git commit -m "chore: update AI token stats [skip ci]"

if [ "$PUSH" -eq 1 ]; then
  LOCALASTRONAUT_TOKEN_STATS_SKIP=1 git push origin main
fi
