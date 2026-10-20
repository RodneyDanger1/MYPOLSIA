"""`/api/v1/integrations` — the Settings → Integrations surface (V26 / F2).

WHAT IT IS
----------
Masked status for the six frozen providers, save/clear through the app's ONE secret store
(`app/services/secret_store.py`), and a `test` action that performs a REAL read-only probe
(`integration_status_service.test_provider`).  The V25 GitHub/Render adapters already read
these same variable names, so a value saved here is the value they use — no second store,
no copy.

THERE IS NO FIELD THAT CAN CARRY A SECRET
-----------------------------------------
Every response is serialized through the pydantic models below; none of them has a field for
a value.  Status is `configured` + `source` + the store's non-reversible `masked` hint.  The
routers never log a request body, and the probe layer scrubs the credential from every string
it returns (`integration_status_service._scrub`).

AUDIT
-----
Every save / clear / test writes an `activity_log` row (`agent_type="integrations"`) whose
`detail` carries the provider, the action, the outcome, the secret NAMES and — on a refusal —
the refusal code.  A value is never in the row.  The refusal path commits the row explicitly
before raising: `get_db` rolls back on exception, so without that commit a failed test would
leave no audit trail at all.

AUTH
----
Router-level `Depends(verify_api_key)`, the same dependency every other owner surface uses.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, verify_api_key
from app.core.errors import PolsiaError
from app.services import integration_status_service
from app.services.activity_service import log_activity

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/integrations", tags=["integrations"], dependencies=[Depends(verify_api_key)]
)

SecretSource = Literal["stored", "env", "none"]
ProviderSource = Literal["stored", "env", "mixed", "none"]


# ── response / request models (a value has no representation here, on purpose) ──────────────


class SecretStatusOut(BaseModel):
    """One credential's masked state.  No `value` field exists, by construction."""

    name: str
    configured: bool
    source: SecretSource
    masked: str


class ProviderStatusOut(BaseModel):
    """One provider's masked state."""

    provider: str
    label: str
    configured: bool
    source: ProviderSource
    read_only: bool
    testable: bool
    secrets: list[SecretStatusOut]


class ProviderListOut(BaseModel):
    providers: list[ProviderStatusOut]


class ProviderValuesUpdate(BaseModel):
    """`PUT /integrations/{provider}` body — variable name -> value.  Never echoed back."""

    values: dict[str, str] = Field(
        default_factory=dict,
        description="secret name -> value, e.g. {\"GITHUB_TOKEN\": \"fake-token\"}; never returned",
    )


class ProviderTestOut(BaseModel):
    """The outcome of a REAL read-only probe.  A failure is a refusal, never `ok: false`."""

    ok: bool
    provider: str
    detail: str


# ── audit ────────────────────────────────────────────────────────────────────────────────────


async def _record(
    db: AsyncSession,
    provider: str,
    action: str,
    outcome: str,
    *,
    secrets: tuple[str, ...] | list[str] = (),
    code: str | None = None,
) -> None:
    """Write the audit row for one action — provider + action + outcome, never a value."""
    detail: dict[str, Any] = {
        "provider": provider,
        "action": action,
        "outcome": outcome,
        "secrets": list(secrets),
    }
    if code:
        detail["code"] = code
    summary = f"{action} {provider}: {outcome}" + (f" ({code})" if code else "")
    try:
        await log_activity(
            db,
            agent_type="integrations",
            action=f"integration_{action}",
            summary=summary,
            level="info" if outcome == "ok" else "warning",
            detail=detail,
        )
    except Exception:  # noqa: BLE001 — an audit write must not replace the real answer
        logger.warning(
            "integrations: could not write the audit row for %s %s", action, provider,
            exc_info=True,
        )


async def _commit_audit(db: AsyncSession) -> None:
    """Commit the audit row before a refusal propagates (`get_db` rolls back on exception)."""
    try:
        await db.commit()
    except Exception:  # noqa: BLE001 — a failed audit commit must not mask the refusal
        logger.warning("integrations: could not commit the audit row", exc_info=True)


# ── reads ────────────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=ProviderListOut)
async def list_integrations() -> ProviderListOut:
    """Masked status for every provider, in the frozen registry order."""
    return ProviderListOut(providers=integration_status_service.list_provider_status())


@router.get("/{provider}", response_model=ProviderStatusOut)
async def read_integration(provider: str) -> ProviderStatusOut:
    """Masked status for ONE provider; an unknown name is the named `E_NOT_FOUND`."""
    spec = integration_status_service.require_provider(provider)
    return ProviderStatusOut(**integration_status_service.provider_status(spec))


# ── writes ───────────────────────────────────────────────────────────────────────────────────


@router.put("/{provider}", response_model=ProviderStatusOut)
async def save_integration(
    provider: str, payload: ProviderValuesUpdate, db: AsyncSession = Depends(get_db)
) -> ProviderStatusOut:
    """Store the supplied values (all-or-nothing) and return the masked status."""
    spec = integration_status_service.require_provider(provider)
    try:
        status = integration_status_service.save_provider_secrets(provider, payload.values)
    except PolsiaError as exc:
        await _record(db, spec.provider, "save", "refused",
                      secrets=tuple(payload.values), code=exc.code)
        await _commit_audit(db)
        raise
    await _record(db, spec.provider, "save", "ok", secrets=tuple(payload.values))
    return ProviderStatusOut(**status)


@router.delete("/{provider}", response_model=ProviderStatusOut)
async def clear_integration(
    provider: str, db: AsyncSession = Depends(get_db)
) -> ProviderStatusOut:
    """Remove this provider's STORED values; an environment value may still configure it."""
    spec = integration_status_service.require_provider(provider)
    try:
        status = integration_status_service.clear_provider_secrets(provider)
    except PolsiaError as exc:
        await _record(db, spec.provider, "clear", "refused", secrets=spec.secrets, code=exc.code)
        await _commit_audit(db)
        raise
    await _record(db, spec.provider, "clear", "ok", secrets=spec.secrets)
    return ProviderStatusOut(**status)


# ── test-connection ──────────────────────────────────────────────────────────────────────────


@router.post("/{provider}/test", response_model=ProviderTestOut)
async def test_integration(
    provider: str, db: AsyncSession = Depends(get_db)
) -> ProviderTestOut:
    """Perform a REAL read-only probe — or refuse by name; never a fabricated success."""
    spec = integration_status_service.require_provider(provider)
    try:
        result = await integration_status_service.test_provider(provider)
    except PolsiaError as exc:
        await _record(db, spec.provider, "test", "refused", secrets=spec.secrets, code=exc.code)
        await _commit_audit(db)
        raise
    await _record(db, spec.provider, "test", "ok", secrets=spec.secrets)
    return ProviderTestOut(**result)
