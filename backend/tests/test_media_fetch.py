"""Tests for the M2.2 media fetcher (media/fetch.py): YtDlpFetcher's two-phase
captions-then-audio subprocess shape, its yt-dlp stderr -> MediaFetchError
mapping, MockMediaFetcher's deterministic offline behavior, and
make_media_fetcher's mock/real selection.

No real subprocess or network anywhere here -- `YtDlpFetcher` always gets a
fake `run` callable that records the argv it was invoked with and simulates
yt-dlp's on-disk effects (writing files into dest_dir) instead of actually
downloading anything.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from brightspace_agent.config import Settings
from brightspace_agent.media.fetch import (
    FetchResult,
    FetchSpec,
    MediaFetchError,
    MockMediaFetcher,
    YtDlpFetcher,
    make_media_fetcher,
)


@pytest.fixture(autouse=True)
def _no_ambient_media_env(monkeypatch):
    """A real key/flag on the host running these tests must not change
    make_media_fetcher's choice or YtDlpFetcher's cookie defaults."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BSA_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BSA_MOCK_LLM", raising=False)
    monkeypatch.delenv("BSA_MOCK_MEDIA", raising=False)
    monkeypatch.delenv("BSA_COOKIES_FILE", raising=False)
    monkeypatch.delenv("BSA_COOKIES_FROM_BROWSER", raising=False)


@pytest.fixture(autouse=True)
def _which_finds_yt_dlp(monkeypatch):
    """Most tests want the "binary is installed" path; the one test that
    cares about a missing binary overrides this itself."""
    monkeypatch.setattr("brightspace_agent.media.fetch.shutil.which", lambda name: "/usr/bin/yt-dlp")


@pytest.fixture
def dest_dir(tmp_path) -> Path:
    d = tmp_path / "dest"
    d.mkdir()
    return d


# --------------------------------------------------------------------------
# Fake `run` -- records every call's argv/kwargs and plays back a queue of
# canned reactions (write files / return a CompletedProcess / raise).
# --------------------------------------------------------------------------


class _FakeRun:
    def __init__(self, *reactions):
        self.calls: list[tuple[list[str], dict]] = []
        self._reactions = list(reactions)

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if not self._reactions:
            raise AssertionError(f"fake run invoked more times than expected; argv={argv!r}")
        return self._reactions.pop(0)(argv, kwargs)


def _ok(stderr: str = "", write: list[tuple[Path, bytes]] | None = None):
    def react(argv, kwargs):
        for path, content in write or []:
            path.write_bytes(content)
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr=stderr)

    return react


def _fail(stderr: str):
    def react(argv, kwargs):
        return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr=stderr)

    return react


def _times_out(timeout_s: float):
    def react(argv, kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout_s)

    return react


# --------------------------------------------------------------------------
# Phase 1: captions (zoom, gdrive)
# --------------------------------------------------------------------------


def test_zoom_captions_phase_argv_and_result(dest_dir):
    fake_run = _FakeRun(_ok(write=[(dest_dir / "Lecture.transcript.vtt", b"WEBVTT\n")]))
    settings = Settings()
    fetcher = YtDlpFetcher(settings, run=fake_run)
    spec = FetchSpec(platform="zoom", url="https://zoom.us/rec/share/abc123", passcode="s3cret", dest_dir=dest_dir)

    result = fetcher.fetch(spec)

    assert result == FetchResult(kind="captions", path=dest_dir / "Lecture.transcript.vtt")
    assert len(fake_run.calls) == 1
    argv, kwargs = fake_run.calls[0]
    assert argv[0] == "/usr/bin/yt-dlp"
    assert spec.url in argv
    assert "--skip-download" in argv
    assert "--write-subs" in argv
    assert "--sub-langs" in argv and "all" in argv
    assert "--cookies-from-browser" in argv and "chrome" in argv
    assert "--video-password" in argv and "s3cret" in argv
    assert "--no-playlist" in argv
    # run() contract: capture_output/text always set, check=True never, shell never.
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("check") is not True
    assert "shell" not in kwargs
    assert kwargs.get("timeout") == settings.media_fetch_timeout_s


