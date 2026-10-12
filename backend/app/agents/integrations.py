"""The agents' third-party action adapters — one honest call site per external capability.

Each function here performs ONE real-world action and returns either
`{"ok": True, "result": ...}` or a NAMED refusal (`E_INTEGRATION`) explaining exactly why the
action could not be performed (library missing, credentials unset, provider error).

Two rules from the build brief are enforced by construction:

  * integrations are imported LAZILY inside the function (CLAUDE.md:116) — importing the agent
    package must never drag in tweepy/stripe/sendgrid/github;
  * a capability that is not configured returns a refusal, never a success-shaped empty result.
    On a host without the libraries (this one) every adapter refuses by name; with the libraries
    and the `*_API_KEY` settings present it makes the documented provider call.

Nothing in this module is reachable while `SANDBOX_MODE=true`: the policy firewall refuses the
action first and the agent records a labelled draft instead.
"""
from __future__ import annotations

from typing import Any

from app.agents.side_effects import integration_call, named_refusal
from app.config import settings
from app.core.errors import E_INTEGRATION, E_VALIDATION

_TWEEPY_HINT = "pip install tweepy==4.14.0 and set TWITTER_API_KEY/TWITTER_API_SECRET/TWITTER_ACCESS_TOKEN/TWITTER_ACCESS_SECRET"
_SENDGRID_HINT = "pip install sendgrid and set SENDGRID_API_KEY/SENDGRID_FROM_EMAIL"
_PYGITHUB_HINT = "pip install PyGithub and set GITHUB_TOKEN"
_STRIPE_HINT = "pip install stripe and set STRIPE_SECRET_KEY"
_ADS_HINT = ("pip install google-ads==24.1.0 facebook-business==21.0.0 and set the "
             "GOOGLE_ADS_* / META_ADS_* credentials")


def _missing_config(*pairs: tuple[str, str]) -> list[str]:
    return [name for name, value in pairs if not str(value or "").strip()]


def post_tweet(text: str, *, platform: str = "twitter") -> dict:
    """Post one status update. Sandbox never reaches here (the firewall refuses first)."""
    if platform != "twitter":
        return named_refusal(
            E_VALIDATION, f"post_tweet adapter does not handle platform {platform!r}",
            how_to_fix="add the platform adapter or route it through the channel it supports",
            platform=platform,
        )
    missing = _missing_config(
        ("TWITTER_API_KEY", settings.twitter_api_key),
        ("TWITTER_API_SECRET", settings.twitter_api_secret),
        ("TWITTER_ACCESS_TOKEN", settings.twitter_access_token),
        ("TWITTER_ACCESS_SECRET", settings.twitter_access_secret),
    )
    if missing:
        return named_refusal(
            E_INTEGRATION, f"Twitter credentials missing: {', '.join(missing)}",
            how_to_fix=_TWEEPY_HINT, missing=missing,
        )
    return integration_call(
        "tweepy", "Client", agent_type="social_media", install_hint=_TWEEPY_HINT,
        call=_tweepy_create_tweet, text=text,
    )


def _tweepy_create_tweet(text: str) -> Any:  # pragma: no cover - requires the provider
    import tweepy

    client = tweepy.Client(
        consumer_key=settings.twitter_api_key,
        consumer_secret=settings.twitter_api_secret,
        access_token=settings.twitter_access_token,
        access_token_secret=settings.twitter_access_secret,
    )
    response = client.create_tweet(text=text)
    return {"id": str(response.data.get("id"))}


def send_email(*, to: str, subject: str, body: str) -> dict:
    """Send one email through SendGrid."""
    missing = _missing_config(
        ("SENDGRID_API_KEY", settings.sendgrid_api_key),
        ("SENDGRID_FROM_EMAIL", settings.sendgrid_from_email),
    )
    if missing:
        return named_refusal(
            E_INTEGRATION, f"SendGrid configuration missing: {', '.join(missing)}",
            how_to_fix=_SENDGRID_HINT, missing=missing,
        )
    return integration_call(
        "sendgrid", "SendGridAPIClient", agent_type="email_outreach", install_hint=_SENDGRID_HINT,
        call=_sendgrid_send, to=to, subject=subject, body=body,
    )


