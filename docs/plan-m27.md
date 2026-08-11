# M2.7 Zero-Paste Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recording channels hidden behind LTI launches get discovered automatically — the extension performs the launch in a background tab and reports the real URL — and pairing becomes a click instead of a paste.

**Architecture:** The D2L ToC only ever gives us an LTI quicklink stub; the real Mediasite/Zoom URL materializes only when a logged-in browser *performs* the launch. The extension is that browser. After each successful course sync, it asks the backend which LTI-hinted materials are still unresolved, opens each quicklink in a background (non-active) tab, waits for the redirect chain to settle, reads the final URL, closes the tab, and POSTs the result. The backend re-validates with the same `classify_url` the passive detector uses and feeds recognized URLs through the existing manual-add expansion path (`fetcher.expand`), so channels become one `media_sources` row per lecture with zero typing. Separately, pairing gets a request/approve/claim flow: the popup requests, the user clicks Approve in the app's Settings page, the extension claims the token.

**Tech Stack:** FastAPI + SQLAlchemy + hand-rolled SQLite migrations (backend); MV3 service worker + chrome.tabs, pure-logic modules under vitest (extension); React + TanStack Query (frontend).

## Global Constraints

- NEVER any Co-Authored-By or AI attribution in commits, code, or docs.
- All tests offline. TDD. Full suites green before every commit round: `cd backend && uv run pytest`, `cd extension && pnpm test`, `cd frontend && pnpm test`, `pnpm tsc --noEmit` in both JS packages.
- Wire format is camelCase JSON via the established `CamelModel` pattern (see `api/media.py`).
- Every URL that can reach a `media_sources` row or an anchor goes through `classify_url` (backend) — which enforces http(s) — and `isSafeHttpUrl` (frontend). No new URL path may bypass either layer.
- The extension only ever opens launch URLs on the tenant origin it is syncing (assert prefix match before `tabs.create`).
- Migrations: next version is 7. MIGRATIONS uniqueness/monotonicity is guarded at import (`db/migrate.py`) — follow the exact schema.sql-vs-MIGRATIONS pattern migration 4 (`media_sources`) used so fresh DBs and upgraded DBs converge identically.
- Commits on branch `m27-autodiscovery`.

## File Structure

- `backend/src/brightspace_agent/db/migrate.py`, `db/schema.sql`, `db/models.py` — migration 7: `lti_resolutions` table + model.
- `backend/src/brightspace_agent/api/ingest.py` — two new extension-facing routes (candidates GET, resolution POST); pairing routes live in `api/pair.py` (new).
- `backend/src/brightspace_agent/api/media.py` — `MediaHintOut` gains resolution state for the drawer.
- `extension/src/lib/lti-resolver.ts` (new) — pure navigation-settle state machine + resolver loop, zero chrome.* imports (house rule from sync-engine.ts).
- `extension/src/background.ts` — thin chrome adapter wiring tabs events into the resolver; runs it after successful sync.
- `extension/src/lib/backend-client.ts` — `ltiCandidates()`, `reportLtiResolution()`, `pairRequest()`, `pairClaim()`.
- `extension/src/popup/` — Connect button (pair flow) + resolver progress line.
- `frontend/src/pages/SettingsPage.tsx` — pending-pair banner with Approve.
- `frontend/src/panels/RecordingsDrawer.tsx` — hint rows show resolution status; paste box demoted to fallback.
- `scripts/e2e.py`, `backend/tests/fake_d2l.py` — e2e leg simulating the extension's resolution POST.

---

### Task 1: Backend — lti_resolutions storage + extension-facing endpoints

**Files:**
- Modify: `backend/src/brightspace_agent/db/migrate.py` (migration 7), `db/schema.sql`, `db/models.py`
- Modify: `backend/src/brightspace_agent/api/ingest.py` (candidates GET + resolution POST)
- Modify: `backend/src/brightspace_agent/api/media.py` (hints gain resolution state)
- Test: `backend/tests/test_db.py`, `backend/tests/test_api_ingest.py` (or the file that already covers ingest routes — follow the existing layout), `backend/tests/test_api_media.py`

