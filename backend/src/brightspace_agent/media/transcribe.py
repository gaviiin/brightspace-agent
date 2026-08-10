"""M2.3 transcriber: turns a downloaded audio file into a WebVTT transcript
locally, using parakeet-mlx (Apple Silicon only) as a subprocess-free,
in-process engine -- unlike `media/fetch.py`'s yt-dlp, there is no external
binary here, just an optional Python package.

Sibling of `agents/llm.py` / `media/fetch.py`: a small Protocol, a real
engine-backed implementation, a deterministic offline mock, and a
`make_transcriber` selector with the same mock/real rule those modules use.

Two things worth knowing before touching `ParakeetTranscriber`:

- **`parakeet_mlx` and `static_ffmpeg` are imported lazily, behind the
  `_import_parakeet_deps` seam, never at module import time.** The base
  install (`uv sync`, no `--group media`) does not have them, and this
  module must still import cleanly and be testable without them. Tests
  monkeypatch `_import_parakeet_deps` itself (not `sys.modules`) to simulate
  both "not installed" and "installed but the engine raised".
- **VTT rendering is a pure function (`segments_to_vtt`), separate from the
  engine adapter.** The adapter's only job is turning parakeet-mlx's
  `AlignedResult.sentences` (each with `.start`/`.end`/`.text`) into
  `(start_s, end_s, text)` tuples; `segments_to_vtt` does the actual
  formatting and is exercised directly by tests and by `MockTranscriber`,
  with no engine involved.

parakeet-mlx API shape found by reading the installed 0.5.2 package
(`.venv/lib/python3.13/site-packages/parakeet_mlx/`), not from memory:

    from parakeet_mlx import from_pretrained
    model = from_pretrained(model_id)          # downloads/loads from HF hub
    result = model.transcribe(audio_path)      # -> AlignedResult
    result.text                                # str, the whole transcript
    result.sentences                           # list[AlignedSentence]
    sentence.start, sentence.end, sentence.text  # float seconds, float seconds, str

`AlignedSentence.start`/`.end` are computed in `__post_init__` from the
sentence's tokens, so they're already exactly the `(start_s, end_s, text)`
shape `segments_to_vtt` wants -- no further massaging needed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from brightspace_agent.config import Settings

logger = logging.getLogger(__name__)


class MediaTranscribeError(Exception):
    """kind in: 'not_installed' | 'engine_error'. `user_message` is meant to
    be shown to the course owner as-is -- 'not_installed' names the fix
    (`uv sync --group media`), 'engine_error' carries the underlying
    exception text."""

    def __init__(self, kind: str, user_message: str) -> None:
        super().__init__(user_message)
        self.kind = kind
        self.user_message = user_message


class Transcriber(Protocol):
    def transcribe(self, audio_path: Path, dest_dir: Path) -> Path:
        """Returns the path of a .vtt file written inside dest_dir."""
        ...


# --------------------------------------------------------------------------
# segments_to_vtt -- pure VTT rendering, no engine involved
# --------------------------------------------------------------------------


def segments_to_vtt(segments: Sequence[tuple[float, float, str]]) -> str:
    """(start_s, end_s, text) cues -> a complete WebVTT document.

    Header `WEBVTT`, blank line, then cues: `HH:MM:SS.mmm --> HH:MM:SS.mmm`
    (always zero-padded hours), cue text on the following line, blank line
    between cues. Cues whose text is empty/whitespace-only are skipped
    entirely. `end < start` is clamped to `end == start` rather than
    producing a negative-duration cue.
    """
    blocks: list[str] = []
    for start, end, text in segments:
        stripped = text.strip()
        if not stripped:
            continue
        if end < start:
            end = start
        blocks.append(f"{_format_timestamp(start)} --> {_format_timestamp(end)}\n{stripped}")

    if not blocks:
        return "WEBVTT\n\n"
    return "WEBVTT\n\n" + "\n\n".join(blocks) + "\n"


def _format_timestamp(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, remainder_ms = divmod(total_ms, 3_600_000)
    minutes, remainder_ms = divmod(remainder_ms, 60_000)
    secs, ms = divmod(remainder_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


# --------------------------------------------------------------------------
# ParakeetTranscriber
# --------------------------------------------------------------------------


def _import_parakeet_deps():
    """The lazy-import seam: imports `parakeet_mlx` and `static_ffmpeg` and
    returns the two modules. Isolated into its own function (rather than
    inline `import` statements in `ParakeetTranscriber.transcribe`) so tests
    can monkeypatch this single seam -- to raise `ImportError` (simulating
    the base install) or to return fakes (simulating the engine raising) --
    without either package actually being installed.
    """
    import parakeet_mlx
    import static_ffmpeg

    return parakeet_mlx, static_ffmpeg


class ParakeetTranscriber:
    """Real backend: parakeet-mlx running in-process (Apple Silicon only).
    `parakeet_mlx`/`static_ffmpeg` are imported lazily, at `transcribe()`
    time, via `_import_parakeet_deps` -- never at module import time."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def transcribe(self, audio_path: Path, dest_dir: Path) -> Path:
        try:
            parakeet_mlx, static_ffmpeg = _import_parakeet_deps()
        except ImportError as exc:
            raise MediaTranscribeError(
                "not_installed",
                "parakeet-mlx is not installed. Run `uv sync --group media` to install it, then try again.",
            ) from exc

        static_ffmpeg.add_paths()

        # Rendering and writing sit INSIDE the same try as the engine call
        # on purpose: from a caller's point of view "the transcription step
        # failed" is one outcome, and `MediaTranscribeError('engine_error')`
        # is this module's whole contract for it. Left outside, a garbage
        # timestamp out of the adapter (a NaN blows up in `_format_timestamp`,
        # not in `model.transcribe`) or an unwritable `dest_dir` would escape
        # as a bare ValueError/OSError, which pipeline/runner.py's
        # `_process_media_source` records as an opaque 'internal: ...' rather
        # than the engine failure it actually is.
        try:
            model = parakeet_mlx.from_pretrained(self._settings.asr_model)
            result = model.transcribe(audio_path)
            segments = [(sentence.start, sentence.end, sentence.text) for sentence in result.sentences]
            vtt_text = segments_to_vtt(segments)
            dest_path = dest_dir / f"{audio_path.stem}.vtt"
            dest_path.write_text(vtt_text, encoding="utf-8")
        except Exception as exc:
            raise MediaTranscribeError("engine_error", str(exc)) from exc

        return dest_path


