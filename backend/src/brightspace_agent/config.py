"""Settings and on-disk config (data dir, pairing token)."""

import os
import secrets
import tomllib
from pathlib import Path

import tomli_w
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_FILENAME = "config.toml"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BSA_", populate_by_name=True)

    data_dir: Path = Path("~/.brightspace-agent").expanduser()
    host: str = "127.0.0.1"
    port: int = 8730

    # LLM layer (agents/llm.py). `anthropic_api_key` also honors the plain
    # `ANTHROPIC_API_KEY` env var (the SDK/CLI convention), with the
    # `BSA_`-prefixed name taking precedence if both are set.
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BSA_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )
    fast_model: str = "claude-haiku-4-5-20251001"
    smart_model: str = "claude-sonnet-5"
    mock_llm: bool = False

    # Media layer (media/fetch.py, Task M2.2). yt-dlp cookie source for
    # gated recordings (Zoom/Drive) -- `cookies_file` (a Netscape cookies.txt
    # exported by the user) wins when set; otherwise yt-dlp reads live
    # cookies straight out of the named browser's profile. `mock_media`
    # forces the offline fetcher even when yt-dlp is installed (same idea as
    # `mock_llm`); `make_media_fetcher` also treats `mock_llm` as forcing it,
    # so an offline test/e2e run never spawns a subprocess either way.
    # `keep_media` is read by a later task (M2.4's cleanup step) -- just the
    # setting lives here for now.
    cookies_from_browser: str = "chrome"
    cookies_file: Path | None = None
    mock_media: bool = False
    media_fetch_timeout_s: int = 1800
    keep_media: bool = False

    # Transcription (media/transcribe.py, Task M2.3): the Hugging Face model
    # id `ParakeetTranscriber` loads via parakeet-mlx's `from_pretrained`.
    # Only read at real-transcribe time -- `make_transcriber` picks the mock
    # under the same `mock_media`/`mock_llm` rule as `make_media_fetcher`.
    asr_model: str = "mlx-community/parakeet-tdt-0.6b-v3"

    # Pipeline runner (Task 9): a hard-ish per-run spend guard shared across
    # the summarize and classify stages (see pipeline/runner.py). Advisory,
    # like the rest of cost estimation -- see agents/llm.py's cost table.
    #
    # "Hard-ish", not exact (Task 13): each capped stage fans out across
    # `_CAPPED_STAGE_CONCURRENCY` (4, pipeline/graph.py) materials at once,
    # and the cap check for the next paid call happens optimistically -- a
    # worker checks "are we under the cap yet?", and only *after* its LLM
    # call completes does it record what that call cost. Up to
    # `_CAPPED_STAGE_CONCURRENCY` workers can therefore all pass the check
    # before any of them has recorded its spend, so actual spend for a run
    # can overshoot this cap by at most
    # `_CAPPED_STAGE_CONCURRENCY x (one call's cost)`. The alternative --
    # capping concurrency at 1 for an exact, race-free check -- was this
    # project's original Task 9 design; it traded away real fan-out
    # throughput (summarize/classify ran ~4x slower on a large course) for
    # a guarantee this background job, run by a single local user, doesn't
    # need to be exact about.
    #
    # Two more documented gaps between this number and a real bill (M3):
    #
    # 1. NON-TOKEN COSTS. Anthropic's `web_search` server tool is billed per
    #    search (~$0.01) on top of tokens, and the M3 enrich stage runs up to
    #    8 searches per finder, ~5 finders per topic. That fee IS counted
    #    against this cap (agents/web.py's `_usage_info` folds it into
    #    `est_cost_usd` -- from the response's own `server_tool_use` blocks
    #    when they're available, otherwise charged at the `max_uses` upper
    #    bound), and api/enrichment.py's dry-run shows it, both via
    #    `WEB_SEARCH_COST_PER_SEARCH_USD` in agents/llm.py. But it is an
    #    ESTIMATE from a hard-coded price, exactly like the token table:
    #    if Anthropic's pricing moves and that constant doesn't, this cap
    #    moves with it.
    # 2. THE ENRICH BATCH APPLIES THIS CAP PER TOPIC, NOT PER RUN (an
    #    accepted M3.1 limitation -- see pipeline/runner.py's
    #    `start_enrichment` docstring): a course-wide enrichment of N topics
    #    can therefore spend up to ~N x this cap.
    max_cost_usd_per_run: float = 5.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "brightspace.db"

    @property
    def blobs_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def text_dir(self) -> Path:
        return self.data_dir / "text"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"


def ensure_data_dir(settings: Settings) -> dict:
    """Ensure the data dir and config.toml exist; return the parsed config.

    On first run, creates the data dir and writes config.toml (mode 0600)
    with a freshly generated pairing token. On later runs, just reads and
    returns the existing config.toml.
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    config_path = settings.data_dir / CONFIG_FILENAME

    if not config_path.exists():
        config = {"pairing_token": secrets.token_urlsafe(32)}
        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tomli_w.dumps(config))
        return config

    with config_path.open("rb") as f:
        return tomllib.load(f)