def test_captions_no_passcode_omits_video_password_flag(dest_dir):
    fake_run = _FakeRun(_ok(write=[(dest_dir / "Lecture.en.vtt", b"WEBVTT\n")]))
    fetcher = YtDlpFetcher(Settings(), run=fake_run)
    spec = FetchSpec(platform="zoom", url="https://zoom.us/rec/share/abc123", passcode=None, dest_dir=dest_dir)

    fetcher.fetch(spec)

    argv, _ = fake_run.calls[0]
    assert "--video-password" not in argv


def test_caption_preference_transcript_over_cc_over_en_over_first_sorted(dest_dir):
    """Same four files (a plain one, an .en one, a .cc one, a .transcript
    one) fed to the picker at shrinking visibility: with all four present
    transcript wins; drop it and cc wins; drop that and .en wins; with only
    the plain file left, it's picked by elimination (first-sorted)."""
    plain = dest_dir / "aaa-plain.vtt"
    en = dest_dir / "bbb.en.vtt"
    cc = dest_dir / "ccc.cc.vtt"
    transcript = dest_dir / "ddd.transcript.vtt"
    for f in (plain, en, cc, transcript):
        f.write_text("WEBVTT\n")

    def fetch_with(files):
        for f in (plain, en, cc, transcript):
            f.unlink(missing_ok=True)
        for f in files:
            f.write_text("WEBVTT\n")
        fake_run = _FakeRun(_ok())
        fetcher = YtDlpFetcher(Settings(), run=fake_run)
        spec = FetchSpec(platform="gdrive", url="https://drive.google.com/file/d/x/view", passcode=None, dest_dir=dest_dir)
        return fetcher.fetch(spec).path

    assert fetch_with([plain, en, cc, transcript]) == transcript
    assert fetch_with([plain, en, cc]) == cc
    assert fetch_with([plain, en]) == en
    assert fetch_with([plain]) == plain


def test_zoom_no_vtt_from_phase1_falls_through_to_phase2_audio(dest_dir):
    fake_run = _FakeRun(
        _ok(),  # phase 1: zero exit, no .vtt written
        _ok(write=[(dest_dir / "Lecture.m4a", b"fake-audio-bytes")]),  # phase 2
    )
    fetcher = YtDlpFetcher(Settings(), run=fake_run)
    spec = FetchSpec(platform="zoom", url="https://zoom.us/rec/share/abc123", passcode=None, dest_dir=dest_dir)

    result = fetcher.fetch(spec)

    assert result == FetchResult(kind="audio", path=dest_dir / "Lecture.m4a")
    assert len(fake_run.calls) == 2
    phase1_argv, _ = fake_run.calls[0]
    phase2_argv, _ = fake_run.calls[1]
    assert "--write-subs" in phase1_argv
    assert "-f" in phase2_argv and "bestaudio" in phase2_argv
    assert "-x" in phase2_argv
    assert "--audio-format" in phase2_argv and "m4a" in phase2_argv


def test_mediasite_skips_captions_phase_entirely(dest_dir):
    fake_run = _FakeRun(_ok(write=[(dest_dir / "Lecture.m4a", b"fake-audio-bytes")]))
    fetcher = YtDlpFetcher(Settings(), run=fake_run)
    spec = FetchSpec(
        platform="mediasite", url="https://mediasite.example.edu/Mediasite/Play/xyz", passcode=None, dest_dir=dest_dir
    )

    result = fetcher.fetch(spec)

    assert result.kind == "audio"
    assert len(fake_run.calls) == 1  # only the audio call, never captions
    argv, _ = fake_run.calls[0]
    assert "--write-subs" not in argv
    assert "-f" in argv and "bestaudio" in argv


def test_audio_phase_zero_exit_no_file_is_extractor_error(dest_dir):
    fake_run = _FakeRun(_ok())  # zero exit, nothing written
    fetcher = YtDlpFetcher(Settings(), run=fake_run)
    spec = FetchSpec(platform="mediasite", url="https://mediasite.example.edu/Mediasite/Play/xyz", passcode=None, dest_dir=dest_dir)

    with pytest.raises(MediaFetchError) as exc_info:
        fetcher.fetch(spec)

    assert exc_info.value.kind == "extractor_error"


