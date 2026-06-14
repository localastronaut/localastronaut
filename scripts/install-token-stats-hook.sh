#!/usr/bin/env bash
# Install the token tracker into the user's configured global Git hooks path.
set -euo pipefail

HOOKS_PATH="$(git config --global --get core.hooksPath || true)"
if [ -z "$HOOKS_PATH" ]; then
  HOOKS_PATH="${HOME}/.git-hooks"
  git config --global core.hooksPath "$HOOKS_PATH"
fi

mkdir -p "$HOOKS_PATH"
cat > "${HOOKS_PATH}/pre-push" <<'HOOK'
#!/usr/bin/env bash
set -u

if [ "${LOCALASTRONAUT_TOKEN_STATS_SKIP:-}" = "1" ]; then
  exit 0
fi

PROFILE_REPO="${HOME}/Github/Personal/localastronaut"
UPDATER="${PROFILE_REPO}/scripts/update-token-stats.sh"
LOG_FILE="${HOME}/.cache/localastronaut-token-stats/pre-push.log"

if [ -x "$UPDATER" ]; then
  mkdir -p "$(dirname "$LOG_FILE")"
  (
    LOCALASTRONAUT_TOKEN_STATS_SKIP=1 "$UPDATER" --commit --push
  ) >> "$LOG_FILE" 2>&1 &
fi

exit 0
HOOK

chmod +x "${HOOKS_PATH}/pre-push"
echo "installed ${HOOKS_PATH}/pre-push"
