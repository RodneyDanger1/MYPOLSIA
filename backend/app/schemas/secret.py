"""Request/response models for the DeepSeek key lifecycle (`/config/deepseek-key`).

The response models exist to make the leak impossible by construction: there is no
field that can carry the raw key, so `response_model=` drops anything an
implementation mistake might add (e.g. an extra `"key"` entry in the dict passed to
`DeepSeekKeyStatus(**…)` would be discarded by pydantic before serialization).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DeepSeekKeyUpdate(BaseModel):
    """`PUT /config/deepseek-key` body. Blank/whitespace is refused by the service (E_VALIDATION)."""

    api_key: str = Field(..., description="the DeepSeek API key to store; never echoed back")


class DeepSeekVerifyRequest(BaseModel):
    """`POST /config/deepseek-key/verify` body — all fields optional.

    With a candidate the probe checks that key WITHOUT storing it (verify-before-save);
    without one it checks whatever is configured (stored, else env).
    """

    api_key: str | None = Field(
        default=None, description="optional candidate key to probe instead of the configured one"
    )


class DeepSeekKeyStatus(BaseModel):
    """Masked status only — the raw key has no representation here on purpose."""

    configured: bool
    source: Literal["stored", "env", "none"]
    masked: str


class DeepSeekVerifyResult(BaseModel):
    """The outcome of a real `GET https://api.deepseek.com/models` probe.

    `ok=False` always carries the genuine reason (HTTP status + provider message,
    timeout, transport error) — a verification that could not run never reports success.
    """

    ok: bool
    detail: str
