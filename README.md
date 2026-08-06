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

Other targets: `make test` (backend tests), `make e2e` (placeholder).
