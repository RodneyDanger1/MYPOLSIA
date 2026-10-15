"""V12 red-team — the one-click opt-out token (`/api/v1/outreach/unsubscribe`) under attack.

House style: `tests/redteam/test_spec_invariants.py` (run explicitly, not part of the shipped
suite).  Every leg here is an ATTACK on a defence V11/W1 shipped; a leg that cannot hold is
marked `xfail` with the defect NAMED (and raised as a REQUEST in the lane receipt) — never
"fixed" here.  No live provider, no network: the route and the token primitives are the real
code on the real schema.

Attack list (each leg names the attack in its docstring):
  1. forged HMAC (right shape, key the attacker does not have)
  2. truncated token (signature / payload / separator)
  3. mutated address (valid signature transplanted onto a victim payload)
  4. token minted for a DIFFERENT purpose (raw-secret HMAC, an approvals token, the reverse)
  5. replay after opt-out (idempotent? state check?)
  6. token bound to the WRONG prospect
  7. no signing secret configured (minting + verifying must refuse BY NAME, never guess)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.policy_firewall import issue_approval, verify_approval
from app.config import settings
from app.core.errors import E_CONFIG, E_VALIDATION, PolsiaError
from app.models.outreach import Prospect
from app.services import email_compliance as ec

SECRET = "redteam-v12-unsubscribe-secret"
ATTACKER = "attacker@example.com"
VICTIM = "victim@example.com"
UNSUB_ROUTE = ec.UNSUBSCRIBE_PATH  # "/api/v1/outreach/unsubscribe"


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch: Any):
    monkeypatch.setattr(settings, "approval_secret", SECRET)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


async def _seed(db: AsyncSession, email: str, status: str = "new") -> Prospect:
    row = Prospect(email=email, status=status)
    db.add(row)
    await db.commit()
    return row


async def _status(db: AsyncSession, email: str) -> str:
    row = (
        await db.execute(select(Prospect).where(func.lower(Prospect.email) == email.lower()))
    ).scalars().one()
    return str(row.status)


async def _count(db: AsyncSession, email: str) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).select_from(Prospect).where(
                    func.lower(Prospect.email) == email.lower()
                )
            )
        ).scalar_one()
    )


# ── 1. forged HMAC ────────────────────────────────────────────────────────────────────────────


async def test_forged_hmac_is_refused_and_moves_nothing(api_client, async_db_session):
    """FORGE: the attacker mints `<b64(address)>.<b64(HMAC over address with a key they made up)>`.

    The token has the exact shipped wire shape; only the secret is wrong.  The route must answer
    the named E_VALIDATION refusal (422, not 500, not 200) and the target must stay untouched.
    """
    db = async_db_session
    await _seed(db, VICTIM)
    forged_key = hmac.new(b"attacker-guess", VICTIM.encode(), hashlib.sha256).digest()
    forged = f"{_b64(VICTIM.encode())}.{_b64(forged_key)}"

    with pytest.raises(PolsiaError) as refusal:
        ec.verify_unsubscribe_token(forged)
    assert refusal.value.code == E_VALIDATION

    resp = await api_client.get(f"{UNSUB_ROUTE}?token={forged}")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == E_VALIDATION
    assert await _status(db, VICTIM) == "new"


# ── 2. truncated token ────────────────────────────────────────────────────────────────────────


async def test_truncated_tokens_are_one_named_refusal(api_client, async_db_session):
    """TRUNCATE: a mail client (or a prober) cuts the link short three ways.

    Last-bytes-cut, half-cut, and separator-cut are ONE refusal shape (E_VALIDATION — a prober
    learns nothing from the difference), and no truncation of a VALID token may pass.
    """
    db = async_db_session
    await _seed(db, VICTIM)
    good = ec.unsubscribe_token(VICTIM)
    payload_part, sig_part = good.split(".")
    truncated = [
        good[:-4],                      # signature cut short
        good[: len(good) // 2],         # entire token halved
        payload_part,                   # separator (and signature) gone
        f"{payload_part}.",             # empty signature half
        f".{sig_part}",                 # empty payload half
    ]
    for bad in truncated:
        with pytest.raises(PolsiaError) as refusal:
            ec.verify_unsubscribe_token(bad)
        assert refusal.value.code == E_VALIDATION, bad
        resp = await api_client.get(f"{UNSUB_ROUTE}?token={bad}")
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["code"] == E_VALIDATION, bad

    assert await _status(db, VICTIM) == "new"
    assert ec.verify_unsubscribe_token(good) == VICTIM  # the untruncated original still works


# ── 3. mutated address ────────────────────────────────────────────────────────────────────────


async def test_mutated_address_with_a_transplanted_signature_is_refused(api_client, async_db_session):
    """MUTATE: keep the real signature, swap the payload for the victim's address.

    This is the whole point of signing the ADDRESS: the signature is over the payload bytes, so
    a harvested valid signature cannot be re-pointed at another recipient.
    """
    db = async_db_session
    await _seed(db, VICTIM)
    await _seed(db, ATTACKER)
    attacker_token = ec.unsubscribe_token(ATTACKER)
    _, sig_part = attacker_token.split(".")

    transplanted = f"{_b64(VICTIM.encode())}.{sig_part}"
    with pytest.raises(PolsiaError) as refusal:
        ec.verify_unsubscribe_token(transplanted)
    assert refusal.value.code == E_VALIDATION

    resp = await api_client.get(f"{UNSUB_ROUTE}?token={transplanted}")
    assert resp.status_code == 422
    assert await _status(db, VICTIM) == "new"
    assert await _status(db, ATTACKER) == "new", "the transplanted token must move NOTHING"


# ── 4. token for a different purpose ─────────────────────────────────────────────────────────


async def test_a_raw_secret_hmac_is_not_an_unsubscribe_token():
    """PURPOSE: another feature signing the same secret must not mint a valid opt-out token.

    `_unsubscribe_key()` derives a purpose-bound key (`sha256(PURPOSE + b":" + secret)`); a
    naive `HMAC(secret, address)` — what a second meaning of the one secret would compute — must
    fail verification, or one secret would silently carry two powers.
    """
    naive = _b64(hmac.new(SECRET.encode(), VICTIM.encode(), hashlib.sha256).digest())
    naive_token = f"{_b64(VICTIM.encode())}.{naive}"
    with pytest.raises(PolsiaError) as refusal:
        ec.verify_unsubscribe_token(naive_token)
    assert refusal.value.code == E_VALIDATION

    # ... and the purpose-bound key really is what verifies (built here independently).
    key = ec._unsubscribe_key(SECRET)
    assert key != SECRET.encode()
    hand_built = _b64(hmac.new(key, VICTIM.encode(), hashlib.sha256).digest())
    assert ec.verify_unsubscribe_token(f"{_b64(VICTIM.encode())}.{hand_built}") == VICTIM


async def test_an_approvals_token_does_not_verify_as_an_unsubscribe_token():
    """PURPOSE (the other direction): a REAL approvals token is not an opt-out token."""
    approvals_token = issue_approval(SECRET.encode(), action_fingerprint="some-other-action", ttl_s=600)
    with pytest.raises(PolsiaError) as refusal:
        ec.verify_unsubscribe_token(approvals_token.token)
    assert refusal.value.code == E_VALIDATION


async def test_an_unsubscribe_token_does_not_verify_as_an_approval(monkeypatch):
    """PURPOSE (reverse): an opt-out token must not approve a firewalled action either."""
    approvals = issue_approval(SECRET.encode(), action_fingerprint="ghost-action", ttl_s=600)
    unsub = ec.unsubscribe_token(VICTIM)
    assert verify_approval(SECRET.encode(), unsub, action_fingerprint="ghost-action") is False
    assert verify_approval(SECRET.encode(), approvals.token, action_fingerprint="ghost-action") is True


# ── 5. replay after opt-out ───────────────────────────────────────────────────────────────────


async def test_replay_after_opt_out_is_idempotent_and_duplicates_nothing(api_client, async_db_session):
    """REPLAY: click the same valid link twice (and after the state already flipped).

    The second click must answer the same 200 page idempotently, the stored state must stay
    `unsubscribed`, and no second prospect row may be created for the same address.
    """
    db = async_db_session
    await _seed(db, ATTACKER)
    token = ec.unsubscribe_token(ATTACKER)

    first = await api_client.get(f"{UNSUB_ROUTE}?token={token}")
    second = await api_client.get(f"{UNSUB_ROUTE}?token={token}")
    assert first.status_code == 200 and second.status_code == 200
    assert first.text == second.text
    assert await _status(db, ATTACKER) == "unsubscribed"
    assert await _count(db, ATTACKER) == 1, "a replayed click must not create a second row"


# ── 6. token bound to the wrong prospect ─────────────────────────────────────────────────────


async def test_a_token_unsubscribes_only_its_own_address(api_client, async_db_session):
    """WRONG-PROSPECT: clicking attacker's link must not touch the victim's consent state."""
    db = async_db_session
    await _seed(db, VICTIM)
    await _seed(db, ATTACKER)

    resp = await api_client.get(f"{UNSUB_ROUTE}?token={ec.unsubscribe_token(ATTACKER)}")
    assert resp.status_code == 200
    assert await _status(db, ATTACKER) == "unsubscribed"
    assert await _status(db, VICTIM) == "new"


# ── 7. no signing secret configured ──────────────────────────────────────────────────────────


async def test_no_secret_refuses_minting_and_verification_by_name(api_client, async_db_session, monkeypatch):
    """NO-SECRET: with APPROVAL_SECRET unset the token cannot be minted AND cannot be verified.

    A guessable/blank-key opt-out token on a public route would let anyone unsubscribe anyone,
    so both halves must refuse by name: minting raises E_CONFIG, the route answers E_CONFIG
    (503) and changes nothing.  The footer degrades to the reply-based opt-out line (asserted),
    so sends are not stalled — but no clickable token exists.
    """
    db = async_db_session
    await _seed(db, VICTIM)
    good = ec.unsubscribe_token(VICTIM)          # minted while the secret existed
    body_before = await _status(db, VICTIM)

    monkeypatch.setattr(settings, "approval_secret", "")
    with pytest.raises(PolsiaError) as mint_refusal:
        ec.unsubscribe_token(ATTACKER)
    assert mint_refusal.value.code == E_CONFIG
    with pytest.raises(PolsiaError) as verify_refusal:
        ec.verify_unsubscribe_token(good)
    assert verify_refusal.value.code == E_CONFIG

    resp = await api_client.get(f"{UNSUB_ROUTE}?token={good}")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == E_CONFIG
    assert await _status(db, VICTIM) == body_before

    # The footer keeps the always-accepted reply route instead of a token it cannot sign.
    footer = ec.footer_for_company(
        {"sender_legal_name": "Acme", "sender_postal_address": "1 Main St",
         "website_url": "https://acme.example"},
        opt_out_url=ec._opt_out_url_or_none("https://acme.example", VICTIM),
    )
    assert "?token=" not in footer
    assert ec.OPT_OUT_LINE in footer
