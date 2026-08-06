#!/usr/bin/env bash
# `make e2e-ui`: seed a real (offline, mocked-LLM) backend via scripts/e2e.py
# --seed-only --keep-running, then run the Playwright smoke spec against it
# (frontend/e2e/smoke.spec.ts, via frontend/playwright.config.ts's own
# webServer, which boots the Vite dev server). Tears the seed process down
# on exit either way and propagates Playwright's exit code.
set -u -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_URL="http://127.0.0.1:8730/api/health"
SEED_MARKER="scripts/e2e.py --seed-only --keep-running"
SEED_PID=""

# `kill "$SEED_PID"` alone isn't enough here: SEED_PID is the PID of the
# `( cd ... && uv run ... )` subshell, and `uv run` forks its own child
# python process rather than exec'ing into it -- a plain kill never reaches
# that child, leaking a server bound to 127.0.0.1:8730/9799. Matching on
# the actual command line with pkill reaches it regardless of how many
# process layers are in between.
cleanup() {
  pkill -TERM -f "$SEED_MARKER" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Playwright needs Chromium downloaded once (`pnpm exec playwright install
# chromium`, documented in the README) -- checked here via the officially
# supported way to ask playwright-core where it expects the binary, so a
# missing browser fails fast with a clear hint instead of a confusing
# mid-test crash. Never auto-installs.
CHROMIUM_PATH="$(cd "$REPO_ROOT/frontend" && pnpm exec node -e "
const p = require.resolve('playwright-core', { paths: [require.resolve('@playwright/test')] });
console.log(require(p).chromium.executablePath());
" 2>/dev/null)"
if [ -z "$CHROMIUM_PATH" ] || [ ! -f "$CHROMIUM_PATH" ]; then
  echo "error: Playwright's Chromium isn't installed." >&2
  echo "  Run this once:  pnpm --dir frontend exec playwright install chromium" >&2
  exit 1
fi

echo "[e2e-ui] seeding backend (scripts/e2e.py --seed-only --keep-running)…"
( cd "$REPO_ROOT/backend" && uv run python ../scripts/e2e.py --seed-only --keep-running ) &
SEED_PID=$!

echo "[e2e-ui] waiting for backend at $BACKEND_URL…"
for _ in $(seq 1 100); do
  if curl -fsS "$BACKEND_URL" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$SEED_PID" 2>/dev/null; then
    echo "error: seed process exited before the backend came up (see output above)." >&2
    exit 1
  fi
  sleep 0.2
done
if ! curl -fsS "$BACKEND_URL" >/dev/null 2>&1; then
  echo "error: backend never became healthy at $BACKEND_URL" >&2
  exit 1
fi
echo "[e2e-ui] backend is up; running Playwright…"

( cd "$REPO_ROOT/frontend" && pnpm exec playwright test )
STATUS=$?

exit $STATUS
