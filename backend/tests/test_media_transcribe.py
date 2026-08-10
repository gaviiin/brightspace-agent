"""Tests for the M2.3 transcriber (media/transcribe.py): the pure
`segments_to_vtt` VTT renderer, `MockTranscriber`'s deterministic offline
output, `ParakeetTranscriber`'s lazy-import error mapping (no real
parakeet-mlx/static-ffmpeg needed -- the import seam is monkeypatched), and
`make_transcriber`'s mock/real selection.

No real ASR engine or subprocess anywhere in this file -- see
`test_parakeet_transcriber_real_engine_smoke` for the one optional exception,
which is skipped unless the real `media` dependency group is installed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from brightspace_agent.config import Settings
from brightspace_agent.ingest.extract import extract_text
from brightspace_agent.media.transcribe import (
    MediaTranscribeError,
    MockTranscriber,
    ParakeetTranscriber,
    make_transcriber,
    segments_to_vtt,
)


@pytest.fixture(autouse=True)
def _no_ambient_media_env(monkeypatch):
    """A real key/flag on the host running these tests must not change
    make_transcriber's choice."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BSA_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BSA_MOCK_LLM", raising=False)
    monkeypatch.delenv("BSA_MOCK_MEDIA", raising=False)


@pytest.fixture
def dest_dir(tmp_path) -> Path:
    d = tmp_path / "dest"
    d.mkdir()
    return d


# --------------------------------------------------------------------------
# segments_to_vtt -- pure function, no engine involved
# --------------------------------------------------------------------------


def test_segments_to_vtt_header_and_single_cue():
    vtt = segments_to_vtt([(0.0, 2.5, "Hello world.")])

    assert vtt == "WEBVTT\n\n00:00:00.000 --> 00:00:02.500\nHello world.\n"


def test_segments_to_vtt_multi_cue_blank_line_between():
    vtt = segments_to_vtt(
        [
            (0.0, 1.0, "First cue."),
            (1.0, 2.0, "Second cue."),
        ]
    )

    assert vtt == (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:01.000\n"
        "First cue.\n\n"
        "00:00:01.000 --> 00:00:02.000\n"
        "Second cue.\n"
    )
    # exactly one blank line separates the two cues, not more
    assert "\n\n\n" not in vtt


def test_segments_to_vtt_skips_empty_or_whitespace_text():
    vtt = segments_to_vtt(
        [
            (0.0, 1.0, "Real cue."),
            (1.0, 2.0, ""),
            (2.0, 3.0, "   "),
            (3.0, 4.0, "Another real cue."),
        ]
    )

    assert vtt.count("-->") == 2
    assert "Real cue." in vtt
    assert "Another real cue." in vtt


def test_segments_to_vtt_clamps_end_before_start_to_start():
    vtt = segments_to_vtt([(5.0, 2.0, "Backwards cue.")])

    assert "00:00:05.000 --> 00:00:05.000" in vtt


def test_segments_to_vtt_zero_padded_hours_over_one_hour():
    vtt = segments_to_vtt([(3661.25, 3662.5, "Over an hour in.")])

    assert "01:01:01.250 --> 01:01:02.500" in vtt


def test_segments_to_vtt_no_cues_still_has_header():
    vtt = segments_to_vtt([])

    assert vtt.startswith("WEBVTT")
    assert "-->" not in vtt


# --------------------------------------------------------------------------
# MockTranscriber
# --------------------------------------------------------------------------


def test_mock_transcriber_writes_parseable_vtt_with_stem_provenance(dest_dir):
    audio_path = Path("/somewhere/Lecture 3.m4a")
    transcriber = MockTranscriber()

    result_path = transcriber.transcribe(audio_path, dest_dir)

    assert result_path.parent == dest_dir
    assert result_path.suffix == ".vtt"
    raw = result_path.read_text(encoding="utf-8")
    assert raw.startswith("WEBVTT")
    assert raw.count("-->") == 2
    assert "Lecture 3" in raw

    text = extract_text(result_path, "text/vtt", "transcript")
    assert text is not None
    assert "Lecture 3" in text
    assert "-->" not in text
    assert "WEBVTT" not in text