**Interfaces:**
- Consumes: `_compute_lti_hints` (api/media.py) — extract its candidate query into a shared helper both the hints computation and the new candidates GET use, so the two can never disagree about what counts as an LTI candidate. Reuses `classify_url` (media/detect.py), `fetcher.expand` + `_upsert_manual_media_source` + `_map_expand_error` (api/media.py — import or move; do NOT duplicate the expand-and-upsert logic).
- Produces (Task 2 and 4 rely on these exact shapes):
  - Table `lti_resolutions`: `id, course_id FK, material_id FK UNIQUE, launch_url TEXT NOT NULL, final_url TEXT NULL, platform TEXT NULL, status TEXT NOT NULL CHECK IN ('resolved','unrecognized','failed'), error TEXT NULL, created_at, updated_at`.
  - `GET /api/ingest/lti-candidates?orgUnitId=<int>` (pairing-token auth, same dependency the other ingest routes use) → `{courseId: int, candidates: [{materialId: int, title: str, launchUrl: str}]}`. Candidates = the shared LTI-hint query MINUS materials that already have an `lti_resolutions` row with status `resolved` or `unrecognized` (rows with status `failed` ARE re-offered — transient launch failures should retry on the next sync). Unknown orgUnitId → 404.
  - `POST /api/ingest/lti-resolution` (pairing-token auth; body `{orgUnitId: int, materialId: int, finalUrl: str | null, error: str | null}`) →
    - `finalUrl` null or not absolute-http(s) → upsert resolution row status `failed` with `error`; respond `{status: "failed"}`.
    - `classify_url(finalUrl)` recognizes a platform → run the expand-and-upsert path exactly as `add_media_url` does (same `MediaFetchError` mapping); upsert resolution row status `resolved` with `platform` + `final_url`; respond `{status: "resolved", platform, added, total}`.
    - Not recognized → upsert row status `unrecognized` with `final_url`; respond `{status: "unrecognized"}`. NOT an error — the landing URL is diagnostic gold for the drawer.
    - `materialId` not in the candidate query for that course → 404 (the extension can only resolve materials the backend itself flagged).
    - Upsert = one row per material (`UNIQUE(material_id)`): a re-resolution overwrites final_url/platform/status/error and bumps `updated_at`.
  - `MediaHintOut` (api/media.py) gains `resolution: HintResolutionOut | None` where `HintResolutionOut = {status: str, finalUrl: str | None, error: str | None}` — joined from `lti_resolutions` by material_id.

- [ ] Migration 7 + model, test-first: fresh DB has the table; a hand-built v6 DB migrates cleanly with existing rows byte-identical; second migrate() is a no-op. Mirror the existing migration-test fixtures in test_db.py.
- [ ] Candidates GET, test-first: seeded LTI-looking link material appears; a non-LTI link does not; a material with a `resolved` row disappears; a `failed` row keeps it listed; bad orgUnitId 404s; missing/wrong token 401s.
- [ ] Resolution POST, test-first: recognized URL creates media_sources rows via the mock fetcher's expand (assert added/total), resolution row `resolved`; unrecognized URL stores `unrecognized` + finalUrl; null finalUrl stores `failed` + error; re-POST overwrites (no duplicate row); non-candidate materialId 404s; `javascript:`-scheme finalUrl lands in `failed`, never anywhere near media_sources.
- [ ] Hints resolution state, test-first: GET media list shows `resolution: null` before, the row's state after.
- [ ] Full backend suite + commit.

### Task 2: Extension — background-tab LTI resolver

**Files:**
- Create: `extension/src/lib/lti-resolver.ts`, `extension/src/lib/lti-resolver.test.ts`
- Modify: `extension/src/lib/backend-client.ts` (+ its test), `extension/src/background.ts`, `extension/src/popup/` (progress line)

**Interfaces:**
- Consumes: Task 1's two endpoints via new `BackendClient` methods `ltiCandidates(orgUnitId): Promise<LtiCandidatesResponse>` and `reportLtiResolution(payload): Promise<LtiResolutionResponse>` (camelCase types in types.ts mirroring Task 1's shapes).
- Produces: `resolveLtiCandidates(deps: LtiResolverDeps, origin: string, orgUnitId: number): Promise<LtiResolveSummary>` where `LtiResolverDeps = {backend: {ltiCandidates, reportLtiResolution}, tabs: TabDriver, onProgress: (p: {done, total}) => void}` and `TabDriver = {open(url: string): Promise<number>, currentUrl(tabId: number): Promise<string | null>, close(tabId: number): Promise<void>, onNavigationSettled(tabId: number, quietMs: number, timeoutMs: number): Promise<void>}`. `LtiResolveSummary = {resolved, unrecognized, failed, total}`.

