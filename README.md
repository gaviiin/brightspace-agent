# BrightSpace Agent

Turn a Brightspace (D2L) course into a browsable topic graph — with an agent
team that finds you supplementary material from the open web.

A thin Chrome extension rides your normal logged-in Brightspace session and
syncs course content (slides, PDFs, assignments, pages) to a local Python
backend. An LLM pipeline summarizes every document, derives the course's
topic taxonomy, and files each material under its topics. A React frontend
renders the result as an interactive graph: click a topic, see everything the
course has on it — plus a "Supplementary" section where a plan → search →
verify → judge agent team suggests vetted external resources (lecture videos,
other universities' notes, practice problems, visualizers) that you can keep
or dismiss.

Everything runs locally. Course content never leaves your machine except as
LLM calls to the Anthropic API (summaries/classification) — and web searches
send only topic names and derived queries, never your course files.

## Prerequisites

- macOS/Linux with [uv](https://docs.astral.sh/uv/) and [pnpm](https://pnpm.io/)
- Chrome (the extension is loaded unpacked; no Web Store install)
- An [Anthropic API key](https://console.anthropic.com/) for the LLM pipeline
  (sync works without one; set `BSA_MOCK_LLM=1` to try everything offline)

## Quickstart

```sh
# 1. Install
cd backend && uv sync && cd ..
pnpm install

# 2. Build the frontend and extension
pnpm --dir frontend build
make ext

# 3. Start the backend (serves the built frontend too)
cd backend && ANTHROPIC_API_KEY=sk-ant-... uv run brightspace-agent
```

Then:

1. Open <http://127.0.0.1:8730>, go to **Settings**, copy the pairing token.
2. Open `chrome://extensions`, enable Developer mode, **Load unpacked**,
   select `extension/dist/`. Paste the pairing token into the extension popup.
3. Log into your school's Brightspace in a normal tab, hit **Discover** in
   the popup, pick a course, **Sync**.
4. Back in the web app: open the course, **Run pipeline**. You get a cost
   estimate to confirm first (a ~120-material course runs about $1).
5. Click any topic → **Find supplementary materials** to send the research
   agents out (~$1/topic, also estimated up front and capped).

For development, `make backend` / `make frontend` run the two halves with
hot reload (Vite on :5173 proxies to the backend).

## How it works

- **Extension** (`extension/`): MV3, no scraping — it calls the same
  `/d2l/api/*` REST endpoints the Brightspace UI itself uses, with your
  session cookies, honoring rate-limit headers. All D2L knowledge lives in
  one file (`src/lib/d2l-client.ts`).
- **Backend** (`backend/`): FastAPI + SQLite + a content-addressed blob
  store in `~/.brightspace-agent/`. The pipeline is a LangGraph state
  machine: summarize (haiku) → taxonomy (sonnet) → classify (haiku) →
  assemble. Every LLM result is cached by content hash, so re-syncs and
  re-runs only pay for what changed.
- **Enrichment** (`pipeline/stages/enrich.py`): per topic, a planner grounds
  search intents in your actual materials; finders run Anthropic server-side
  web search; verifiers fetch every candidate URL and demand verbatim
  evidence from the page; a judge ranks with a format-diversity constraint.
  Your keep/dismiss decisions feed a per-domain reputation that biases
  future runs.
- **Frontend** (`frontend/`): React Flow + dagre graph, outline and detail
  panels, taxonomy editor, run history with per-stage costs.

Costs are estimated before every paid action, counted per run (including
web-search fees), and hard-capped (`BSA_MAX_COST_USD_PER_RUN`, default $5).

## Tests

```sh
make test    # backend (pytest) + extension + frontend (vitest), all offline
```

No API key or real tenant needed: LLM stages run against a deterministic
mock (`BSA_MOCK_LLM=1`), and `make e2e` drives a fake D2L tenant through
sync → pipeline → graph end to end. See `docs/OVERVIEW.md` for architecture
detail and `docs/plan.md` for the roadmap (lecture-recording transcripts and
an MCP server are next).

## Caveats

- Uses Brightspace's unofficial session-authenticated REST surface — the
  same routes the web UI calls. Fine for personal, local study use; check
  your school's acceptable-use policy before sharing widely. The app never
  redistributes course content, and none is committed to this repo.
- Tested against one real tenant so far; other tenants may have API
  variations (version discovery runs at handshake, and a zip-import
  fallback exists if the API surface is unavailable).

MIT licensed.
