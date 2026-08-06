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

    @property
    def db_path(self) -> Path:
        return self.data_dir / "brightspace.db"

    @property
    def blobs_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def text_dir(self) -> Path:
        return self.data_dir / "text"


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