Requirements:

- ARCHITECTURE RULE (same as sync-engine.ts): `lti-resolver.ts` imports zero chrome.* — the `TabDriver` is injected; background.ts implements it with `chrome.tabs.create({active: false})`, `chrome.tabs.get`, `chrome.tabs.remove`, and a settle-watcher built on `chrome.tabs.onUpdated`/`onRemoved`.
- Settle semantics (implement in the chrome adapter, but keep the *policy* — quietMs/timeoutMs values and the decision flow — in the pure module so it's testable): a navigation is settled when a `status: "complete"` update has been followed by `quietMs = 2000` ms with no further onUpdated events for that tab; hard cap `timeoutMs = 25000` ms from open, after which the current URL is taken as final anyway. Tab closed by the user before settling → that candidate reports `finalUrl: null, error: "tab closed"`.
- Per candidate, sequentially (never parallel tabs — the point is to be unobtrusive): resolve `launchUrl` against `origin` (relative URLs allowed); if the resulting absolute URL is not on `origin`, skip it client-side as failed (`error: "launch URL not on tenant origin"`) without opening anything. Open → settle → read URL → close (close in a finally — never leak tabs, including on timeout) → POST the resolution. A candidate whose final URL still sits on the tenant origin (launch bounced back or errored) reports it as-is — the backend files it `unrecognized`, which is exactly right.
- Cap 8 candidates per run (log-and-skip the rest; next sync picks them up since only resolved/unrecognized rows leave the candidate list).
- Backend/network errors on one candidate never abort the loop — collect and continue.
- background.ts: after `syncCourse`/`resume` returns with phase `complete`, run the resolver for that course before returning; emit `{evt: "lti-progress", done, total}` runtime messages (ignore-if-closed, same pattern as sync progress); resolver failure never turns a complete sync into a failed one — wrap the whole call, report `{evt: "lti-progress", error}` at worst.
- Popup: a one-line status under the sync progress ("Resolving recording links… 2/3", then "Recording links: 1 resolved, 1 needs a look") driven by those messages. No new screens.
- Tests (vitest, fake TabDriver + fake backend): happy path (open → settle → close → POST with final URL); timeout path takes current URL; user-closed-tab path; off-origin launchUrl never opens a tab; per-candidate error isolation; cap at 8; progress callback sequence. Settle policy tested with fake timers.

- [ ] Types + BackendClient methods, test-first.
- [ ] Pure resolver loop + settle policy, test-first (this is the bulk of the task).
- [ ] Chrome adapter in background.ts + popup line; `pnpm tsc --noEmit` clean in extension/.
- [ ] Full extension + frontend suites + commit.

### Task 3: One-click pairing — request/approve/claim

**Files:**
- Create: `backend/src/brightspace_agent/api/pair.py`, `backend/tests/test_api_pair.py`
- Modify: `backend/src/brightspace_agent/main.py` (mount router, in-memory pending-pair state on app.state)
- Modify: `extension/src/lib/backend-client.ts` (+test), `extension/src/popup/` (Connect button + polling)
- Modify: `frontend/src/pages/SettingsPage.tsx` (+test), `frontend/src/api/client.ts` + `api/types.ts`

**Interfaces:**
- Produces:
  - `POST /api/pair/request` (UNauthenticated — this is the bootstrap; read main.py's CSRF guard first and pass it the way the extension's other POSTs do) → `{requestId: str}` (`secrets.token_urlsafe(16)`). Stores a single pending request on `app.state.pending_pair = {"request_id", "created_at", "approved": False}`; a new request replaces any prior un-claimed one; entries expire 180s after creation (checked lazily on every pair endpoint touch).
  - `GET /api/pair/pending` (frontend, no token — same exposure rules as other GETs) → `{pending: bool}`. True only for an unexpired, unapproved request.
  - `POST /api/pair/approve` (frontend, CSRF header like other mutating routes) → marks the pending request approved; 404 if none pending/expired.
  - `GET /api/pair/claim?requestId=<id>` (extension) → while unapproved: `{status: "pending"}`; wrong/expired id: 404; once approved AND id matches (constant-time compare): `{status: "approved", pairingToken: <the real token>}` and the pending state is cleared (single use).
  - Security invariants to test explicitly: the token is released ONLY to the requestId that was approved; a second `request` before approval invalidates the first id (last-writer-wins keeps the approve button unambiguous); claim after expiry 404s; approve with nothing pending 404s.
- Extension: popup shows "Connect to BrightSpace Agent" when `health().paired` is false → `pairRequest()` → renders "Open the app's Settings page and click Approve" with a link to `http://127.0.0.1:8730/settings` → polls `pairClaim(requestId)` every 2s (up to 3 min) → on approved, stores `pairingToken` in `chrome.storage.local` (same key the manual paste path uses) and flips to the paired UI. Manual paste field stays as the fallback, below a divider.
- Frontend Settings: while mounted, poll `/api/pair/pending` every 2s (TanStack Query `refetchInterval`); when pending, show a banner — "A browser extension is asking to connect. Approve only if you just clicked Connect in the BrightSpace Agent extension." — with an Approve button that POSTs approve and invalidates the query. Vitest: banner renders when pending, approve fires the POST, banner absent when not pending.

- [ ] Backend pair router, test-first (including all four security invariants above).
- [ ] Extension client methods + popup flow, test-first for the client; popup wiring.
- [ ] Settings banner, test-first.
- [ ] Full three suites + tsc + commit.

### Task 4: Drawer resolution status, e2e leg, docs

**Files:**
- Modify: `frontend/src/api/types.ts` (hint resolution shape), `frontend/src/panels/RecordingsDrawer.tsx` (+test)
- Modify: `backend/tests/fake_d2l.py`, `scripts/e2e.py`
- Modify: `README.md`, `docs/OVERVIEW.md`

**Interfaces:**
- Consumes: Task 1's `MediaHintOut.resolution` and the two ingest endpoints; the e2e script plays the extension's role directly against them.

Requirements:

- Drawer hint rows (currently paste-first copy) become status-first: `resolution == null` → "Will resolve automatically on your next sync"; `unrecognized` → "Launch landed at <finalUrl> — not a recognized platform" with the URL rendered as text-plus-guarded-anchor (`isSafeHttpUrl` — never an anchor otherwise) and the paste box offered as the fallback; `failed` → the error + paste fallback. The Add-URL box itself stays, demoted below the hints.
- e2e (`scripts/e2e.py`): fake tenant gains one LTI-quicklink link material whose title matches the hint regex; the script asserts it appears in `GET /api/ingest/lti-candidates`, POSTs a resolution whose finalUrl is a mediasite-shaped URL, asserts `media_sources` rows appear (mock fetcher expansion) and the candidate leaves the list and the drawer hint carries `resolution.status == "resolved"`; then POSTs an unrecognized resolution for a second candidate and asserts the `unrecognized` state round-trips.
- README: "Lecture recordings" section rewritten — automatic discovery on sync is the front door, paste is the fallback; one-click pairing replaces the paste-the-token instruction in the quickstart (manual paste documented as fallback).
- [ ] Drawer states, test-first (all three resolution states + null).
- [ ] e2e leg + fake tenant material.
- [ ] Docs. Full suites + `make e2e` green + commit.

---

## Self-review notes

- Spec coverage: auto-discovery (Tasks 1+2), one-click pairing (Task 3), UI surfacing + fallback retention + e2e + docs (Task 4). The per-platform landing-page extraction (viewer URL ≠ channel URL) is deliberately OUT of scope until Gavin's real-course validation shows what real launches land on — the `unrecognized` state is the designed landing spot for that data.
- Type consistency: `lti_resolutions.status ∈ {resolved, unrecognized, failed}` is used identically in Task 1 (CHECK constraint), Task 2 (summary buckets), and Task 4 (drawer states). `HintResolutionOut` field names match `frontend/src/api/types.ts` camelCase.
- The candidate query lives in ONE shared helper (Task 1) consumed by both hints and the GET — divergence-proof by construction.
