# BrightSpace Agent — Technical Overview

## The problem

Course material in Brightspace (D2L) is organized the way it was *delivered* — by week, by module, by upload date. That is rarely how you want it when you study. The syllabus, three lecture slide decks, a homework set, and a textbook chapter may all cover dynamic programming, but they live in five different places and nothing connects them.

This project reorganizes a course around its **topics**. It pulls everything out of Brightspace, has an LLM pipeline work out what the course is actually about, assigns every material to the topics it teaches, and renders the result as a graph you can walk.

Milestones 1 and 3 are built: sync, organization, browsing, and the web-enrichment agent team (validated against the live API 2026-08-07; see the TIER DECISION note in `pipeline/stages/enrich.py` for the cost lesson that run taught). Lecture recordings and transcripts (M2) and an MCP server exposing the graph to other AI tools (M4) are designed but not built.

---

## Architecture

Three components, each doing the one thing it is good at:

```
Chrome — you, already logged into Brightspace
  └── MV3 extension (thin)
        │  GET /d2l/api/...   credentials: 'include'   (your live session)
        │  POST http://127.0.0.1:8730/api/ingest/...   (Bearer pairing token)
        ▼
Local backend — FastAPI on 127.0.0.1:8730
  ├── ingest      → SQLite + content-addressed blob store (~/.brightspace-agent/)
  ├── pipeline    → LangGraph: summarize → taxonomy → classify → assemble
  └── serves      → /api/* + Server-Sent Events + the built frontend
        ▲
        │  fetch /api/...  (React Query)
  React frontend — React Flow topic graph, panels, taxonomy editor
```

Everything runs on your machine. There is no server, no account, and no course content leaves the laptop except the text sent to the Anthropic API during the pipeline run.

### Why this shape

**The extension exists to borrow your session, and nothing else.** Brightspace's official API requires an OAuth app registered by a campus administrator, which a student cannot get. But the `/d2l/api/lp/...` and `/d2l/api/le/...` endpoints the Brightspace web UI itself calls accept the browser's ordinary session cookie. An extension with host permission for your school's domain can call them with `credentials: 'include'` and simply *be* you — no credentials stored, no scripted login, no MFA automation, nothing to break when the university changes its SSO pages. You stay logged in as normal; the extension rides along.

**The heavy work is not in the extension.** Manifest V3 service workers are killed after ~30 seconds idle and capped at five minutes per event, and they have no real filesystem. A multi-minute LLM pipeline and a growing corpus of course files do not belong there. So the extension is a fetcher — a few hundred lines that enumerate content and stream it to localhost — and the backend owns storage, orchestration, and every decision.

**Reading is unofficial, so the design assumes it can break.** All Brightspace endpoint knowledge is confined to one file (`extension/src/lib/d2l-client.ts`), API versions are discovered at runtime rather than hardcoded, and a zip-import fallback (`POST /api/ingest/zip`) keeps the whole pipeline usable if the API path ever closes.

---

## Sync

The extension drives the loop; the backend decides what is needed.

1. **Handshake** — discover API versions (`/d2l/api/versions/`), confirm the session (`users/whoami`), list courses (`enrollments/myenrollments/`), and register them with the backend.
2. **Table of contents** — fetch the course's content tree, hand the raw JSON to the backend. The backend stores the module structure, creates a stub row for every file topic, and returns a **needed list**: only the topics whose content it does not already have.
3. **Files** — fetch each needed file from Brightspace and upload it to `POST /api/ingest/file`, one at a time, with its source URL, title, and last-modified timestamp.
4. **Complete** — finalize the run with per-file errors and statistics.

**Incremental by construction.** A material is "needed" only if there is no stored copy, no recorded modification date, no stored content hash, or the incoming timestamp is newer. A second sync of an unchanged course transfers nothing.

**Resumable by construction.** The needed list is server-side state keyed to a sync run. If your Brightspace session expires mid-sync the extension pauses, badges "needs login", and resumes the same run against the remaining queue after you refresh the tab. A watchdog alarm restarts an interrupted sync if Chrome evicted the service worker.

**Polite by construction.** Requests are capped at two concurrent, and the client reads `X-Rate-Limit-Remaining` and `Retry-After` from every response, entering a shared cooldown when the budget runs low and backing off exponentially on a 429.

The orchestration logic lives in `sync-engine.ts` with all dependencies injected and no `chrome.*` calls anywhere, which is what makes the pause/resume behavior unit-testable. `background.ts` is a thin adapter that wires Chrome's messaging, storage, and alarms to it.

---

## Storage

Data lives in `~/.brightspace-agent/`: a SQLite database, a `blobs/` tree, `text/` sidecars, and a `config.toml` (mode 0600) holding the pairing token.