# --------------------------------------------------------------------------
# stderr -> MediaFetchError mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "stderr", "expected_kind", "message_substring"),
    [
        (
            "zoom",
            "ERROR: This webinar is protected, please use --video-password (validate_passwd failed)",
            "wrong_passcode",
            "passcode",
        ),
        (
            "zoom",
            "ERROR: [zoom] Password is required for this video",
            "wrong_passcode",
            "passcode",
        ),
        (
            "gdrive",
            "ERROR: [GoogleDrive] xyz: Permission denied: HTTP Error 403: Forbidden",
            "downloads_disabled",
            "owner",
        ),
        (
            "gdrive",
            "ERROR: [GoogleDrive] cannot download file, the owner disabled downloads",
            "downloads_disabled",
            "owner",
        ),
        (
            "zoom",
            "ERROR: Please sign in to access this recording; login required",
            "auth_expired",
            "chrome",
        ),
        (
            "zoom",
            "ERROR: Unable to download webpage: authentication cookies missing",
            "auth_expired",
            "chrome",
        ),
        (
            "gdrive",
            "ERROR: Unsupported URL: https://drive.google.com/file/d/x/view",
            "not_found",
            "dead or has moved",
        ),
        (
            "gdrive",
            "ERROR: 404: this file was not found, or is no longer available",
            "not_found",
            "dead or has moved",
        ),
        (
            "zoom",
            "ERROR: some completely unrelated garbage from yt-dlp's internals",
            "extractor_error",
            "uv lock --upgrade-package yt-dlp",
        ),
    ],
)
def test_error_mapping(dest_dir, platform, stderr, expected_kind, message_substring):
    fake_run = _FakeRun(_fail(stderr))
    fetcher = YtDlpFetcher(Settings(), run=fake_run)
    spec = FetchSpec(platform=platform, url="https://example.com/rec", passcode="pw" if platform == "zoom" else None, dest_dir=dest_dir)

    with pytest.raises(MediaFetchError) as exc_info:
        fetcher.fetch(spec)

    assert exc_info.value.kind == expected_kind
    assert message_substring.lower() in exc_info.value.user_message.lower()


def test_gdrive_403_wins_over_auth_expired_when_stderr_mentions_both(dest_dir):
    """The auth_expired substrings (login/sign in/authentication/cookies) and
    the gdrive-403 case can both appear in the same yt-dlp message (it often
    suggests trying --cookies after a 403) -- downloads_disabled must win."""
    stderr = "ERROR: [GoogleDrive] 403: Forbidden -- try using --cookies if this is a private file"
    fake_run = _FakeRun(_fail(stderr))
    fetcher = YtDlpFetcher(Settings(), run=fake_run)
    spec = FetchSpec(platform="gdrive", url="https://drive.google.com/file/d/x/view", passcode=None, dest_dir=dest_dir)

    with pytest.raises(MediaFetchError) as exc_info:
        fetcher.fetch(spec)

    assert exc_info.value.kind == "downloads_disabled"


def test_error_stderr_tail_logged_at_warning(dest_dir, caplog):
    import logging

    fake_run = _FakeRun(_fail("ERROR: garbage " * 100))  # long enough to exercise the ~500-char tail
    fetcher = YtDlpFetcher(Settings(), run=fake_run)
    spec = FetchSpec(platform="zoom", url="https://example.com/rec", passcode=None, dest_dir=dest_dir)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(MediaFetchError):
            fetcher.fetch(spec)

    assert any("garbage" in record.message for record in caplog.records)


# --------------------------------------------------------------------------
# Cookies
# --------------------------------------------------------------------------


def test_cookies_file_set_uses_cookies_flag_not_browser(dest_dir, tmp_path):
    cookies_path = tmp_path / "cookies.txt"
    cookies_path.write_text("# netscape cookie file\n")
    settings = Settings(cookies_file=cookies_path)
    fake_run = _FakeRun(_ok(write=[(dest_dir / "Lecture.transcript.vtt", b"WEBVTT\n")]))
    fetcher = YtDlpFetcher(settings, run=fake_run)
    spec = FetchSpec(platform="zoom", url="https://zoom.us/rec/share/abc", passcode=None, dest_dir=dest_dir)

    fetcher.fetch(spec)

    argv, _ = fake_run.calls[0]
    assert "--cookies" in argv
    assert str(cookies_path) in argv
    assert "--cookies-from-browser" not in argv


# --------------------------------------------------------------------------
# Timeout
# --------------------------------------------------------------------------


