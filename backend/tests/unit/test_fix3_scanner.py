"""L3-03 — `card_number` is an ADVISORY tier: issuer prefix + Luhn; only real credential
SHAPES stay fatal.

What this file pins (finding lane-3 L3-03 + research-C):

* a legitimate 13+ digit business number (order id, invoice number, epoch timestamp) no longer
  kills the run — the old `\\b(?:\\d[ -]?){13,19}\\b` rule raised `SecretLeak` before the model
  call for ANY digit run in any context (measured: a customer_support run died over an order id);
* the card heuristic is graded the Presidio way — weak 12-19-digit candidate window, issuer
  prefix groups, Luhn check digit — so Stripe's documented `incorrect_number` recipe
  (`4242424242424241`, a Luhn-FAILING card-shaped number) is normal input and is NOT flagged,
  while a Luhn-valid test card IS flagged;
* a flagged card is a WARNING on the assembled context (surfaced into the run record), never a
  refusal;
* real credential shapes (API keys, private-key blocks) are still FATAL — `assemble` raises.
"""
from __future__ import annotations

import pytest

from app.agents.context_assembler import (
    Layer,
    SecretLeak,
    assemble,
    card_number_hit,
    screen_prompt,
)

# The exact strings lane-3's probe used: plausible business text / page dumps.
LEGITIMATE_BUSINESS_TEXT = (
    "ORDER 1234567890123",
    "tracking number 9400111899223197412345",
    "invoice 2026101300001",
    "my order #1234567890123",
    "window.ytplayer = 1755000000000000",
    "Call me on 555-123-4567",
)


def _assembled_text(text: str):
    return assemble({"task_brief": Layer("task_brief", 800, entries=[text])}, label="test")


def test_order_numbers_and_timestamps_are_not_flagged_at_all():
    for sample in LEGITIMATE_BUSINESS_TEXT:
        assert screen_prompt(sample) == [], f"{sample!r} is normal business text"
        assert card_number_hit(sample) is False


def test_a_legitimate_order_id_no_longer_kills_a_run():
    ctx = _assembled_text("Hi, my order 1234567890123 never arrived. Can you check?")
    assert "ORDER" not in ctx.warnings
    assert "1234567890123" in ctx.full_prompt
    assert ctx.warnings == []


def test_a_luhn_valid_card_is_flagged_as_an_advisory_not_a_refusal():
    assert card_number_hit("4242424242424242") is True
    assert screen_prompt("card on file 4242424242424242") == ["card_number"]

    ctx = _assembled_text("payment failed for 4242424242424242 please retry")

    # no raise, and the advisory is auditable on the assembled context
    assert any("card_number" in w for w in ctx.warnings)
    assert any("not fatal" in w for w in ctx.warnings)


def test_the_space_and_dash_separated_form_is_detected_too():
    assert card_number_hit("4242 4242 4242 4242") is True
    assert card_number_hit("4242-4242-4242-4242") is True


def test_a_luhn_failing_card_shape_is_not_flagged():
    """Stripe's documented `incorrect_number` recipe is indistinguishable from normal input."""
    assert card_number_hit("4242424242424241") is False
    assert screen_prompt("the number 4242424242424241 did not work") == []
    assert _assembled_text("the number 4242424242424241 did not work").warnings == []


def test_the_candidate_window_is_12_to_19_digits():
    assert card_number_hit("12345678901") is False       # 11 digits: below the window
    assert card_number_hit("424242424242") is True       # 12 digits, issuer prefix + Luhn valid
    assert card_number_hit("378282246310005") is True    # 15 digits (Amex test PAN)
    assert card_number_hit("6011111111111117") is True   # 16 digits (Discover test PAN)


def test_the_22_digit_tracking_number_is_not_flagged():
    """The window is NOT widened: USPS's modified Luhn would make those false-positive fuel."""
    assert card_number_hit("9400111899223197412345") is False


# ------------------------------------------------------------------ real credentials stay fatal


@pytest.mark.parametrize("text", [
    "ANTHROPIC_KEY=sk-ant-abcdefghijklmnopqrstuvwx012345",
    "aws AKIAIOSFODNN7EXAMPLE",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...",
    'credentials: {"refreshToken": "1//abc"}',
])
def test_credential_shapes_are_still_fatal(text: str):
    assert screen_prompt(text), f"{text!r} must be flagged"
    with pytest.raises(SecretLeak):
        _assembled_text(text)


def test_the_fatal_list_no_longer_contains_a_heuristic_for_digits():
    from app.agents.context_assembler import _ADVISORY_PATTERNS, _FORBIDDEN_PATTERNS

    names = [name for name, _ in _FORBIDDEN_PATTERNS]
    assert "card_number" not in names, "the advisory pattern must not be a fatal pattern"
    assert _ADVISORY_PATTERNS == ("card_number",)
