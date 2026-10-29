"""SV-W4-05 subset (wave 4, forge territory) — the ws caps must be REAL settings, not phantoms.

Measured by `systems-validator` (`VALIDATION-MATRIX.md` §5, `raw/w4/config-census.json`): the socket
caps and the ticket TTL are read as `getattr(settings, "<name>", <default>)` in
`app/api/v1/ws.py:76-81`, but those names were **not** `Settings` fields.  `Settings` is
`extra="ignore"`, so `WS_MAX_SOCKETS_PER_OPERATOR=1` in the environment (or `.env`) was silently
discarded and the code always ran with the hardcoded default — the code claimed a tunable bound
that no deployment could actually set.  That is a false capability claim in the R-W03-1 file.

Pinned here:

  * the three names are `Settings` fields (a real, declarable knob);
  * an environment value really reaches the Settings object (not ignored as an extra);
  * the ws module's caps are the configured values (not a second, hardcoded constant).
"""
from __future__ import annotations

from app.api.v1 import ws as ws_module
from app.config import Settings, settings

WS_KNOBS = (
    "ws_ticket_ttl_seconds",
    "ws_max_sockets_per_client",
    "ws_max_sockets_per_operator",
    "ws_heartbeat_seconds",
)


def test_the_ws_caps_are_settings_fields() -> None:
    """RED at HEAD: none of the three names existed on Settings, so the env var was inert."""
    missing = [name for name in WS_KNOBS if name not in Settings.model_fields]
    assert missing == [], f"these ws knobs cannot be declared on this deployment: {missing}"


def test_an_environment_value_reaches_the_settings_object(monkeypatch) -> None:
    monkeypatch.setenv("WS_MAX_SOCKETS_PER_CLIENT", "2")
    monkeypatch.setenv("WS_MAX_SOCKETS_PER_OPERATOR", "7")
    monkeypatch.setenv("WS_TICKET_TTL_SECONDS", "45")
    monkeypatch.setenv("WS_HEARTBEAT_SECONDS", "1.5")

    built = Settings(_env_file=None)

    assert built.ws_max_sockets_per_client == 2
    assert built.ws_max_sockets_per_operator == 7
    assert built.ws_ticket_ttl_seconds == 45
    assert built.ws_heartbeat_seconds == 1.5


def test_the_module_caps_are_the_configured_values() -> None:
    """The module's constants are derived from Settings, never a second hardcoded number."""
    assert int(settings.ws_max_sockets_per_client) == ws_module._MAX_SOCKETS_PER_CLIENT
    assert int(settings.ws_max_sockets_per_operator) == ws_module._MAX_SOCKETS_PER_OPERATOR
    assert int(settings.ws_ticket_ttl_seconds) == ws_module._TICKET_TTL_SECONDS
