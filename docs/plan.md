# BrightSpace Agent — Implementation Plan

## Context

Gavin (a student) wants an agentic app that organizes all Brightspace (D2L) course materials — lecture recordings, transcripts, homework, slides, syllabus, textbook chapters — into topic-based groups presented as a traversable graph UI: each topic node carries its relevant materials, plus AI agents that find good supplementary materials on the web. Shareable with classmates (simple install, no hosted service). Greenfield in `/Users/gavinzhou/Desktop/BrightSpace Agent` (empty, not yet git).

### Why this architecture (research findings, verified Aug 2026)

- **Official Brightspace OAuth API is admin-gated** — students cannot register apps. But `/d2l/api/lp|le/...` REST endpoints accept the browser's own session (cookies `d2lSessionVal`/`d2lSecureSessionVal`, or Bearer JWTs from `localStorage['D2L.Fetch.Tokens']`). Every successful student tool (RohanMuppa/brightspace-mcp-server, Aaryan-Kapoor/d2l-cli, byuitechops/d2l-login) uses this path. **No DOM scraping.**
- **Pure Chrome extension is the wrong host for agents**: MV3 service workers die after ~30s idle / 5-min event caps; no real filesystem. The proven pattern (Tasks for Canvas, LearnlyAI) is a **thin MV3 extension riding the user's live logged-in session** (`fetch` with `credentials:'include'` + host_permissions) streaming to a **local backend** — this eliminates SSO/Duo automation entirely. Playwright-scripted login rejected as primary path (recurring MFA friction, breakage).
- **Lecture video lives outside D2L** (Panopto/Kaltura/Echo360/YuJa via LTI). yt-dlp has cookie-auth extractors for Panopto (DeliveryInfo.aspx → HLS/MP4 + captions) and Kaltura (partnerId/entryId → playManifest + captionAsset.serveWebVTT). Prefer platform VTT transcripts; fallback = local Whisper (mlx-whisper large-v3-turbo, ~6GB RAM, 5–9× realtime on Apple Silicon).
- **Topic organization**: TnT-LLM pattern (Microsoft, KDD 2024) — LLM proposes taxonomy from syllabus + module structure + doc summaries, then cheap models classify each material against it. Beats embeddings clustering (BERTopic/HDBSCAN unreliable at course-sized corpora). **No GraphRAG** — a typed bipartite topic↔material graph + topic→topic prerequisite/related edges is the education-proven shape.
- **Graph UI**: React Flow (@xyflow/react, MIT) + dagre. Scoped per-course graphs with expand/collapse (Obsidian lesson: global graphs become hairballs); typed nodes; click → detail panel; outline view alongside. React Flow's expand/collapse example is Pro-paywalled — reimplement via filter-visible → dagre relayout → fitView.
- **Rate limits**: token-bucket; honor `X-Rate-Limit-Remaining`, `X-Request-Cost`, `Retry-After`; naive bulk sync gets 429'd.
- **Gap confirmed**: no existing tool (LearnlyAI, StudyFetch, NotebookLM, Atlas, Recall, D2L's own Lumi) combines auto-LMS-ingestion + topic graph + web enrichment.

### User decisions (locked)
- Audience: personal + shareable with classmates (unpacked extension, `uv run` backend)
- Backend: **Python**; Frontend: **React + TS**; Extension: TS
- Agent runtime: **LangGraph** (`langgraph` + `langchain-anthropic`) — user choice Aug 2026: the most widespread/transferable agent framework, model-agnostic, best for learning. Claude models via user-supplied API key; sonnet for taxonomy/judging, haiku for summarize/classify fan-out; structured outputs via `.with_structured_output(PydanticModel)` at every stage boundary; optional LangSmith tracing (env vars) for observability
- Milestones: **M1 ingest + topic graph → M2 lecture media/transcripts → M3 web enrichment**
- Recordings platform unknown/mixed → platform-agnostic capture design

## Architecture

```
Chrome (user logged into Brightspace normally)
  └─ Thin MV3 extension: enumerates courses/ToC, streams files
       │  fetch with credentials:'include' against /d2l/api/*  (adaptive rate limiter)
       ▼  Authorization: Bearer <pairing token>
Local Python backend (FastAPI, 127.0.0.1:8730)
  ├─ Ingest API → SQLite + content-addressed blob store (~/.brightspace-agent/)
  ├─ Pipeline (asyncio): S1 summarize → S2 taxonomy → S3 classify → S4 graph build → S5 enrich
  │    (LangGraph StateGraph; llm_cache keyed on sha256+prompt_version = idempotent re-runs)
  └─ Serves React frontend (built) + /api/* + SSE events
```

## Tooling (one pick each)

| Choice | Pick |
|---|---|
| Python packaging | uv (`uv sync && uv run brightspace-agent` is the install story) |
| Backend | FastAPI + uvicorn (async fan-out, Pydantic-native, SSE) |
| Storage | SQLite + SQLAlchemy 2.0; hand-rolled migrations (`schema.sql` + `PRAGMA user_version`) |
| JS | pnpm workspaces (frontend, extension) |
| Frontend | Vite + React + TS, TanStack Query + Zustand, Tailwind, @xyflow/react + @dagrejs/dagre, lucide-react |
| Extension build | Plain Vite multi-entry + static manifest.json (no WXT/CRXJS) |
| Task runner | Makefile (`make backend/frontend/ext/dev/e2e`) |

## Repo Layout

```
extension/
  manifest.json                    # MV3; host_permissions: school D2L origin + http://127.0.0.1:8730/*
  src/background.ts                # sync loop driver
  src/content/token-capture.ts     # localStorage JWT fallback (only if cookie auth 401s)
  src/popup/                       # pairing token entry, course picker, sync progress
  src/lib/d2l-client.ts            # ALL D2L endpoint/auth/rate-limit knowledge (the tenant-quirk file)
  src/lib/backend-client.ts        # localhost API + pairing token
  src/lib/types.ts                 # mirrors backend Pydantic 1:1
backend/src/brightspace_agent/
  main.py config.py                # app factory, CLI entry; pydantic-settings (data dir, port, key, cost caps)
  db/{models,session,migrate}.py db/schema.sql
  api/{ingest,courses,graph,materials,taxonomy,pipeline,events}.py
  ingest/{store,extract,diff}.py   # blob store (sha256), pdf/pptx/docx/html/vtt→text, ToC-vs-DB diff
  pipeline/graph.py                # LangGraph StateGraph wiring (nodes = stages, Send API fan-out)
  pipeline/runner.py pipeline/stages/{summarize,taxonomy,classify,assemble,enrich}.py
  agents/{llm,schemas}.py agents/prompts/*.md   # tiered ChatAnthropic factories, structured-output helper, retry, usage/cost tracking
  media/{ytdlp,whisper,capture}.py # M2
  graph/build.py                   # deterministic S4 assembly → graph JSON
backend/tests/                     # + fixtures/d2l/, fixtures/llm/, fake_d2l.py
frontend/src/
  pages/{CourseListPage,CourseWorkspacePage,SettingsPage}.tsx
  graph/{GraphView.tsx,transform.ts,layout.ts,nodes/,edges.ts}
  panels/{OutlinePanel,DetailPanel,MaterialReader,TaxonomyEditor}.tsx
  api/{client,types}.ts state/uiStore.ts
```

Deps (backend): fastapi uvicorn sqlalchemy pydantic-settings langgraph langchain-anthropic langchain-core pymupdf python-pptx python-docx beautifulsoup4 httpx sse-starlette.

## Data Model

Data dir `~/.brightspace-agent/` (override `BSA_DATA_DIR`): `brightspace.db`, `blobs/<sha256[:2]>/<sha256>`, `text/<sha256>.txt`, `media/`, `config.toml` (chmod 600: API key + pairing token — never in DB/frontend).

Tables: `courses` (d2l_org_unit_id, tenant_origin, toc_json cache, taxonomy_version), `modules` (structural prior), `materials` (kind: syllabus|slides|document|assignment|announcement|video|transcript|link|other; d2l_topic_id, sha256, d2l_updated_at, summary, status: fetched|extracted|summarized|failed), `topics` (taxonomy_version, slug, created_by: agent|user), `topic_edges` (prerequisite|related), `material_topics` (confidence, rationale, method, review_status), `enrichment_resources` (M3: intent type, rubric scores_json, verification_json, shared flag, status suggested|kept|dismissed), `domain_reputation` (M3: domain → kept/dismissed counts, feeds judge bias), `sync_runs`, `pipeline_runs` (usage_json per model), `llm_cache` ((sha256, stage, prompt_version, model) → output — the idempotency backbone).

Incremental re-sync: diff incoming ToC vs `materials` by `d2l_topic_id` + LastModifiedDate/size (no download needed); sha256 on receipt catches unchanged bytes → llm_cache skips re-pay.

## Extension ↔ Backend Contract

- Fixed port 8730; backend generates pairing token shown in Settings page; user pastes into popup once (chrome.storage). All requests `Authorization: Bearer <token>` (blocks localhost-CSRF).
- No CORS needed for extension (host_permissions exempts it); CORSMiddleware allows only Vite dev origin.
- Endpoints: `POST /api/ingest/handshake` {tenantOrigin, apiVersions, whoami, enrollments} → {knownCourses}; `POST /api/ingest/toc` {orgUnitId, raw ToC JSON, extras} → {syncRunId, needed:[{d2lTopicId,url,title}]}; `POST /api/ingest/file?syncRunId=&d2lTopicId=` raw streamed body + metadata headers (memory-flat for big files); `POST /api/ingest/complete`; `POST /api/ingest/zip` (manual Classic-Content zip fallback — keeps app usable with zero API access).
- Extension drives the fetch loop (owns session + rate-limit headers); backend owns decisions (diff, storage, pipeline). Cookie auth first; JWT capture only if 401.

## Pipeline (LangGraph, no Celery)

Stages wired as a **LangGraph StateGraph** (`pipeline/graph.py`): one node per stage, per-document fan-out via the Send API (map-reduce pattern) bounded to ~4 concurrent LLM calls; run as a background asyncio task per course. Resume = re-run: every stage's worklist is a DB query for rows below target state — the DB is the source of truth (LangGraph checkpointing not required for M1); llm_cache prevents double-pay. Optional LangSmith tracing via `LANGSMITH_API_KEY`/`LANGSMITH_TRACING` env vars to watch every node execute.

- **S1 summarize**: extract text, haiku per doc (no tools, ~12k chars in) → `DocSummary{summary, doc_kind_guess, key_terms[]}`
- **S2 taxonomy**: one sonnet call (syllabus + module tree + all summaries) → `Taxonomy{topics[{slug,name,description,module_hints[]}], edges[]}` (~8–20 topics); writes at taxonomy_version+1
- **S3 classify**: haiku per material vs fixed taxonomy → multi-label `{topic_slug, confidence, rationale}`; <0.5 flagged for review
- **S4 assemble**: pure Python → graph JSON; orphans → synthetic "Unsorted" topic
- **S5 enrich (M3)**: a per-topic **link-research agent team** (detailed below), not a single worker — finding genuinely good links is the hardest quality problem in the app and gets the deepest agent design

Structured outputs: Pydantic schema on every call; validate → retry once with error appended → fail row (never the run). Cost: usage tracked per run, `max_cost_usd_per_run` hard cap, dry-run endpoint estimates before running.

Taxonomy edits (`PUT /api/courses/{id}/taxonomy`): rename-only patches in place; structural edits bump version → auto re-classify only materials lacking rows at new version; merges remap old→new topic ids so confirmed assignments carry over.

## Frontend

Routes: `/` (courses + sync status + pairing), `/courses/:id` (workspace), `/settings`. Selection in search params. Workspace = OutlinePanel | GraphView | DetailPanel (MaterialReader: PDFs via iframe to `/api/materials/{id}/file`, text/VTT inline). TaxonomyEditor as drawer.

`GET /api/courses/{id}/graph` → {topics, materials, topicEdges, attachments, meta}. `transform.ts` + `uiStore.expandedTopicIds` → React Flow props. nodeTypes {topic, material}; edges prerequisite (solid) / related (dashed) / attachment (thin). Expand/collapse: filter visible set → dagre relayout (rankdir TB) → controlled nodes → `fitView({duration:300})`.

## Milestones

**M1 — extension → sync → pipeline → graph on a real course** (S≤½d, M≈1d, L≈2d):
1. Scaffold (M): git init, uv+FastAPI hello, pnpm workspaces, Vite hello, extension hello (popup pings /api/health with pairing token), Makefile
2. DB layer + blob store (M) with unit tests
3. Ingest API + diff + pairing auth (M); tests on hand-built ToC fixture
4. Extension d2l-client (M): version discovery, whoami, enrollments, ToC walk, streaming fetch, adaptive rate limiter
5. Extension sync loop + popup (M): course picker, progress, 401-pause/resume
6. **First real sync (S) — earliest de-risk checkpoint**; save scrubbed responses as fixtures; fix tenant surprises
7. Extract + agents/llm.py + S1 (M): extractors, tiered-model + structured-output layer (+`BSA_MOCK_LLM` mode), summarize node
8. S2 + S3 + S4 (L): taxonomy, classify, assemble, llm_cache, orphans
9. Runner + SSE + graph API + dry-run cost (M)
10. Graph UI (L): GraphView, transform, dagre, expand/collapse, node/edge components
11. Outline + DetailPanel + MaterialReader (M)
12. TaxonomyEditor + re-classify loop (M)
13. E2E hardening on a bigger course (M); tag v0.1

**M2 — media + transcripts**: extension capture.ts grabs LTI launch URLs + player network requests (VTT/manifest) during sync; media/ytdlp.py (browser cookies, Panopto/Kaltura); media/whisper.py (mlx-whisper large-v3-turbo fallback); transcripts become kind=transcript materials flowing through S1–S4 unchanged.

**M4 / backlog — MCP server**: expose the organized course graph as an MCP server (`backend/src/brightspace_agent/mcp_server.py`, stdio transport) so Claude Desktop/Code and other MCP clients can query topics/materials — makes the app a first-class agent-ecosystem citizen.

**M3 — enrichment: the link-research agent team** (`pipeline/stages/enrich.py`)

Finding genuinely good links per topic is a search-quality problem, not a single-prompt problem. Per topic, a 5-stage team (orchestrated deterministically in the runner via asyncio, same resume/cache semantics as S1–S4):

1. **Query planner** (sonnet, no tools): reads topic name/description + summaries of the topic's attached materials + course level/code → emits `SearchPlan{intents[]}` — diverse search intents typed by need: *alternative explanation*, *video lecture*, *worked practice problems with solutions*, *interactive visualization/simulation*, *other universities' lecture notes*, *past exams*. Grounding queries in the actual course materials (terminology, notation, textbook used) is what makes results level-appropriate rather than generic.
2. **Finder fan-out** (haiku/sonnet LangGraph ReAct workers with Anthropic server-side `web_search`/`web_fetch` tools via langchain-anthropic — no extra search-API key needed; Tavily optional alternative; ~8 tool-call cap each): one finder per intent, run in parallel, each blind to the others (multi-modal sweep — one search angle won't find everything). Source-quality heuristics in the prompt: prefer .edu/OCW/established channels (MIT OCW, 3Blue1Brown-class creators, university course pages, official docs), explicitly avoid SEO content farms, listicles, and course-seller spam. Each returns `Candidate{url, type, claimed_coverage, why}`.
3. **Verification** (haiku per candidate with `web_fetch`): actually fetch each candidate URL and check — is it live, is it actually about the topic (not clickbait/snippet mismatch), is it accessible (not paywalled/login-walled), does the depth match the course level? Verifier returns `Verification{ok, evidence_quote, level_fit, accessibility}`. **Never trust search snippets** — this stage is what separates genuinely good links from plausible-looking ones.
4. **Judge/ranker** (sonnet, one call per topic, sees verified content excerpts): rubric scoring — topical relevance, source authority, recency, level match, pedagogical value — plus a **format-diversity constraint** (never 5 videos; aim for a mix across intents). Keeps top 3–5 with a one-line rationale each (rationale doubles as UI copy).
5. **Cross-topic dedup + feedback** (deterministic): same URL surviving for multiple topics → attach to best-fit topic, mark `shared`. User keep/dismiss actions update a per-domain reputation table that biases future judge calls (dismissed-domain penalty, kept-domain boost) — the team gets better as Gavin uses it.

Quality loop: if <3 candidates survive verification for a topic, the runner re-invokes the planner with the failure reasons ("all videos paywalled", "results too advanced") to narrow/redirect queries — one retry round, then surface "thin topic" in the UI rather than padding with junk.

Cost control: enrichment is the most expensive stage, so it runs per-topic on demand (button in DetailPanel) or batch with dry-run estimate; all stages cached by (topic content hash, prompt_version).

UI: DetailPanel "Supplementary" section with keep/dismiss per resource; per-topic re-run button.

## Verification

- Fixtures over live hits: scrubbed real responses in `tests/fixtures/d2l/` after M1 step 6; all backend tests run from them
- `tests/fake_d2l.py`: FastAPI fake tenant on :9799; dev manifest variant lets the extension sync loop run E2E with zero real traffic (simulates 429s, mid-sync 401s)
- LLM stages: recorded outputs in `tests/fixtures/llm/`; `BSA_MOCK_LLM=1` replays → full 5-stage pipeline offline in pytest; schema tests assert recordings still parse
- `make e2e`: temp data dir + fake D2L + mock LLM; script replays extension sync sequence; asserts /graph payload (topic count range, zero orphans); one Playwright smoke (graph renders, expand works)
- Real-course checklist: sync smallest course → compare material counts vs Brightspace UI; read taxonomy before accepting; spot-check 10 classifications; check 429 stats; dry-run cost before first full pipeline

## Top Risks

1. **Per-tenant API variation** → version discovery + capability probe at handshake; all endpoint knowledge in one file (d2l-client.ts); unstable ToC falls back to stable; raw ToC persisted for debugging
2. **Session expiry mid-sync** → server-side needed-list makes syncs resumable; extension detects 401, badges "refresh Brightspace tab", resumes same syncRunId
3. **Rate limiting** → concurrency 2, honor headers, exponential backoff + jitter, stats surfaced per run
4. **LLM cost/quality blowups** → structured-output validate/retry/fail-row, dry-run preview, hard cost cap, content-hash cache, user-editable taxonomy as first-class recovery
5. **D2L changes unofficial surface** → zip-import fallback keeps pipeline usable; fixtures double as drift detectors; distribution stays unpacked-load (no Web Store review) until surface stabilizes

Note: session-cookie API access is unofficial (works because the Brightspace web UI uses the same routes). Personal/local study use is the design center; no redistribution of course content. Worth a glance at the university's acceptable-use policy before sharing with classmates.

## Key references
- https://github.com/RohanMuppa/brightspace-mcp-server (auth capture, version discovery, rate limiter patterns)
- https://github.com/Aaryan-Kapoor/d2l-cli (Python read-only GET design)
- https://docs.valence.desire2learn.com/res/content.html (ToC + file endpoints)
- https://arxiv.org/abs/2403.12173 (TnT-LLM taxonomy→classify pattern)
- https://www.anthropic.com/engineering/multi-agent-research-system (enrichment orchestrator-worker pattern)
- https://reactflow.dev/learn/layouting/layouting (dagre integration)
- https://langchain-ai.github.io/langgraph/ (StateGraph, Send API map-reduce, create_react_agent)
- https://docs.langchain.com/oss/python/integrations/chat/anthropic (ChatAnthropic, server-side web_search/web_fetch tools)