# --------------------------------------------------------------------------
# ParakeetTranscriber -- lazy import seam monkeypatched, no real deps
# --------------------------------------------------------------------------


def test_parakeet_transcriber_missing_deps_raises_not_installed(monkeypatch, dest_dir, tmp_path):
    import brightspace_agent.media.transcribe as transcribe_mod

    def _boom():
        raise ImportError("no module named parakeet_mlx")

    monkeypatch.setattr(transcribe_mod, "_import_parakeet_deps", _boom)

    audio_path = tmp_path / "lecture.m4a"
    audio_path.write_bytes(b"fake-audio")
    transcriber = ParakeetTranscriber(Settings())

    with pytest.raises(MediaTranscribeError) as exc_info:
        transcriber.transcribe(audio_path, dest_dir)

    assert exc_info.value.kind == "not_installed"
    assert "uv sync --group media" in exc_info.value.user_message


class _FakeSentence:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


class _FakeResult:
    def __init__(self, sentences):
        self.sentences = sentences
        self.text = " ".join(s.text for s in sentences)


class _FakeModel:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def transcribe(self, path, **kwargs):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeParakeetModule:
    def __init__(self, model):
        self._model = model
        self.requested_model_id = None

    def from_pretrained(self, model_id):
        self.requested_model_id = model_id
        return self._model


class _FakeStaticFfmpegModule:
    def __init__(self):
        self.add_paths_called = False

    def add_paths(self):
        self.add_paths_called = True


def test_parakeet_transcriber_engine_success_writes_vtt_from_sentences(monkeypatch, dest_dir, tmp_path):
    import brightspace_agent.media.transcribe as transcribe_mod

    fake_model = _FakeModel(
        result=_FakeResult(
            [
                _FakeSentence(0.0, 1.5, "First sentence."),
                _FakeSentence(1.5, 3.0, "Second sentence."),
            ]
        )
    )
    fake_parakeet = _FakeParakeetModule(fake_model)
    fake_ffmpeg = _FakeStaticFfmpegModule()
    monkeypatch.setattr(
        transcribe_mod, "_import_parakeet_deps", lambda: (fake_parakeet, fake_ffmpeg)
    )

    settings = Settings(asr_model="mlx-community/parakeet-tdt-0.6b-v3")
    audio_path = tmp_path / "lecture.m4a"
    audio_path.write_bytes(b"fake-audio")
    transcriber = ParakeetTranscriber(settings)

    result_path = transcriber.transcribe(audio_path, dest_dir)

    assert fake_ffmpeg.add_paths_called is True
    assert fake_parakeet.requested_model_id == "mlx-community/parakeet-tdt-0.6b-v3"
    assert result_path.parent == dest_dir
    raw = result_path.read_text(encoding="utf-8")
    assert raw.startswith("WEBVTT")
    assert "First sentence." in raw
    assert "Second sentence." in raw
    assert raw.count("-->") == 2


def test_parakeet_transcriber_engine_exception_raises_engine_error(monkeypatch, dest_dir, tmp_path):
    import brightspace_agent.media.transcribe as transcribe_mod

    fake_model = _FakeModel(error=RuntimeError("mlx blew up"))
    fake_parakeet = _FakeParakeetModule(fake_model)
    fake_ffmpeg = _FakeStaticFfmpegModule()
    monkeypatch.setattr(
        transcribe_mod, "_import_parakeet_deps", lambda: (fake_parakeet, fake_ffmpeg)
    )

    audio_path = tmp_path / "lecture.m4a"
    audio_path.write_bytes(b"fake-audio")
    transcriber = ParakeetTranscriber(Settings())

    with pytest.raises(MediaTranscribeError) as exc_info:
        transcriber.transcribe(audio_path, dest_dir)

    assert exc_info.value.kind == "engine_error"
    assert "mlx blew up" in exc_info.value.user_message


