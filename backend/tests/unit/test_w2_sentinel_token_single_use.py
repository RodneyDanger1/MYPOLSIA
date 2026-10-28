"""W2/SENTINEL — approval tokens are SINGLE-USE: nonce (`jti`) + a consumed-approval ledger.

THE DEFECT THIS FILE PINS (wave-1 P1, `sentinel/REPORT.md` §4.3).

`issue_approval` minted `{exp}.{hmac}` and `verify_approval` recomputed the HMAC on demand, but
nothing recorded that a token had been SPENT.  Wave-1 measured the consequence against the live
deploy gate: the same token verified twice (`first=True second=True`) and the second live deploy was
ALLOWED.  Four docstrings claimed "single-use"; no code made it so.  Two mints in the same second
for the same fingerprint were also BYTE-IDENTICAL (whole-second `exp`, same HMAC input), so a naive
consumption record could not even tell a fresh approval from a replay.

THE FIX, in two parts:
  1. every mint carries a random 64-bit `jti` nonce inside the signed message
     (`{action}|{exp}|{jti}|{issued_to}`) and inside the token body (`{exp}.{sig}{jti}`), so two
     approvals are never the same string;
  2. the ONE action a token authorises SPENDS it: `consume_approval` inserts the token's digest into
     `consumed_approvals`, whose UNIQUE index (`uq_consumed_approvals_token_id`) makes the second
     spend impossible — atomically, across the API process and every worker.

Tests below cover both halves separate from the deploy seam (which has its own file,
`test_w2_sentinel_deploy_replay.py`): the nonce, legacy-token compatibility, the ledger, replay,
and the durability of the spend when the caller's own transaction is rolled back.
"""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agents import policy_firewall as pf

SECRET = b"w2-sentinel-test-approval-secret-not-a-real-one"
FP = "0123456789abcdef01234567"


def _mint(fingerprint: str = FP, *, ttl_s: int = 600) -> pf.ApprovalToken:
    return pf.issue_approval(SECRET, action_fingerprint=fingerprint, ttl_s=ttl_s)


def _legacy_token(fingerprint: str = FP, *, exp: int | None = None) -> str:
    """A `{exp}.{sig}` token in the PRE-nonce shape — the format already in an operator's inbox."""
    expiry = int(time.time()) + 600 if exp is None else exp
    sig = hmac.new(
        SECRET, f"{fingerprint}|{expiry}|operator".encode(), hashlib.sha256
    ).hexdigest()[: pf.SIG_HEX_CHARS]
    return f"{expiry}.{sig}"


# ── the nonce distinguishes two approvals that used to be identical ──────────────────────────
def test_two_mints_in_the_same_second_are_different_tokens() -> None:
    """The exact collision wave 1 recorded: same fingerprint, same second, same string.

    RED on the pre-fix code: both mints produced `{exp}.{same hmac}`; a consumption record keyed on
    the token could not distinguish "the owner approved twice" from "the same approval replayed".
    """
    first, second = _mint(), _mint()

    assert first.expires_at == second.expires_at, "these mints share one whole-second expiry"
    assert first.token != second.token, "two mints in one second must not be the same token"
    assert first.jti != second.jti
    assert len(first.jti) == pf.JTI_HEX_CHARS
    for token in (first.token, second.token):
        assert pf.verify_approval(SECRET, token, action_fingerprint=FP) is True


def test_the_wire_format_keeps_one_dot_so_the_data_wipe_parser_still_works() -> None:
    """`app/api/v1/data.py::_verify_wipe_token` partitions on the FIRST dot and refuses a second.

    The nonce lives INSIDE the signature half for exactly this reason: the data-wipe seam (another
    owner's file) keeps accepting freshly minted tokens with no cross-lane edit, and its own
    single-use marker keys on that half — which the nonce makes unique per mint.
    """
    token = _mint().token
    exp_part, dot, body = token.partition(".")

    assert dot == "."
    assert "." not in body, "a second dot would be read as a MALFORMED wipe token"
    assert int(exp_part) > 0
    assert len(body) == pf.SIG_HEX_CHARS + pf.JTI_HEX_CHARS


