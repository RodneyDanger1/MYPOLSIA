"""SENTINEL (2026-10-28): the CLI child's environment is a WHITELIST — close two shape gaps.

Why this test exists (measured, not hypothetical):
`claude_cli.child_environment()` refuses an explicit override whose NAME is credential-shaped so a
caller cannot quietly hand the Claude CLI a provider key.  The marker list covered
`*API_KEY*`/`*SECRET*`/`*TOKEN*`/`*PASSWORD*` but NOT a URL-style credential: `DATABASE_URL`,
`REDIS_URL`, an `*_DSN` or a connection string all publish a password inside the VALUE while their
NAME carries no marker, so they were forwarded verbatim.  The postgres password IS a live secret in
this deployment (docker-compose passes the URL), and the CLI child is the one process the module
promises never sees it.

The last test is the over-blocking guard: an ordinary non-credential override must still pass, so
the marker list cannot silently swallow a legitimate variable.
"""
from __future__ import annotations

import pytest

from app.agents import claude_cli
from app.core.errors import E_POLICY_DENIED, PolsiaError


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DATABASE_URL", "postgresql+asyncpg://polsia:sekret@postgres:5432/polsia"),
        ("REDIS_URL", "redis://:sekret@redis:6379/0"),
        ("SENTRY_DSN", "https://abc123@o1.ingest.sentry.io/42"),
        ("BROKER_CONNECTION_STRING", "amqp://guest:sekret@rabbit:5672//"),
    ],
)
def test_a_url_style_credential_cannot_be_forwarded_into_the_cli_child(name: str, value: str) -> None:
    """A credential carried in a URL's VALUE is refused exactly like one named by its KEY."""
    with pytest.raises(PolsiaError) as caught:
        claude_cli.child_environment(extra_env={name: value})
    assert caught.value.code == E_POLICY_DENIED
    assert caught.value.context.get("variable") == name
    assert "child environment is a whitelist" in caught.value.message


def test_an_ordinary_override_is_still_passed_through() -> None:
    """The guard must not over-block: a non-credential variable reaches the child unchanged."""
    env = claude_cli.child_environment(extra_env={"POLSIA_PROBE_MARKER": "ok"})
    assert env["POLSIA_PROBE_MARKER"] == "ok"