def test_parakeet_transcriber_unrenderable_timestamps_raise_engine_error(monkeypatch, dest_dir, tmp_path):
    """A garbage timestamp out of the engine (NaN here) blows up in VTT
    RENDERING, not in the engine call -- so it only maps to the documented
    'engine_error' contract if rendering/writing is inside the same try.
    Otherwise a bare ValueError escapes `transcribe()` and the runner
    records the source as 'internal: ...' instead of a real engine error."""
    import brightspace_agent.media.transcribe as transcribe_mod

    fake_model = _FakeModel(result=_FakeResult([_FakeSentence(float("nan"), 1.0, "Unrenderable.")]))
    fake_parakeet = _FakeParakeetModule(fake_model)
    fake_ffmpeg = _FakeStaticFfmpegModule()
    monkeypatch.setattr(
        transcribe_mod, "_import_parakeet_deps", lambda: (fake_parakeet, fake_ffmpeg)
    )

    audio_path = tmp_path / "lecture.m4a"
    audio_path.write_bytes(b"fake-audio")
    transcriber = ParakeetTranscriber(Settings())

    with pytest.raises(MediaTranscribeError) as exc_info:
        transcriber.transcribe(audio_path, dest_dir)

    assert exc_info.value.kind == "engine_error"


def test_parakeet_transcriber_unwritable_dest_raises_engine_error(monkeypatch, tmp_path):
    """Same contract for the write half: an OS-level failure putting the
    .vtt on disk is still 'the transcription engine step failed', not an
    unmapped exception."""
    import brightspace_agent.media.transcribe as transcribe_mod

    fake_model = _FakeModel(result=_FakeResult([_FakeSentence(0.0, 1.0, "Fine.")]))
    monkeypatch.setattr(
        transcribe_mod, "_import_parakeet_deps", lambda: (_FakeParakeetModule(fake_model), _FakeStaticFfmpegModule())
    )

    audio_path = tmp_path / "lecture.m4a"
    audio_path.write_bytes(b"fake-audio")
    missing_dest = tmp_path / "does" / "not" / "exist"  # never created
    transcriber = ParakeetTranscriber(Settings())

    with pytest.raises(MediaTranscribeError) as exc_info:
        transcriber.transcribe(audio_path, missing_dest)

    assert exc_info.value.kind == "engine_error"


# --------------------------------------------------------------------------
# Base install (`uv sync`, no `--group media`): importing the app must not
# need parakeet_mlx/static_ffmpeg at all. Every other test in this file
# monkeypatches `_import_parakeet_deps`, which would keep passing even if
# someone hoisted those imports to module level -- this one wouldn't.
# --------------------------------------------------------------------------

_BASE_INSTALL_SCRIPT = '''
import sys

BLOCKED = ("parakeet_mlx", "static_ffmpeg")


class _NotInstalled:
    """A meta_path finder that makes the media group look absent even on a
    machine where it is installed."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            raise ImportError(f"simulated base install: no module named {fullname}")
        return None


for name in [n for n in sys.modules if n.split(".")[0] in BLOCKED]:
    del sys.modules[name]
sys.meta_path.insert(0, _NotInstalled())

import brightspace_agent.main  # noqa: F401  -- the whole app's import graph
from brightspace_agent.config import Settings
from brightspace_agent.media.transcribe import MediaTranscribeError, ParakeetTranscriber

leaked = sorted(n for n in sys.modules if n.split(".")[0] in BLOCKED)
assert not leaked, f"media-group modules imported at module level: {leaked}"

# ... and the seam still degrades to the documented, actionable error rather
# than an ImportError traceback when the engine is genuinely missing.
from pathlib import Path

try:
    ParakeetTranscriber(Settings()).transcribe(Path("/nonexistent/audio.m4a"), Path("/nonexistent"))
except MediaTranscribeError as exc:
    assert exc.kind == "not_installed", exc.kind
    assert "uv sync --group media" in exc.user_message
else:
    raise AssertionError("expected MediaTranscribeError('not_installed')")

print("BASE_INSTALL_OK")
'''


