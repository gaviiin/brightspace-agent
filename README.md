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

1. Open `chrome://extensions`, enable Developer mode, **Load unpacked**,
   select `extension/dist/`.
2. Open the extension popup and click **Connect to BrightSpace Agent**. Back
   in the app (<http://127.0.0.1:8730>) go to **Settings** and click
   **Approve** — the popup picks up the pairing token itself, no copying.
   (If the two can't reach each other, Settings still shows a pairing token
   you can paste into the popup's token field by hand.)
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

### Lecture recordings

Optional, and off the default install path — the download/transcription
dependencies are heavy, so they live in their own group:

```sh
make install-media        # cd backend && uv sync --group media
```

Discovery is automatic and happens in two passes, no pasting required for
either:

- **On sync**, a deterministic detector scans every synced page and link for
  a Mediasite / Zoom / Google Drive URL sitting right there in the page.
- **After sync**, the extension goes after the URLs Brightspace never
  exposes directly — a recording embedded behind an LTI launch stub, where
  the real address only materializes once a logged-in browser actually
  performs the launch. It opens each such stub in an unobtrusive background
  tab, follows the redirect to wherever it lands, reports that back, and
  closes the tab. A landing page the backend recognizes becomes a recording
  with zero typing.

Open a course's **Recordings** drawer to see what showed up, and **Process**
to transcribe it. A platform caption track is used when there is one;
otherwise the audio is downloaded and transcribed locally with
`parakeet-mlx` (Apple Silicon). The transcript becomes an ordinary material,
so re-running the pipeline files it under topics like anything else.

**Pasting is the fallback**, for whatever the two passes above can't reach —
a launch that lands somewhere the backend doesn't recognize, one that never
got a shot yet, or a background tab that got closed. The drawer names
exactly what happened per recording and offers a box to paste the real page
URL yourself (right-click the embedded player → open frame in new tab, copy
its address) — a single lecture's URL, or a channel/catalog page's, which
expands into one entry per lecture automatically.

Notes:

- **Auth:** yt-dlp reads your live Chrome cookies, so macOS shows a Keychain
  prompt the first time — choose **Always Allow** or it will ask on every
  fetch. If reading the browser profile doesn't work (Chrome running, a
  locked profile, a different browser), export a Netscape `cookies.txt` and
  point `BSA_COOKIES_FILE=/path/to/cookies.txt` at it instead.
- **Zoom passcodes:** detection picks one out of the page text when it's
  written next to the link; otherwise type it into the row and Save.
- **Extractor breakage:** platforms change their players, and a fetch that
  used to work starts failing. That's a yt-dlp version problem, not a
  passcode one: `cd backend && uv lock --upgrade-package yt-dlp && uv sync
  --group media`.
- **The group is easy to lose:** any plain `uv sync` — including the one a
  bare `uv run ...` (so `make test`) performs — re-syncs the environment to
  the default groups and uninstalls these. Rerun `make install-media`
  afterwards.

## Tests

```sh
make test    # backend (pytest) + extension + frontend (vitest), all offline
```

No API key or real tenant needed: LLM stages run against a deterministic
mock (`BSA_MOCK_LLM=1`), and `make e2e` drives a fake D2L tenant through
sync → LTI-launch autodiscovery → recordings → pipeline → graph end to end
(the media stage runs against mock fetch/transcribe backends, so no yt-dlp
or ASR is needed to run the suite). See `docs/OVERVIEW.md` for architecture
detail and `docs/plan.md` for the roadmap (an MCP server is next).

## Caveats

- Uses Brightspace's unofficial session-authenticated REST surface — the
  same routes the web UI calls. Fine for personal, local study use; check
  your school's acceptable-use policy before sharing widely. The app never
  redistributes course content, and none is committed to this repo.
- Tested against one real tenant so far; other tenants may have API
  variations (version discovery runs at handshake, and a zip-import
  fallback exists if the API surface is unavailable).

MIT licensed.
