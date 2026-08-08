# M2 — Lecture recordings and transcripts: implementation plan

## Context

M1 syncs and organizes documents; M3 enriches topics from the web. The
biggest course artifact still missing is the lectures themselves. M2 turns
lecture recordings into `transcript` materials that flow through the
existing summarize → classify pipeline unchanged, so topic nodes carry the
actual lectures.

The original plan assumed Panopto/Kaltura. The user's real courses use
**Mediasite, Zoom, and Google Drive**, and could use others — so M2 is
platform-registry-shaped: URL detection and per-platform quirks live in one
module each, and adding a platform later is one entry, not a redesign.

## Research findings that shape the design (verified Aug 2026)

- **yt-dlp** (2026.08.04, actively shipped) has maintained extractors for
  all three platforms. Auth rides the user's Chrome session via
  `--cookies-from-browser chrome`, which still works on macOS (App-Bound
  Encryption is Windows-only); first run needs a one-time Keychain
  "Always Allow". Fallback: a `cookies.txt` file via config.
- **Zoom**: `transcript`/`cc` VTT tracks are directly downloadable via
  subtitles when the host allows viewing them — often no media download or
  local ASR needed at all. Passcodes go via `--video-password`; parse them
  from the surrounding D2L page text when present.
- **Mediasite**: yt-dlp does NOT extract its transcripts (explicit `XXX`
  stub in source). Mediasite always takes the audio + local-ASR path.
- **Google Drive**: cookies required (undocumented playback API — the most
  fragile extractor); owner-disabled downloads give an unfixable 403 that
  must surface as "the professor disabled downloads", not a retry loop.
- **Local ASR**: `parakeet-mlx` (0.5.2, June 2026, active) over
  `mlx-whisper` (PyPI stale a year; documented hallucination-on-silence and
  word-timestamp memory-leak issues). Parakeet: better English WER, native
  VTT with word timestamps, transducer architecture without Whisper's
  repetition/hallucination failure class. English-first is acceptable — the
  user's lectures are English; the Transcriber protocol keeps a
  multilingual slot open.
- **ffmpeg**: `static-ffmpeg>=3.0` (native darwin_arm64) as a normal uv
  dependency + `static_ffmpeg.add_paths()` — keeps `uv sync` the whole
  install story and dodges uv/Homebrew PATH issues.

## Design

### Detection (extension already delivers the inputs)

M1 already stores link materials and topic HTML. A deterministic detector
scans link URLs and page HTML for recording URLs:

- Mediasite: `/Mediasite/Play/<id>`, `/Mediasite/Catalog/...` on any domain
- Zoom: `*.zoom.us/rec/share|rec/play|recording/play|clips/share`
- Google Drive: `drive.google.com/file/d/<id>`, `/uc?id=`, Drive folders
- Zoom passcodes: regex the surrounding text/HTML ("Passcode: …") into the
  source row; UI accepts manual entry when absent.

Detected recordings land in a new `media_sources` table:
`(id, course_id, material_id FK, platform, url, passcode, status, error,
transcript_material_id FK, created_at, updated_at)` with
`status ∈ detected | fetching | transcribing | done | failed | skipped`.

### Fetching (`media/fetch.py`, MediaFetcher protocol + yt-dlp impl + mock)

Caption-first, audio-only fallback, never store video:

1. Try platform captions: `--write-subs --sub-langs all --skip-download`
   (Zoom transcript/cc; Drive auto-captions). Got VTT → done, no ASR.
2. Else download audio only (`-f bestaudio` / `-x`), hand to the
   transcriber, delete the audio afterward (config `keep_media` to retain).
3. Cookies: `--cookies-from-browser chrome` default; `BSA_COOKIES_FILE`
   fallback. Per-platform error mapping to human-readable failures
   (expired SSO session, wrong passcode, downloads disabled by owner,
   extractor breakage → "try updating yt-dlp").