def test_timeout_raises_extractor_error(dest_dir):
    settings = Settings()
    fake_run = _FakeRun(_times_out(settings.media_fetch_timeout_s))
    fetcher = YtDlpFetcher(settings, run=fake_run)
    spec = FetchSpec(platform="zoom", url="https://zoom.us/rec/share/abc", passcode=None, dest_dir=dest_dir)

    with pytest.raises(MediaFetchError) as exc_info:
        fetcher.fetch(spec)

    assert exc_info.value.kind == "extractor_error"
    assert "timed out" in exc_info.value.user_message.lower() or "timeout" in exc_info.value.user_message.lower()


# --------------------------------------------------------------------------
# Missing binary
# --------------------------------------------------------------------------


def test_which_returns_none_raises_not_installed(dest_dir, monkeypatch):
    monkeypatch.setattr("brightspace_agent.media.fetch.shutil.which", lambda name: None)
    fake_run = _FakeRun()  # must never be called
    fetcher = YtDlpFetcher(Settings(), run=fake_run)
    spec = FetchSpec(platform="zoom", url="https://zoom.us/rec/share/abc", passcode=None, dest_dir=dest_dir)

    with pytest.raises(MediaFetchError) as exc_info:
        fetcher.fetch(spec)

    assert exc_info.value.kind == "not_installed"
    assert "uv sync --group media" in exc_info.value.user_message
    assert fake_run.calls == []


# --------------------------------------------------------------------------
# MockMediaFetcher
# --------------------------------------------------------------------------


def test_mock_fetcher_captions_url_writes_vtt(dest_dir):
    fetcher = MockMediaFetcher()
    spec = FetchSpec(platform="zoom", url="https://zoom.us/rec/share/mock-captions", passcode=None, dest_dir=dest_dir)

    result = fetcher.fetch(spec)

    assert result.kind == "captions"
    assert result.path.parent == dest_dir
    text = result.path.read_text(encoding="utf-8")
    assert text.startswith("WEBVTT")
    assert text.count("-->") == 2  # exactly two cues


def test_mock_fetcher_fail_url_raises_matching_kind(dest_dir):
    fetcher = MockMediaFetcher()
    spec = FetchSpec(platform="gdrive", url="https://drive.google.com/mock-fail-downloads_disabled", passcode=None, dest_dir=dest_dir)

    with pytest.raises(MediaFetchError) as exc_info:
        fetcher.fetch(spec)

    assert exc_info.value.kind == "downloads_disabled"


def test_mock_fetcher_fail_url_covers_every_error_kind(dest_dir):
    fetcher = MockMediaFetcher()
    for kind in ("not_installed", "auth_expired", "wrong_passcode", "downloads_disabled", "not_found", "extractor_error"):
        spec = FetchSpec(platform="zoom", url=f"https://zoom.us/rec/share/mock-fail-{kind}", passcode=None, dest_dir=dest_dir)
        with pytest.raises(MediaFetchError) as exc_info:
            fetcher.fetch(spec)
        assert exc_info.value.kind == kind


def test_mock_fetcher_otherwise_writes_placeholder_audio(dest_dir):
    fetcher = MockMediaFetcher()
    spec = FetchSpec(platform="mediasite", url="https://mediasite.example.edu/Mediasite/Play/xyz", passcode=None, dest_dir=dest_dir)

    result = fetcher.fetch(spec)

    assert result.kind == "audio"
    assert result.path == dest_dir / "audio.m4a"
    assert result.path.read_bytes()  # a few bytes, non-empty


# --------------------------------------------------------------------------
# make_media_fetcher selection
# --------------------------------------------------------------------------


def test_make_media_fetcher_mock_under_bsa_mock_media(monkeypatch):
    monkeypatch.setenv("BSA_MOCK_MEDIA", "1")
    settings = Settings()

    assert isinstance(make_media_fetcher(settings), MockMediaFetcher)


def test_make_media_fetcher_mock_under_bsa_mock_llm(monkeypatch):
    monkeypatch.setenv("BSA_MOCK_LLM", "1")
    settings = Settings()

    assert isinstance(make_media_fetcher(settings), MockMediaFetcher)


def test_make_media_fetcher_real_otherwise():
    settings = Settings()  # mock_media/mock_llm both default False

    assert isinstance(make_media_fetcher(settings), YtDlpFetcher)
