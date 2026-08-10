"""M2.2 media fetcher: turns a detected `media_sources` row into either a
platform-caption VTT file or a downloaded audio file, using yt-dlp as a
subprocess -- captions first (cheap, already-transcribed, no ASR needed
later), audio only as fallback, and NEVER video (nothing downstream needs the
picture, and video is the slow/large case this module exists to avoid).

Sibling of `agents/llm.py` / `agents/web.py`: a small Protocol, a real
subprocess-backed implementation, a deterministic offline mock, and a
`make_media_fetcher` selector with the same mock/real rule those modules use.

Two things worth knowing before touching `YtDlpFetcher`:

- **`run` is injected, never `subprocess.run` called directly.** Every test
  in `tests/test_media_fetch.py` passes a fake `run` that records argv and
  simulates yt-dlp's on-disk effects -- no real subprocess/network in this
  module's test suite, ever.
- **Error mapping is substring matching over stderr, order-sensitive.** The
  gdrive-403 case and the auth_expired case can both match the same yt-dlp
  message (it often suggests trying `--cookies` right after a 403), so
  `_map_error` checks gdrive-403 BEFORE the generic auth_expired substrings
  even though a naive top-to-bottom reading of the brief's table would check
  them in the other order -- see `_map_error`'s docstring.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from brightspace_agent.config import Settings

logger = logging.getLogger(__name__)

RunFunc = Callable[..., subprocess.CompletedProcess]

# Extensions `-x --audio-format m4a` can leave behind: normally just m4a, but
# yt-dlp falls back to whatever ffmpeg produced if remuxing to m4a wasn't
# possible for that source, so the "newest audio file" scan checks all of
# them rather than assuming m4a specifically.
_AUDIO_EXTS = {"m4a", "mp3", "opus", "webm", "wav"}

# Caption filename preference, checked in order; first token found wins.
_CAPTION_PREFERENCE = ("transcript", "cc", ".en")


@dataclass(frozen=True)
class FetchSpec:
    platform: str  # 'mediasite' | 'zoom' | 'gdrive'
    url: str
    passcode: str | None
    dest_dir: Path  # exists; the fetcher writes ONLY inside it


@dataclass(frozen=True)
class FetchResult:
    kind: str  # 'captions' | 'audio'
    path: Path  # the .vtt file (captions) or audio file


class MediaFetchError(Exception):
    """kind in: 'not_installed' | 'auth_expired' | 'wrong_passcode' |
    'downloads_disabled' | 'not_found' | 'extractor_error'. `user_message`
    is meant to be shown to the course owner as-is -- it names the fix,
    never yt-dlp internals."""

    def __init__(self, kind: str, user_message: str) -> None:
        super().__init__(user_message)
        self.kind = kind
        self.user_message = user_message


class MediaFetcher(Protocol):
    def fetch(self, spec: FetchSpec) -> FetchResult: ...


# --------------------------------------------------------------------------
# YtDlpFetcher
# --------------------------------------------------------------------------


class YtDlpFetcher:
    """Real backend: shells out to yt-dlp via the injected `run` callable
    (defaults to `subprocess.run`, never called with `shell=True` or
    `check=True` -- exit codes are always handled manually here)."""

    def __init__(self, settings: Settings, run: RunFunc = subprocess.run) -> None:
        self._settings = settings
        self._run = run

    def fetch(self, spec: FetchSpec) -> FetchResult:
        binary = shutil.which("yt-dlp")
        if binary is None:
            raise MediaFetchError(
                "not_installed",
                "yt-dlp is not installed. Run `uv sync --group media` to install it, then try again.",
            )

        # Mediasite's own transcripts aren't extractable by yt-dlp (it has no
        # Mediasite extractor with subtitle support) -- skip straight to the
        # audio phase rather than spending a whole subprocess round-trip on a
        # captions attempt that can never succeed.
        if spec.platform != "mediasite":
            captions = self._try_captions(binary, spec)
            if captions is not None:
                return captions

        return self._fetch_audio(binary, spec)

    # -- phase 1: captions ------------------------------------------------

    def _try_captions(self, binary: str, spec: FetchSpec) -> FetchResult | None:
        argv = [
            binary,
            spec.url,
            "--skip-download",
            "--write-subs",
            "--sub-langs",
            "all",
            *self._output_args(spec),
            *self._cookie_args(),
            *self._passcode_args(spec),
        ]
        result = self._run_yt_dlp(argv, spec)
        if result.returncode != 0:
            raise self._map_error(spec, result.stderr or "")

        vtt_files = sorted(spec.dest_dir.glob("*.vtt"))
        if not vtt_files:
            # Not an error -- plenty of recordings simply have no platform
            # captions. Fall through to the audio phase.
            return None
        return FetchResult(kind="captions", path=_pick_caption(vtt_files))

    # -- phase 2: audio -----------------------------------------------------

    def _fetch_audio(self, binary: str, spec: FetchSpec) -> FetchResult:
        argv = [
            binary,
            spec.url,
            "-f",
            "bestaudio",
            "-x",
            "--audio-format",
            "m4a",
            *self._output_args(spec),
            *self._cookie_args(),
            *self._passcode_args(spec),
        ]
        result = self._run_yt_dlp(argv, spec)
        if result.returncode != 0:
            raise self._map_error(spec, result.stderr or "")

        audio_files = [
            path for path in spec.dest_dir.iterdir() if path.suffix.lstrip(".").lower() in _AUDIO_EXTS
        ]
        if not audio_files:
            raise MediaFetchError(
                "extractor_error",
                "yt-dlp exited successfully but produced no audio file; try updating yt-dlp: "
                "`uv lock --upgrade-package yt-dlp` then `uv sync --group media`.",
            )
        newest = max(audio_files, key=lambda path: path.stat().st_mtime)
        return FetchResult(kind="audio", path=newest)

    # -- shared argv/run plumbing -------------------------------------------

    def _output_args(self, spec: FetchSpec) -> list[str]:
        return ["-o", str(spec.dest_dir / "%(title).100B.%(ext)s"), "--no-playlist"]

    def _cookie_args(self) -> list[str]:
        if self._settings.cookies_file:
            return ["--cookies", str(self._settings.cookies_file)]
        return ["--cookies-from-browser", self._settings.cookies_from_browser]

    def _passcode_args(self, spec: FetchSpec) -> list[str]:
        return ["--video-password", spec.passcode] if spec.passcode else []

    def _run_yt_dlp(self, argv: list[str], spec: FetchSpec) -> subprocess.CompletedProcess:
        try:
            return self._run(
                argv, capture_output=True, text=True, timeout=self._settings.media_fetch_timeout_s
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaFetchError(
                "extractor_error",
                f"yt-dlp timed out after {self._settings.media_fetch_timeout_s}s fetching {spec.url}.",
            ) from exc

    def _map_error(self, spec: FetchSpec, stderr: str) -> MediaFetchError:
        """Case-insensitive substring match over stderr, first match wins.

        The gdrive-403 check runs BEFORE the generic auth_expired substrings
        even though the brief's table lists downloads_disabled third: a real
        gdrive 403 message often also contains "cookies" (yt-dlp suggests
        trying `--cookies` right after a permission error), which would
        otherwise match auth_expired's "cookies" substring first and mask
        the actual downloads-disabled case. Checking gdrive-403 first makes
        the two rules mutually exclusive by construction instead of relying
        on stderr wording never colliding.
        """
        tail = stderr[-500:]
        logger.warning("yt-dlp failed for %s: %s", spec.url, tail)
        lowered = stderr.lower()

        if any(
            token in lowered
            for token in ("video-password", "password is required", "wrong password", "validate_passwd")
        ):
            return MediaFetchError(
                "wrong_passcode",
                "The passcode for this recording is wrong or missing. Set or re-enter the passcode and try again.",
            )

        if spec.platform == "gdrive" and any(
            token in lowered for token in ("403", "forbidden", "cannot download")
        ):
            return MediaFetchError(
                "downloads_disabled",
                "The file's owner has disabled downloads for it; nothing on our end can bypass that.",
            )

        if any(token in lowered for token in ("login", "sign in", "authentication", "cookies")):
            return MediaFetchError(
                "auth_expired",
                "Your login session for this platform appears to have expired. "
                "Open the platform in Chrome and log in again, then retry.",
            )

        if any(
            token in lowered for token in ("unsupported url", "404", "not found", "no longer available")
        ):
            return MediaFetchError("not_found", "The link may be dead or has moved.")

        return MediaFetchError(
            "extractor_error",
            "yt-dlp failed to fetch this recording. Try updating yt-dlp: "
            "`uv lock --upgrade-package yt-dlp` then `uv sync --group media`.",
        )


def _pick_caption(vtt_files: list[Path]) -> Path:
    """Preference order: filename containing "transcript", else "cc", else
    ".en", else the first file in sorted order. `vtt_files` is expected
    pre-sorted (glob result sorted by caller), so each token match already
    resolves ties by name."""
    for token in _CAPTION_PREFERENCE:
        for path in vtt_files:
            if token in path.name.lower():
                return path
    return vtt_files[0]


# --------------------------------------------------------------------------
# MockMediaFetcher -- deterministic, offline, zero subprocess. Behavior is
# keyed on the URL so tests/e2e runs can request a specific outcome by
# constructing the right fake URL, same idea as agents/web.py's
# `_mock_verify` keying off "paywall"/"login" substrings.
# --------------------------------------------------------------------------

_MOCK_VTT = (
    "WEBVTT\n\n"
    "00:00:00.000 --> 00:00:02.000\n"
    "This is a mock caption cue for offline testing.\n\n"
    "00:00:02.000 --> 00:00:04.000\n"
    "A second cue, so preference-picking has two real timestamps to see.\n"
)

_MOCK_FAIL_MARKER = "mock-fail-"


class MockMediaFetcher:
    """Deterministic, offline stand-in for `YtDlpFetcher`: same URL always
    yields the same outcome, no subprocess, no network."""

    def fetch(self, spec: FetchSpec) -> FetchResult:
        if "mock-captions" in spec.url:
            path = spec.dest_dir / "mock-captions.vtt"
            path.write_text(_MOCK_VTT, encoding="utf-8")
            return FetchResult(kind="captions", path=path)

        marker_at = spec.url.find(_MOCK_FAIL_MARKER)
        if marker_at != -1:
            kind = spec.url[marker_at + len(_MOCK_FAIL_MARKER) :]
            raise MediaFetchError(kind, f"mock {kind}")

        path = spec.dest_dir / "audio.m4a"
        path.write_bytes(b"mock-audio")
        return FetchResult(kind="audio", path=path)


# --------------------------------------------------------------------------
# Backend selection -- same rule as make_backend/make_web_backend, extended
# with `mock_media`: mock media fetching can be forced independently of the
# LLM mock (e.g. to test the yt-dlp integration path with a real Anthropic
# key but no real downloads), and `mock_llm` also forces it so an offline
# test/e2e run never spawns a subprocess regardless of which flag it set.
# --------------------------------------------------------------------------


def make_media_fetcher(settings: Settings) -> MediaFetcher:
    if settings.mock_media:
        logger.info("media fetcher: mock (BSA_MOCK_MEDIA is set)")
        return MockMediaFetcher()
    if settings.mock_llm:
        logger.info("media fetcher: mock (BSA_MOCK_LLM is set)")
        return MockMediaFetcher()

    logger.info("media fetcher: yt-dlp subprocess")
    return YtDlpFetcher(settings)