def _sendgrid_send(*, to: str, subject: str, body: str) -> Any:  # pragma: no cover - provider
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    client = SendGridAPIClient(settings.sendgrid_api_key)
    message = Mail(from_email=settings.sendgrid_from_email, to_emails=to,
                   subject=subject, plain_text_content=body)
    response = client.send(message)
    return {"status_code": int(getattr(response, "status_code", 0))}


def open_pull_request(*, repo: str, branch: str, title: str, body: str,
                      base: str = "main") -> dict:
    """Open a pull request. Irreversible: the firewall requires an approval token for this."""
    missing = _missing_config(("GITHUB_TOKEN", settings.github_token))
    if missing:
        return named_refusal(
            E_INTEGRATION, f"GitHub credentials missing: {', '.join(missing)}",
            how_to_fix=_PYGITHUB_HINT, missing=missing,
        )
    return integration_call(
        "github", "Github", agent_type="code_generation", install_hint=_PYGITHUB_HINT,
        call=_pygithub_open_pr, repo=repo, branch=branch, title=title, body=body, base=base,
    )


def _pygithub_open_pr(*, repo: str, branch: str, title: str, body: str, base: str) -> Any:  # pragma: no cover
    from github import Github

    client = Github(settings.github_token)
    repository = client.get_repo(repo)
    pr = repository.create_pull(title=title, body=body, head=branch, base=base)
    return {"number": int(pr.number), "url": str(pr.html_url)}


def refund_payment(*, payment_id: str, amount_cents: int | None = None) -> dict:
    """Issue a Stripe refund. Irreversible: approval-gated by the firewall."""
    missing = _missing_config(("STRIPE_SECRET_KEY", settings.stripe_secret_key))
    if missing:
        return named_refusal(
            E_INTEGRATION, f"Stripe credentials missing: {', '.join(missing)}",
            how_to_fix=_STRIPE_HINT, missing=missing,
        )
    return integration_call(
        "stripe", "Refund", agent_type="finance", install_hint=_STRIPE_HINT,
        call=_stripe_refund, payment_id=payment_id, amount_cents=amount_cents,
    )


def _stripe_refund(*, payment_id: str, amount_cents: int | None) -> Any:  # pragma: no cover
    import stripe

    stripe.api_key = settings.stripe_secret_key
    params: dict[str, Any] = {"payment_intent": payment_id}
    if amount_cents is not None:
        params["amount"] = int(amount_cents)
    refund = stripe.Refund.create(**params)
    return {"id": str(refund.get("id")), "status": str(refund.get("status"))}


def update_ad_budget(*, platform: str, campaign_ref: str, budget_change_usd: float) -> dict:
    """Mutate a live ad campaign's budget.

    The advertising APIs need an OAuth client + a customer/account id that this deployment does
    not provision, so this adapter refuses by NAME instead of pretending: the recommendation is
    still returned and recorded, and the operator sees exactly what is missing.
    """
    if platform == "google_ads":
        missing = _missing_config(
            ("GOOGLE_ADS_DEVELOPER_TOKEN", settings.google_ads_developer_token),
            ("GOOGLE_ADS_CLIENT_ID", settings.google_ads_client_id),
            ("GOOGLE_ADS_REFRESH_TOKEN", settings.google_ads_refresh_token),
            ("GOOGLE_ADS_CUSTOMER_ID", settings.google_ads_customer_id),
        )
    elif platform == "meta_ads":
        missing = _missing_config(
            ("META_ADS_ACCESS_TOKEN", settings.meta_ads_access_token),
            ("META_ADS_ACCOUNT_ID", settings.meta_ads_account_id),
        )
    else:
        return named_refusal(E_VALIDATION, f"unknown ad platform {platform!r}",
                             how_to_fix="use google_ads or meta_ads", platform=platform)
    if missing:
        return named_refusal(
            E_INTEGRATION,
            f"{platform} mutation is not configured: {', '.join(missing)}",
            how_to_fix=_ADS_HINT, platform=platform, campaign_ref=campaign_ref, missing=missing,
        )
    return named_refusal(
        E_INTEGRATION,
        f"{platform} budget mutation needs an account-scoped API client that this deployment "
        f"does not ship; the recommendation was recorded instead",
        how_to_fix=_ADS_HINT,
        platform=platform, campaign_ref=campaign_ref, budget_change_usd=budget_change_usd,
    )


__all__ = [
    "open_pull_request",
    "post_tweet",
    "refund_payment",
    "send_email",
    "update_ad_budget",
]