# ── a token already minted before the change must not die mid-flight ────────────────────────
def test_a_legacy_token_without_a_nonce_still_verifies_until_it_expires() -> None:
    legacy = _legacy_token()
    assert pf.verify_approval(SECRET, legacy, action_fingerprint=FP) is True

    _exp, _dot, body = legacy.partition(".")
    assert pf.approval_token_id(legacy) == hashlib.sha256(body.encode()).hexdigest()


def test_appending_a_nonce_to_a_legacy_token_cannot_re_authorise_it() -> None:
    """The HMAC covers the nonce, so 16 extra hex chars on the body are not a free upgrade."""
    exp_part, _, body = _legacy_token().partition(".")
    forged = f"{exp_part}.{body}{'0' * pf.JTI_HEX_CHARS}"

    assert pf.verify_approval(SECRET, forged, action_fingerprint=FP) is False


def test_tampering_with_the_nonce_invalidates_the_token() -> None:
    exp_part, _, body = _mint().token.partition(".")
    flipped = ("1" if body[-1] != "1" else "0")
    tampered = f"{exp_part}.{body[:-1]}{flipped}"

    assert pf.verify_approval(SECRET, tampered, action_fingerprint=FP) is False


@pytest.mark.parametrize(
    "token",
    ["", "not-a-token", "1.a.b", "x.deadbeef", "123", None, 12345],
)
def test_a_string_that_is_not_a_token_never_verifies(token) -> None:
    assert pf.verify_approval(SECRET, token, action_fingerprint=FP) is False
    assert pf.approval_token_id(token) == ""


def test_scope_is_still_exact_fingerprint_and_ttl() -> None:
    token = _mint()
    assert pf.verify_approval(SECRET, token.token, action_fingerprint="other") is False
    assert pf.verify_approval(b"different-secret", token.token, action_fingerprint=FP) is False
    expired = _mint(ttl_s=-5)
    assert pf.verify_approval(SECRET, expired.token, action_fingerprint=FP) is False


# ── the ledger: one spend per token, and the DATABASE is what refuses the second ────────────
@pytest.mark.asyncio
async def test_the_first_spend_succeeds_and_the_replay_is_already_used(async_db_session) -> None:
    token = _mint().token

    first = await pf.consume_approval(
        async_db_session, SECRET, token, action_fingerprint=FP, consumed_by="unit:first"
    )
    second = await pf.consume_approval(
        async_db_session, SECRET, token, action_fingerprint=FP, consumed_by="unit:replay"
    )

    assert (first.ok, first.reason) == (True, "")
    assert first.jti and len(first.jti) == pf.JTI_HEX_CHARS
    assert (second.ok, second.reason) == (False, "already_used")
    assert second.token_id == first.token_id


@pytest.mark.asyncio
async def test_the_unique_index_is_what_refuses_the_second_row(async_db_session) -> None:
    """The guarantee is the DATABASE's, not a Python `if`: a second INSERT of the same id fails."""
    from app.models.approval import ConsumedApproval

    token_id = pf.approval_token_id(_mint().token)
    async_db_session.add(
        ConsumedApproval(token_id=token_id, jti="x", action_fingerprint=FP, consumed_by="a")
    )
    await async_db_session.commit()

    async_db_session.add(
        ConsumedApproval(token_id=token_id, jti="x", action_fingerprint=FP, consumed_by="b")
    )
    with pytest.raises(IntegrityError):
        await async_db_session.commit()


