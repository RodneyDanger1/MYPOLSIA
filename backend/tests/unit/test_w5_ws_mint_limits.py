"""F-03b (wave 5) â€” one client must not be able to occupy the install-wide socket cap.

The defect (wave-4 residual, measured then): a ticket carries a fresh RANDOM SUBJECT per mint and
the socket cap is per SUBJECT (`ws_max_sockets_per_client`, 4).  Nothing bounded the MINT, so a
client that re-minted in a loop could create unlimited identities â€” 4 sockets each â€” and fill the
install-wide operator cap (32), starving every other page.  Minting was also free of any accounting
for the sockets the operator already holds, so an exhausted install kept handing out tickets that
could only be refused.

The fix has two bounds (`app/api/v1/ws.py::_authorise_mint`), both per operator:

  * cap accounting â€” no mint while the operator already holds `ws_max_sockets_per_operator` sockets;
  * mint rate â€” at most `ws_ticket_mint_limit` mints per `ws_ticket_mint_window_seconds`.

Both refuse by NAME (`E_RATE_LIMITED`, HTTP 429 through `refusal_guard`) with a `context` that says
which bound fired and when to retry, and neither consumes the other's budget.  Every assertion here
runs against the REAL route (`POST /api/v1/ws/ticket`) or the real function â€” no re-implementation.
"""
from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient

from app.api.v1 import ws as ws_module
from app.config import settings
from app.core.errors import PolsiaError

KEY = "w5-mint-limit-owner-key"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """A configured owner key and EMPTY module counters: process state must not leak between tests."""
    monkeypatch.setattr(settings, "api_key", KEY)
    monkeypatch.setattr(ws_module, "_mint_times", {})
    monkeypatch.setattr(ws_module, "_operator_sockets", {})
    monkeypatch.setattr(ws_module, "_open_sockets", {})
    yield


@pytest.fixture()
def client():
    from app.main import app

    test_client = TestClient(app)  # no lifespan: the mint route needs no DB/Redis
    try:
        yield test_client
    finally:
        test_client.close()


def _mint(client: TestClient):
    return client.post("/api/v1/ws/ticket", headers={"X-API-Key": KEY})


def _refusal(response) -> dict:
    """The repo's ONE wire refusal shape (`app/main.py`'s coded-HTTPException handler):

    `{"ok": false, "error": {"code", "message", "how_to_fix", "context"}}` â€” the same envelope a
    `PolsiaError` produces via `errors.to_http_detail`, and the same one a 405 carries since
    SV-W4-02 (wave 5).
    """
    body = response.json()
    assert body.get("ok") is False and "error" in body, f"not the named refusal: {body}"
    return body["error"]


# â”€â”€ bound 1: the mint RATE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_a_client_cannot_mint_more_tickets_than_the_window_allows(client, monkeypatch):
    """The denial vector: mint in a loop, get a new identity each time, fill the operator cap."""
    monkeypatch.setattr(settings, "ws_ticket_mint_limit", 3)
    monkeypatch.setattr(settings, "ws_ticket_mint_window_seconds", 60.0)

    tickets = []
    for _ in range(3):
        response = _mint(client)
        assert response.status_code == 200, response.text
        tickets.append(response.json()["ticket"])
    assert len({ticket.split(".")[2] for ticket in tickets}) == 3, "each mint is a new identity"

    refused = _mint(client)
    assert refused.status_code == 429, (
        f"the 4th mint in one window must be refused, got {refused.status_code}: {refused.text}"
    )
    detail = _refusal(refused)
    assert detail["code"] == "E_RATE_LIMITED"
    assert detail["context"]["reason"] == "mint_rate"
    assert detail["context"]["mint_limit"] == 3
    assert detail["context"]["window_seconds"] == 60.0
    assert detail["context"]["mints_in_window"] == 3
    assert detail["context"]["retry_after_seconds"] > 0
    assert "ticket" not in refused.json(), "a refused mint must not hand out a ticket"
    assert refused.headers.get("X-Polsia-Error-Code") == "E_RATE_LIMITED"


def test_the_window_rolls_and_the_budget_comes_back(monkeypatch):
    """A fixed window: stamps older than the window are pruned, not remembered forever."""
    monkeypatch.setattr(settings, "ws_ticket_mint_limit", 2)
    monkeypatch.setattr(settings, "ws_ticket_mint_window_seconds", 30.0)
    start = 1_000_000.0
    ws_module._authorise_mint("operator-a", now=start)
    ws_module._authorise_mint("operator-a", now=start + 1)

    with pytest.raises(PolsiaError) as excinfo:  # the named refusal IS the assertion
        ws_module._authorise_mint("operator-a", now=start + 2)
    assert getattr(excinfo.value, "code", None) == "E_RATE_LIMITED"
    assert excinfo.value.context["reason"] == "mint_rate"

    # one second after the FIRST stamp leaves the window, one slot is free again
    ws_module._authorise_mint("operator-a", now=start + 30.5)
    assert len(ws_module._mint_times["operator-a"]) == 2, "pruned to the live window only"