Blobs are **content-addressed** — stored at `blobs/<sha256[:2]>/<sha256>`, written to a temp file and moved atomically. Identical bytes are stored once no matter how many courses or re-syncs produce them, and the file path never derives from anything the zip or the LMS supplied.

Eleven tables carry courses, modules, materials, topics, topic edges, material-to-topic assignments, sync runs, pipeline runs, enrichment resources (M3), domain reputation (M3), and an LLM cache. `schema.sql` is the source of truth for DDL, applied through a small `PRAGMA user_version` migration runner; SQLAlchemy models mirror it for typed access.

---

## The pipeline

Four stages, wired as a LangGraph `StateGraph` and run as a background task per course. LangGraph orchestrates at stage granularity with conditional routing; the fan-out within a stage is plain `asyncio` over a semaphore.

**S1 — Summarize.** Extract text from every material (PDF via PyMuPDF, PPTX, DOCX, HTML, VTT/SRT, plain text), then ask a fast model for a short summary, a document-kind guess, and key terms. Cheap model, no tools, per-item failure isolation.

**S2 — Taxonomy.** One strong-model call reads the syllabus, the module tree, and every summary, and proposes 8–20 course topics with descriptions plus prerequisite and related edges between them. This is the [TnT-LLM](https://arxiv.org/abs/2403.12173) pattern — generate a taxonomy, then classify against it — chosen over embedding clustering, which is unreliable at the scale of a single course (tens to low hundreds of documents) and produces clusters nobody has named.

**S3 — Classify.** Each material is scored against the fixed taxonomy by a fast model: one to three topics, each with a confidence and a one-line rationale. Multi-label, because a lecture genuinely can cover two topics.

**S4 — Assemble.** Pure Python. Builds the graph payload the frontend consumes: topic nodes, material nodes, typed edges, and attachments. Anything unclassified lands in a synthetic "Unsorted" topic — a code-enforced invariant guarantees every material appears somewhere, so nothing silently vanishes.

Deliberately **not** GraphRAG. Entity extraction over every transcript sentence is expensive and unstable at this scale; a bipartite topic↔material graph with prerequisite edges is the shape education research keeps arriving at for study tools, and it is what a student actually navigates.

### Caching and versioning

This is where most of the engineering went, because the naive version either re-bills you on every sync or silently overwrites your edits.

- Every LLM call is cached on a **content hash**, so re-running is free. The taxonomy key hashes the *entire prompt* — course name, syllabus text, module titles, all summaries — which is what prevents two courses that happen to share module titles from being handed each other's topic map.
- Classification is keyed on the **taxonomy's content digest**, not its version number. A taxonomy that is byte-identical after a re-run keeps its cache; a genuinely changed one invalidates exactly what it should.
- A taxonomy proposal identical to the current one **does not bump the version and writes nothing**. Repeat runs are true no-ops.
- Corrupt cache rows are treated as misses and overwritten, so a bad row can never wedge a material permanently.
- Spend is capped per run (default $5), and a dry-run endpoint estimates cost from database state — with zero LLM calls — before you approve anything.

### Human-in-the-loop taxonomy editing

The AI's topic map is a starting point, not a verdict. The editor lets you rename, re-describe, add, delete, and merge topics, and edit the edges between them. The backend then does the least work that is correct:

- **Wording-only edits** patch in place. No new version, no re-classification, nothing re-billed.
- **Structural edits** (add, delete, merge, edge changes) mint version N+1, **carry over** every still-valid classification — merged topics map their materials to the merge target, keeping the highest-confidence assignment when several collapse into one — and then re-classify only the materials that lost their home.

And because a later pipeline run would otherwise regenerate the taxonomy and quietly discard your edits, S2 **skips** whenever the current version contains topics you authored, unless explicitly forced.

---

## Frontend

React with React Query for server state and Zustand for UI state. Three panes: an outline list, the graph, and a detail panel, all reading one shared `/graph` payload and one shared selection store, so clicking a topic in the outline and clicking it in the graph are the same action.

The graph is React Flow with a dagre layout. Topics are always visible; a topic's materials appear when you expand it, and the layout re-runs on the visible subgraph. That scoping is deliberate — the lesson from Obsidian's graph view is that a global view of everything becomes an unnavigable hairball, so this renders one course, expanded where you are looking.

Node types carry icons by material kind and flag low-confidence assignments. The detail panel shows a topic's description, its prerequisite and related edges as clickable chips, and its materials with confidence and rationale — or, for a material, its summary, key terms, topics, a link back to Brightspace, and a reader: PDFs inline, extracted text for slides and documents, download for anything else.

Pipeline progress streams over Server-Sent Events, so the graph refreshes itself as stages complete.

---

## Security posture

The threat model is a hostile web page running in the same browser, on a machine where a local service holds your course content and a pairing token.

- The backend **binds to 127.0.0.1 only** and validates the `Host` header, which is what stops DNS rebinding from turning a remote page into a same-origin caller.
- Ingest endpoints require a **pairing token** (generated on first run, mode 0600, copied into the extension once).
- Browser-facing mutating endpoints require a custom **`X-BSA-Request` header**, which forces a CORS preflight and blocks drive-by cross-origin POSTs — no random page can trigger LLM spend.
- CORS is restricted to the dev-server origin.
- Served blobs are **neutered**: only PDFs are served inline with their real type; everything else is `application/octet-stream` as an attachment, with `nosniff` and a sandbox CSP, so a hostile HTML file in a course cannot execute on the backend's origin.
- The Anthropic API key is never returned by any endpoint — the settings response carries a boolean and nothing more.

---

## Testing

367 tests: 216 backend, 39 extension, 112 frontend. Plus:

- **A fake D2L tenant** (`backend/tests/fake_d2l.py`) serving real Valence-shaped responses, with switches for 429 storms and mid-sync session expiry.
- **An offline end-to-end run** (`make e2e`) that drives sync → pipeline → graph twice against that tenant with a mock LLM backend, asserting the incremental contract: the second sync finds nothing needed, the pipeline no-ops, and the graph payload is byte-identical.
- **A Playwright smoke test** (`make e2e-ui`) clicking through the real UI against a seeded backend.
- **A mock LLM backend** that makes the entire pipeline runnable with no API key, deterministically.

Every task went through implementation, an adversarial review by a separate reviewer, a fix round, and a scoped re-review, followed by a whole-branch review before merge. That process caught, among others: a cache key that could hand one course another course's taxonomy, classifications going stale when a file changed, and a wire-format mismatch that would have failed the first real sync.

### What the suite could not catch

The first run against a real tenant found two bugs the entire suite was structurally blind to, both in the same seam:

1. The rate limiter stored the global `fetch` as an object property and called it as a method, so the browser rejected it with "Illegal invocation".
2. File uploads used a streamed request body, which Chrome only permits over HTTP/2 — against the HTTP/1.1 local backend every upload failed before leaving the browser.

Neither was reachable by the tests, because every test injects a mock `fetch` (indifferent to `this` and to HTTP versions) and the offline E2E drives a Python replica of the sync loop rather than the actual extension. The lesson is specific and worth acting on: **a browser-driven E2E of the real extension against the fake tenant** is the missing layer, and it should exist before anyone else installs this.

---

## Status and what is next

**Working:** sync, storage, the full pipeline, the graph UI, the taxonomy editor, offline E2E. **In progress:** first validation against a real tenant — sync works; the pipeline has yet to run against real course material, and the prompts have never been read against real output.

**M2 — Lecture media and transcripts.** Recordings live outside Brightspace on Panopto, Kaltura, Echo360, or YuJa, embedded over LTI. The plan is to capture player URLs during sync, prefer the platform's own caption track, and fall back to local Whisper transcription on Apple Silicon. Transcripts then flow through the existing pipeline unchanged — which is the payoff of treating everything as a "material".

**M3 — Web enrichment.** A per-topic research team rather than a single search call: a planner that grounds queries in the course's own terminology, parallel finders each pursuing a different intent (alternative explanation, video, practice problems, visualization), a verifier that actually fetches each candidate to confirm it is live, on-topic, and level-appropriate, a judge that scores against a rubric with a format-diversity constraint, and a feedback loop where your keep/dismiss decisions bias future results.

**M4 — MCP server.** Expose the organized graph over the Model Context Protocol so any MCP client can ask questions of your course material.

---

## Running it

```bash
make install                                    # uv sync + pnpm install
ANTHROPIC_API_KEY=sk-ant-... make backend       # 127.0.0.1:8730
make frontend                                   # Vite dev server on :5173
make ext                                        # build extension/dist
```

Load `extension/dist` unpacked at `chrome://extensions`, copy the pairing token from the app's Settings page into the popup, connect it to your school's Brightspace origin, discover courses, and sync one. Then run the pipeline from the course workspace — you will be shown a cost estimate before anything is spent.

`make test` runs all three suites; `make e2e` runs the offline end-to-end with no network and no API key.

---

## A note on the API path

Session-cookie access to `/d2l/api/...` is unofficial. It works because the Brightspace web UI depends on those same routes, but it is unsupported, undocumented, and could change without notice. This is built for local, personal study use of your own course material — not for redistribution, and worth a glance at your institution's acceptable-use policy before sharing it around.