@pytest.mark.asyncio
async def test_the_ledger_stores_a_digest_and_never_the_token(async_db_session) -> None:
    """A token is a bearer credential: the ledger must not become a store of live approvals."""
    from app.models.approval import ConsumedApproval

    token = _mint().token
    body = token.partition(".")[2]
    await pf.consume_approval(async_db_session, SECRET, token, action_fingerprint=FP)

    rows = (await async_db_session.execute(select(ConsumedApproval))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.token_id == hashlib.sha256(body.encode()).hexdigest()
    assert body not in row.token_id, "the raw signature half must never be persisted"
    assert row.jti == body[pf.SIG_HEX_CHARS:]
    assert row.action_fingerprint == FP
    assert row.consumed_by == "unit:first" or row.consumed_by == ""


@pytest.mark.asyncio
async def test_an_invalid_expired_or_mis_scoped_token_spends_nothing(async_db_session) -> None:
    """A rejected token must not be able to burn a real approval's record."""
    from app.models.approval import ConsumedApproval

    cases = [
        (_mint(ttl_s=-60).token, FP, "expired"),
        (_mint().token, "someone-elses-fingerprint", "invalid"),
        (_legacy_token(exp=int(time.time()) - 10), FP, "expired"),
    ]
    for token, fingerprint, expected in cases:
        got = await pf.consume_approval(
            async_db_session, SECRET, token, action_fingerprint=fingerprint
        )
        assert got.ok is False and got.reason == expected

    rows = (await async_db_session.execute(select(ConsumedApproval))).scalars().all()
    assert rows == [], "a refused token must leave no consumption row behind"


@pytest.mark.asyncio
async def test_the_second_spend_is_refused_even_after_the_callers_transaction_rolls_back(
    async_db_session,
) -> None:
    """The spend is its OWN committed transaction: a failed request cannot re-arm the approval."""
    token = _mint().token
    spent = await pf.consume_approval(async_db_session, SECRET, token, action_fingerprint=FP)
    assert spent.ok is True

    await async_db_session.rollback()  # what a request that raised mid-flight does

    replay = await pf.consume_approval(async_db_session, SECRET, token, action_fingerprint=FP)
    assert replay.reason == "already_used"


@pytest.mark.asyncio
async def test_consume_or_refuse_names_the_replay_with_its_own_policy_code(async_db_session) -> None:
    token = _mint().token
    await pf.consume_approval(async_db_session, SECRET, token, action_fingerprint=FP)

    with pytest.raises(Exception) as caught:
        await pf.consume_approval_or_refuse(
            async_db_session,
            SECRET,
            token,
            action_fingerprint=FP,
            action_label="unit action",
            consumed_by="unit:replay",
        )
    err = caught.value
    assert getattr(err, "code", "") == "E_POLICY_DENIED"
    context = getattr(err, "context", {})
    assert context["reason"] == "already_used"
    assert context["approval_replay"] is True
    assert context["policy_code"] == "POLSIA-E121", (
        "a replay is its own control, not 'approval required' (E115) — an operator greps the "
        "difference between 'mint a token' and 'that token was already spent'"
    )
    assert context["fingerprint"] == FP
    assert "already used" in err.message


@pytest.mark.asyncio
async def test_consume_or_refuse_distinguishes_expired_from_invalid(async_db_session) -> None:
    with pytest.raises(Exception) as expired:
        await pf.consume_approval_or_refuse(
            async_db_session, SECRET, _mint(ttl_s=-60).token,
            action_fingerprint=FP, action_label="unit action",
        )
    assert expired.value.context["reason"] == "expired"

    with pytest.raises(Exception) as invalid:
        await pf.consume_approval_or_refuse(
            async_db_session, SECRET, _mint().token,
            action_fingerprint="different-action", action_label="unit action",
        )
    assert invalid.value.context["reason"] == "invalid"


@pytest.mark.asyncio
async def test_a_missing_ledger_table_fails_closed(async_db_session) -> None:
    """An approval whose spend CANNOT be recorded must refuse the action, never allow it.

    The live PostgreSQL database is at alembic `0017` until the wave-2 migration runs, so this is
    the exact state the deployed stack would be in if `0018_consumed_approvals` were skipped: the
    honest answer is `E_DB` ("cannot prove the approval is single-use"), not a successful spend.
    """
    from sqlalchemy import text

    await async_db_session.execute(text("DROP TABLE consumed_approvals"))
    await async_db_session.commit()

    with pytest.raises(Exception) as caught:
        await pf.consume_approval(
            async_db_session, SECRET, _mint().token, action_fingerprint=FP
        )
    assert getattr(caught.value, "code", "") == "E_DB"
    assert "single-use" in getattr(caught.value, "message", "")
    assert "alembic upgrade head" in getattr(caught.value, "how_to_fix", "")


@pytest.mark.asyncio
async def test_verify_approval_stays_non_consuming(async_db_session) -> None:
    """The DECISION question must not spend: the worker re-validates a parked token repeatedly."""
    token = _mint().token
    for _ in range(3):
        assert pf.verify_approval(SECRET, token, action_fingerprint=FP) is True

    spent = await pf.consume_approval(async_db_session, SECRET, token, action_fingerprint=FP)
    assert spent.ok is True