def test_the_bounds_are_per_operator(monkeypatch):
    """Two operator identities have independent budgets (one API key = one operator today)."""
    monkeypatch.setattr(settings, "ws_ticket_mint_limit", 1)
    monkeypatch.setattr(settings, "ws_ticket_mint_window_seconds", 60.0)
    now = 2_000_000.0
    ws_module._authorise_mint("operator-a", now=now)
    with pytest.raises(PolsiaError):  # exhausted budget for operator-a
        ws_module._authorise_mint("operator-a", now=now)
    ws_module._authorise_mint("operator-b", now=now)  # unaffected
    assert len(ws_module._mint_times["operator-b"]) == 1


# â”€â”€ bound 2: CAP ACCOUNTING â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_a_mint_is_refused_while_the_operator_is_at_the_socket_cap(client, monkeypatch):
    """An exhausted install must not keep minting tickets that can only be refused at connect."""
    monkeypatch.setattr(ws_module, "_MAX_SOCKETS_PER_OPERATOR", 2)
    operator = ws_module.operator_identity()
    ws_module._operator_sockets[operator] = 2  # the install is full

    refused = _mint(client)
    assert refused.status_code == 429, refused.text
    detail = _refusal(refused)
    assert detail["context"]["reason"] == "socket_cap"
    assert detail["context"]["open_sockets"] == 2
    assert detail["context"]["socket_cap"] == 2
    assert detail["how_to_fix"], "a refusal must say what an operator can do about it"
    assert "ticket" not in refused.json()


def test_a_cap_refusal_does_not_consume_the_mint_budget(client, monkeypatch):
    """The two bounds are independent: a cap refusal must not also burn the rate window."""
    monkeypatch.setattr(settings, "ws_ticket_mint_limit", 2)
    monkeypatch.setattr(ws_module, "_MAX_SOCKETS_PER_OPERATOR", 1)
    operator = ws_module.operator_identity()
    ws_module._operator_sockets[operator] = 1

    for _ in range(3):
        assert _mint(client).status_code == 429
    assert ws_module._mint_times.get(operator, []) == [], (
        "a refusal must not record a mint; otherwise the client is punished twice"
    )

    ws_module._operator_sockets[operator] = 0  # a page closed: capacity is back
    assert _mint(client).status_code == 200
    assert _mint(client).status_code == 200
    assert _mint(client).status_code == 429, "the rate bound still applies"


def test_a_mint_under_both_bounds_is_still_issued(client, monkeypatch):
    """Anti-regression: the fix must not refuse normal use."""
    monkeypatch.setattr(settings, "ws_ticket_mint_limit", 30)
    monkeypatch.setattr(settings, "ws_ticket_mint_window_seconds", 60.0)
    response = _mint(client)
    assert response.status_code == 200, response.text
    ticket = response.json()["ticket"]
    assert ws_module.verify_ws_ticket(ticket) is True
    assert response.json()["expires_in"] == ws_module._TICKET_TTL_SECONDS
    assert len(ws_module._mint_times[ws_module.operator_identity()]) == 1


def test_the_defaults_bound_a_runaway_child(monkeypatch):
    """The SHIPPED defaults must be a real bound: one identity per ~2 s, not per connect."""
    monkeypatch.undo()  # the shipped Settings values, not a test override
    assert settings.ws_ticket_mint_limit >= 1
    assert settings.ws_ticket_mint_window_seconds >= 1
    rate = settings.ws_ticket_mint_limit / settings.ws_ticket_mint_window_seconds
    assert rate <= 2.0, (
        f"{rate:.2f} identities/second is not a bound on a looping client "
        f"(limit {settings.ws_ticket_mint_limit} per "
        f"{settings.ws_ticket_mint_window_seconds:g}s)"
    )


def test_the_rate_budget_is_bounded_not_accumulating(monkeypatch):
    """The counter is pruned on every call, so a long-lived process cannot grow it forever."""
    monkeypatch.setattr(settings, "ws_ticket_mint_limit", 5)
    monkeypatch.setattr(settings, "ws_ticket_mint_window_seconds", 10.0)
    now = time.time()
    for index in range(200):
        # one call per 20 virtual seconds: each is its own window (the window is 10 s)
        ws_module._authorise_mint("operator-a", now=now + index * 20)
        assert len(ws_module._mint_times["operator-a"]) <= 5
    assert len(ws_module._mint_times["operator-a"]) == 1
