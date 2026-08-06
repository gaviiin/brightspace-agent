# BrightSpace Agent

A course-material organizer: a thin Chrome extension syncs your Brightspace
(D2L) course content to a local Python backend, which organizes it into a
topic graph you can browse from a React frontend.

## Install

```sh
# backend (Python, via uv)
cd backend && uv sync

# frontend + extension (pnpm workspaces, from repo root)
pnpm install
```

## Run

```sh
make backend    # starts the FastAPI backend on 127.0.0.1:8730
make frontend   # starts the Vite dev server on :5173
make ext        # builds the extension to extension/dist/
```

Load the extension: open `chrome://extensions`, enable Developer mode,
"Load unpacked", select `extension/dist/`.

Other targets: `make test` (backend + extension + frontend unit tests).

## Offline E2E

No real Brightspace tenant needed -- `make e2e` drives a fake D2L tenant
(`backend/tests/fake_d2l.py`) and the real backend end to end (sync ->
pipeline -> graph, twice, to prove incremental sync and the no-op re-run),
entirely offline (`BSA_MOCK_LLM=1`).

```sh
pnpm --dir frontend exec playwright install chromium   # one-time
make e2e                                                # backend + fake D2L only
make e2e-ui                                             # + a Playwright smoke test against the real frontend
```
