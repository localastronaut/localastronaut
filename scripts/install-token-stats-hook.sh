#!/usr/bin/env bash
# Install the token tracker into the user's global Git hooks path.
#
# A global core.hooksPath makes Git ignore each repo's own .git/hooks. To avoid
# silently disabling local hooks (pre-commit framework, hand-written hooks,
# etc.), we install transparent "passthrough" dispatchers that chain to the
# repo-local hook of the same name. Only pre-push additionally fires the stats
# updater.
set -euo pipefail

HOOKS_PATH="$(git config --global --get core.hooksPath || true)"
if [ -z "$HOOKS_PATH" ]; then
  HOOKS_PATH="${HOME}/.git-hooks"
  git config --global core.hooksPath "$HOOKS_PATH"
fi
mkdir -p "$HOOKS_PATH"

# --- pre-push: fire the stats updater (background), then chain to local -------
cat > "${HOOKS_PATH}/pre-push" <<'HOOK'
#!/usr/bin/env bash
set -u

# Refresh the AI-stats profile in the background, unless this push IS the
# profile repo (the updater sets SKIP=1 when it pushes — prevents recursion).
if [ "${LOCALASTRONAUT_TOKEN_STATS_SKIP:-}" != "1" ]; then
  UPDATER="${HOME}/Github/Personal/localastronaut/scripts/update-token-stats.sh"
  LOG_FILE="${HOME}/.cache/localastronaut-token-stats/pre-push.log"
  if [ -x "$UPDATER" ]; then
    mkdir -p "$(dirname "$LOG_FILE")"
    ( LOCALASTRONAUT_TOKEN_STATS_SKIP=1 "$UPDATER" --commit --push ) >> "$LOG_FILE" 2>&1 &
  fi
fi

# Chain to a repo-local pre-push if one exists (stdin/args pass through).
local_hook="$(git rev-parse --git-path hooks/pre-push 2>/dev/null || true)"
if [ -n "$local_hook" ] && [ -x "$local_hook" ]; then
  case "$local_hook" in
    "$0") ;;
    *) exec "$local_hook" "$@" ;;
  esac
fi
exit 0
HOOK
chmod +x "${HOOKS_PATH}/pre-push"

# --- one passthrough dispatcher, symlinked for every other common hook --------
cat > "${HOOKS_PATH}/_passthrough" <<'DISP'
#!/usr/bin/env bash
# Transparent passthrough: run the repo-local hook of the same name, if any.
set -u
hook="$(basename "$0")"
local_hook="$(git rev-parse --git-path "hooks/${hook}" 2>/dev/null || true)"
if [ -n "$local_hook" ] && [ -x "$local_hook" ]; then
  case "$local_hook" in
    "$0") ;;
    *) exec "$local_hook" "$@" ;;
  esac
fi
exit 0
DISP
chmod +x "${HOOKS_PATH}/_passthrough"

for hook in pre-commit prepare-commit-msg commit-msg post-commit post-merge \
            post-checkout post-rewrite pre-rebase pre-merge-commit applypatch-msg \
            pre-applypatch post-applypatch; do
  ln -sf "_passthrough" "${HOOKS_PATH}/${hook}"
done

echo "installed global hooks in ${HOOKS_PATH}"
echo "  pre-push  -> stats updater + chain to local"
echo "  others    -> chain to local repo hooks (no behavior change)"