# --------------------------------------------------------------------------
# MockTranscriber -- deterministic, offline, zero heavy imports.
# --------------------------------------------------------------------------


class MockTranscriber:
    """Deterministic, offline stand-in for `ParakeetTranscriber`: writes a
    small valid two-cue VTT whose text names the audio file's stem, so a
    test asserting on the output can trace it back to its input. No
    parakeet_mlx/static_ffmpeg import anywhere in this class."""

    def transcribe(self, audio_path: Path, dest_dir: Path) -> Path:
        stem = audio_path.stem
        segments = [
            (0.0, 2.0, f"Mock transcript for {stem}, cue one."),
            (2.0, 4.0, f"Mock transcript for {stem}, cue two."),
        ]
        vtt_text = segments_to_vtt(segments)
        dest_path = dest_dir / f"{stem}.vtt"
        dest_path.write_text(vtt_text, encoding="utf-8")
        return dest_path


# --------------------------------------------------------------------------
# Backend selection -- same rule as make_media_fetcher/make_backend: mock
# when `mock_media` is set, and `mock_llm` also forces it so an offline
# test/e2e run never touches the engine either way.
# --------------------------------------------------------------------------


def make_transcriber(settings: Settings) -> Transcriber:
    if settings.mock_media:
        logger.info("transcriber: mock (BSA_MOCK_MEDIA is set)")
        return MockTranscriber()
    if settings.mock_llm:
        logger.info("transcriber: mock (BSA_MOCK_LLM is set)")
        return MockTranscriber()

    logger.info("transcriber: parakeet-mlx (%s)", settings.asr_model)
    return ParakeetTranscriber(settings)