def test_app_imports_cleanly_without_the_media_dependency_group(tmp_path):
    """Runs in a SUBPROCESS on purpose: the blocker has to be installed
    before `brightspace_agent.main` is imported for the first time, and this
    process imported it long ago (conftest/other tests), so no amount of
    sys.modules surgery here would exercise the real import path."""
    import subprocess
    import sys as _sys

    env = {**os.environ, "BSA_DATA_DIR": str(tmp_path)}
    result = subprocess.run(
        [_sys.executable, "-c", _BASE_INSTALL_SCRIPT], capture_output=True, text=True, env=env
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "BASE_INSTALL_OK" in result.stdout


# --------------------------------------------------------------------------
# make_transcriber selection
# --------------------------------------------------------------------------


def test_make_transcriber_mock_under_bsa_mock_media(monkeypatch):
    monkeypatch.setenv("BSA_MOCK_MEDIA", "1")
    settings = Settings()

    assert isinstance(make_transcriber(settings), MockTranscriber)


def test_make_transcriber_mock_under_bsa_mock_llm(monkeypatch):
    monkeypatch.setenv("BSA_MOCK_LLM", "1")
    settings = Settings()

    assert isinstance(make_transcriber(settings), MockTranscriber)


def test_make_transcriber_real_otherwise():
    settings = Settings()  # mock_media/mock_llm both default False

    assert isinstance(make_transcriber(settings), ParakeetTranscriber)


# --------------------------------------------------------------------------
# Optional: real engine integration smoke test -- skipped unless BOTH the
# `media` dependency group's heavy deps are installed AND the model weights
# are already sitting in the local Hugging Face cache. The cache check
# matters as much as the import check: `from_pretrained` on a cold cache
# reaches out to the network to download ~600MB of weights, which can hang
# indefinitely in a sandboxed/offline test run -- exactly what "must be
# skipped (not failed) in the base environment" and "keep it fast" rule out.
# Only a machine that has already run parakeet-mlx once (warming the cache)
# exercises this test; everyone else skips it, deterministically and fast.
# --------------------------------------------------------------------------

_ASR_MODEL_ID = Settings().asr_model


def _real_engine_smoke_available() -> bool:
    try:
        import parakeet_mlx  # noqa: F401
        import static_ffmpeg  # noqa: F401
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return False

    # Both files, not just config.json: config.json is tiny and downloads
    # near-instantly, so checking it alone would judge a model "cached" while
    # its ~600MB model.safetensors is still mid-download (exactly what
    # happened while developing this test) -- the smoke test would then
    # silently resume a slow network fetch instead of skipping.
    return all(
        isinstance(try_to_load_from_cache(_ASR_MODEL_ID, filename), str)
        for filename in ("config.json", "model.safetensors")
    )


@pytest.mark.skipif(
    not _real_engine_smoke_available(),
    reason="media deps not installed or model weights not already cached locally (no network in tests)",
)
def test_parakeet_transcriber_real_engine_smoke(dest_dir, tmp_path):
    import struct
    import wave

    audio_path = tmp_path / "tone.wav"
    with wave.open(str(audio_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        frames = b"".join(struct.pack("<h", 0) for _ in range(16000))  # 1s of silence
        wav_file.writeframes(frames)

    transcriber = ParakeetTranscriber(Settings())
    result_path = transcriber.transcribe(audio_path, dest_dir)

    assert result_path.exists()
    assert result_path.read_text(encoding="utf-8").startswith("WEBVTT")