yt-dlp runs as a subprocess (its CLI is the stable, documented surface the
research verified; a crash can't take the backend down; flags match the
research examples exactly).

### Transcription (`media/transcribe.py`, Transcriber protocol + parakeet impl + mock)

`parakeet-mlx` with `mlx-community/parakeet-tdt-0.6b-v3` → WebVTT. The
resulting VTT is stored through the normal ingest path as a
`kind=transcript` material (blob + extracted text via the existing VTT
extractor), linked from `media_sources.transcript_material_id`, and the
material is attributed to the same module as its source recording. Heavy
deps (`parakeet-mlx`, `static-ffmpeg`) go in an optional dependency group
(`uv sync --group media`) so the base install stays light; the API returns
a clear "media extras not installed" error otherwise.

### Orchestration + API + UI

- A media runner mirroring the pipeline runner's shape: background job per
  course, sequential (one ASR job at a time — single GPU), SSE progress
  events, statuses in `media_sources`. Shares the per-course active-run
  guard so it can't collide with a pipeline run's material writes.
- API: `GET /api/courses/{id}/media` (detected list + statuses),
  `POST /api/courses/{id}/media/process` (all pending) and
  `POST /api/media/{id}/process` (one), `PUT /api/media/{id}` (set
  passcode / skip). CSRF header on mutations, as everywhere.
- UI: a "Recordings" section in the course workspace listing detected
  recordings with platform badge, status, error text, passcode input when
  needed, and Process buttons. After transcripts land, the existing
  "Run pipeline" flow summarizes/classifies them (the dry-run estimate
  already prices new materials automatically).
- Zero LLM cost; time is the budget (a 75-min lecture ≈ 10–20 min ASR).
  The UI says so instead of showing a dollar estimate.

### Testing

Both protocols get deterministic mocks (the LLMBackend/WebBackend pattern):
the fetcher mock returns fixture VTT or fixture audio paths by URL pattern;
the transcriber mock returns fixture VTT. Detector, state machine, API, and
UI are fully testable offline; `make e2e` grows a media leg. Real-platform
validation is a checkpoint task on the user's machine (their courses, their
cookies), like M1's first real sync — expected to surface tenant quirks the
mocks can't know.

## Tasks

1. **M2.1** `media_sources` table + migration + URL/passcode detector over
   existing materials (runs at sync-complete and on demand) — M
2. **M2.2** MediaFetcher protocol, yt-dlp subprocess impl (captions-first,
   audio fallback, cookie strategy, per-platform error mapping), mock — L
3. **M2.3** Transcriber protocol, parakeet-mlx impl behind optional dep
   group, VTT → transcript-material ingest, mock — M
4. **M2.4** Media runner + API endpoints + SSE events — M
5. **M2.5** Frontend Recordings section (list, statuses, passcode entry,
   process buttons) — M
6. **M2.6** Real-course validation on the user's machine (Mediasite + Zoom
   + Drive links from real courses; fix quirks; record scrubbed fixtures) — S/M
7. **M2.7** Whole-branch review + fix wave; docs update — S

## Risks

1. **Extractor drift** (Zoom #16377-class breakage, Drive's undocumented
   API): subprocess isolation + explicit "update yt-dlp" error path;
   `uv lock --upgrade-package yt-dlp` documented.
2. **Cookie extraction friction** (Keychain prompt, locked DB): first-run
   guidance in the UI error text; cookies.txt fallback.
3. **View-only Drive files**: unfixable by design; clear per-item error and
   `skipped` status so one locked file never blocks the batch.
4. **ASR quality on hard audio** (accents, math notation, bad mics):
   transcripts are inputs to summarize/classify, which tolerate noise; the
   graph links back to the real recording either way.
5. **LTI-embedded players** (recording reachable only through an LTI
   launch, no direct URL in content): out of scope for M2.1–M2.7; the
   detector records what it can see. If real courses show LTI-only
   recordings, a capture assist in the extension becomes an M2 follow-up.
