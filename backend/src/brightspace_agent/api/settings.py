"""`GET /api/settings`: the Settings page's read model -- pairing token,
data dir, configured models, mock-LLM flag, per-run cost cap, and whether an
Anthropic API key is configured.

The pairing token IS exposed here on purpose (see the Task 11 brief): it's
the pairing UX itself -- the Settings page is where the user copies it into
the extension popup. This is safe to serve as a plain GET because it's not
CSRF-readable: `main.py`'s CORS middleware restricts the allowed origin to
the frontend's own dev/prod origin, so a response body can't be read back by
a page on another origin even if it could trigger the (side-effect-free)
GET.

The Anthropic API key itself is NEVER serialized -- only the boolean
`apiKeyConfigured`. There is no field anywhere in `SettingsOut` that could
carry the key by accident (`Settings.anthropic_api_key` is never passed to
this model at all, not even a truncated/masked version).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from starlette.requests import Request

from brightspace_agent.api.deps import get_settings
from brightspace_agent.config import Settings

router = APIRouter(prefix="/api", tags=["settings"])


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ModelsOut(CamelModel):
    fast: str
    smart: str


class SettingsOut(CamelModel):
    pairing_token: str
    data_dir: str
    models: ModelsOut
    mock_llm: bool
    max_cost_usd_per_run: float
    api_key_configured: bool


@router.get("/settings", response_model=SettingsOut)
def get_settings_view(request: Request, settings: Settings = Depends(get_settings)) -> SettingsOut:
    return SettingsOut(
        pairing_token=request.app.state.pairing_token,
        data_dir=str(settings.data_dir),
        models=ModelsOut(fast=settings.fast_model, smart=settings.smart_model),
        mock_llm=settings.mock_llm,
        max_cost_usd_per_run=settings.max_cost_usd_per_run,
        api_key_configured=settings.anthropic_api_key is not None,
    )
