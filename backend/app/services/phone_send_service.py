"""The REAL phone send path — policy gate, validation, driver subprocess, typed result.

THE CONTRACT (frozen in wave-v17/V17-PLAN.md, consumed by `api/v1/phone.py` and the frontend)
    POST /api/v1/phone/send  {"to": "<thread_id:int | E.164 digits>", "body": "1..640 chars"}
    success -> {sent_at, to_masked, driver: "cdp-google-messages"}
    refusals BY NAME: E_SANDBOX_BLOCKED / E_PHONE_SEND_UNPAIRED / E_PHONE_UNAVAILABLE / E_VALIDATION

WHY THIS MODULE EXISTS
    A send is the one phone operation that cannot be undone, so every layer of this path is ordered
    to refuse EARLY:
        1. policy  — `settings.sandbox_mode` (the shipped default) refuses before anything else runs:
                     no validation, no path lookup, no subprocess. A sandboxed deployment must not
                     even probe the driver, because probing is a side effect on a machine it should
                     not touch.
        2. input   — `to` and `body` are validated into their canonical shapes (digits-only E.164 or
                     a positive thread id; 1..640 characters with no control characters).
        3. driver  — the script must exist; otherwise the honest answer is E_PHONE_UNAVAILABLE, not
                     a 500 and not a silent no-op.
        4. process — `asyncio.create_subprocess_exec` with an ARGV LIST (never a shell string, so no
                     interpolation of caller input into a command line), the body in a private temp
                     FILE (argv is world-readable in a process list), an enforced timeout, and the
                     child killed on timeout/cancel so a hung driver cannot outlive the request.
        5. protocol— the driver's stdout is one JSON envelope; anything else (garbage, a foreign
                     driver name, ok-with-nonzero-exit, a missing `sent_at`) is E_PHONE_UNAVAILABLE.
                     Success is only returned when the driver reports an OBSERVED send; nothing here
                     invents a timestamp or a recipient.

THE ONE CODE THAT DOES NOT LIVE IN `core/errors.py`
    `E_PHONE_SEND_UNPAIRED` is defined here as a string constant with a local 409 mapping because
    `core/errors.py` is outside this lane's write territory (HANDOFF note in the receipt asks its
    owner to absorb it). The refusal still travels in the shipped envelope: `http_refusal()` builds
    the same `{code, message, how_to_fix, context}` detail `deps.as_http_exception` builds, and
    `main.py`'s coded-HTTP handler adds `X-Polsia-Error-Code` and the `{ok:false,error}` wrapper.

NO MOCK BRANCH
    There is no demo/dry-run mode in this module. Tests replace `_run_driver` (the process boundary)
    with a fake process result; everything above and below it is production code.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.config import settings
from app.core.errors import (
    E_AUTH,
    E_CONFIG,
    E_PHONE_UNAVAILABLE,
    E_SANDBOX_BLOCKED,
    E_VALIDATION,
    ERROR_HINTS,
    PolsiaError,
    http_status_for,
)

logger = logging.getLogger(__name__)

#: HANDOFF (core/errors.py owner): move this constant + its hint + its 409 into the canonical tables.
E_PHONE_SEND_UNPAIRED = "E_PHONE_SEND_UNPAIRED"

DRIVER_NAME = "cdp-google-messages"
BODY_MIN = 1
BODY_MAX = 640
TO_MAX_DIGITS = 15
E164_MIN_DIGITS = 8
THREAD_ID_MAX = 2**31 - 1
SEND_TIMEOUT_S = 45.0
DEFAULT_PORT = 9223
TEMP_PREFIX = "polsia-phone-send-"
AUDIT_ACTION = "phone_send"
AUDIT_AGENT = "system"

#: The real driver script. `POLSIA_PHONE_SEND_DRIVER` exists for an operator who keeps the driver
#: outside the repo; nothing in the app sets it, and a missing file is a named 503.
DRIVER_PATH = Path(__file__).resolve().parents[3] / "scripts" / "phone_send_driver.py"

#: Statuses this route's codes answer with. Codes the canonical table already maps keep their
#: shipped status (`http_status_for`); only E_PHONE_SEND_UNPAIRED needs the local row.
SEND_STATUS_BY_CODE: dict[str, int] = {E_PHONE_SEND_UNPAIRED: 409}

#: Codes the driver may refuse with. Anything else it prints is re-mapped to E_PHONE_UNAVAILABLE —
#: an unknown code must never become a new wire contract by accident.
DRIVER_CODES = frozenset({E_PHONE_SEND_UNPAIRED, E_PHONE_UNAVAILABLE, E_VALIDATION})

# ── host companion bridge transport (wave V19) ──────────────────────────────────────────────
#
# The Docker stack cannot reach the host's Chrome DevTools port, so an operator can run
# `scripts/phone_send_bridge.py` on the host and point this service at it.  The bridge is a
# TRANSPORT, not a policy bypass: the sandbox gate and validation above still run first, and the
# bridge applies its own token gate on the host side.
#
# Direct mode (both variables unset) is the shipped default and stays byte-identical: no URL means
# the `_run_driver` path below is the only code that can run.
BRIDGE_URL_ENV = "PHONE_SEND_BRIDGE_URL"
PHONE_SEND_BRIDGE_TOKEN_ENV = "PHONE_SEND_BRIDGE_TOKEN"
BRIDGE_TOKEN_MIN_CHARS = 24
#: Client patience for one bridged send.  The bridge itself refuses at 90 s at the latest, so the
#: client must outwait it rather than abandon a request the host may still finish.
BRIDGE_TIMEOUT_S = 90.0
BRIDGE_MAX_REPLY_BYTES = 64 * 1024
#: Refusal codes the bridge may answer with.  Anything else is a protocol fault, not a new code.
BRIDGE_CODES = frozenset({E_AUTH, E_CONFIG, *DRIVER_CODES})
_BRIDGE_HINTS = {
    E_AUTH: (
        "set PHONE_SEND_BRIDGE_TOKEN in the backend environment to the exact token the host "
        "bridge was started with"
    ),
    E_CONFIG: (
        f"set both {BRIDGE_URL_ENV} and {PHONE_SEND_BRIDGE_TOKEN_ENV} (>= "
        f"{BRIDGE_TOKEN_MIN_CHARS} chars), or unset {BRIDGE_URL_ENV} to use the direct driver"
    ),
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A bridge redirect is never part of the protocol; refuse it instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


#: A dedicated opener with proxies DISABLED: the bridge is a direct host endpoint, and an
#: HTTP_PROXY inherited by the container must not become a man-in-the-middle for a token-bearing
#: request.  Redirects are refused by design.
_BRIDGE_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect()
)


def bridge_url() -> str:
    """`PHONE_SEND_BRIDGE_URL` (empty = direct-subprocess mode)."""
    return os.environ.get(BRIDGE_URL_ENV, "").strip()


def _bridge_endpoint(url: str) -> str:
    return url.rstrip("/") + "/send"


def _decode_bridge_reply(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolsiaError(
            E_PHONE_UNAVAILABLE,
            f"the phone send bridge answered something that is not JSON "
            f"({type(exc).__name__}: {exc})",
            how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
        ) from exc
    if not isinstance(payload, dict):
        raise PolsiaError(
            E_PHONE_UNAVAILABLE,
            "the phone send bridge answered a non-object JSON payload",
            how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
        )
    return payload


def _post_bridge(
    endpoint: str, token: str, payload: dict[str, Any], timeout: float
) -> tuple[int, dict[str, Any]]:
    """POST one send to the bridge (blocking; the caller moves it off the event loop)."""
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Bridge-Token": token,
        },
    )
    try:
        with _BRIDGE_OPENER.open(request, timeout=timeout) as response:
            return response.status, _decode_bridge_reply(response.read(BRIDGE_MAX_REPLY_BYTES))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, _decode_bridge_reply(exc.read(BRIDGE_MAX_REPLY_BYTES))
        finally:
            exc.close()


def _bridge_timeout(url: str, timeout: float) -> PolsiaError:
    return PolsiaError(
        E_PHONE_UNAVAILABLE,
        f"the phone send bridge at {url} timed out after {timeout:g}s — nothing is claimed "
        "as sent",
        how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
        context={"bridge_url": url, "timeout_s": timeout},
    )


def _bridge_unreachable(url: str, exc: BaseException | str) -> PolsiaError:
    """A bridge connect failure, rendered from whichever shape `URLError.reason` carries.

    `urllib.error.URLError.reason` is `str | BaseException` (typeshed): the socket layer reports
    a plain message ("Connection refused") and the URL layer an exception.  A string rendered as
    `str: <text>` would read like a fabricated exception type, so the string arm is rendered
    as-is; an exception keeps its own type name.
    """
    detail = f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else exc
    return PolsiaError(
        E_PHONE_UNAVAILABLE,
        f"the phone send bridge at {url} is unreachable ({detail}) — start "
        "scripts/phone_send_bridge.py on the host, then retry",
        how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
        context={"bridge_url": url},
    )


def _map_bridge_reply(status: int, payload: dict[str, Any], url: str) -> SendResult:
    """Turn the bridge's answer into the SAME typed result/refusal the direct path produces."""
    if status == 200 and payload.get("status") == "sent":
        sent_at = payload.get("sent_at")
        to_masked = payload.get("to_masked")
        driver_name = payload.get("driver")
        if driver_name != DRIVER_NAME:
            raise PolsiaError(
                E_PHONE_UNAVAILABLE,
                f"the phone send bridge result came from an unrecognised driver ({driver_name!r})",
                how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
                context={"bridge_url": url, "driver": driver_name},
            )
        if not isinstance(sent_at, str) or not sent_at.strip():
            raise PolsiaError(
                E_PHONE_UNAVAILABLE,
                "the phone send bridge did not report a send time — no send is claimed",
                how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
                context={"bridge_url": url},
            )
        if not isinstance(to_masked, str) or not to_masked.strip():
            raise PolsiaError(
                E_PHONE_UNAVAILABLE,
                "the phone send bridge did not report a masked recipient — no send is claimed",
                how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
                context={"bridge_url": url},
            )
        return SendResult(sent_at=sent_at, to_masked=to_masked.strip(), driver=DRIVER_NAME)

    code = payload.get("code")
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        message = f"the phone send bridge refused the send (HTTP {status})"
    if not isinstance(code, str) or code not in BRIDGE_CODES:
        if status == 401:  # a token gate that did not name itself still means E_AUTH
            code = E_AUTH
        else:
            raise PolsiaError(
                E_PHONE_UNAVAILABLE,
                f"the phone send bridge at {url} answered HTTP {status} without a recognised "
                "refusal",
                how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
                context={"bridge_url": url, "bridge_status": status},
            )
    hint = payload.get("how_to_fix")
    how_to_fix = hint if isinstance(hint, str) and hint.strip() else (
        _BRIDGE_HINTS.get(code) or _HINTS.get(code) or ERROR_HINTS.get(code, "")
    )
    if code == E_AUTH:
        raise PolsiaError(
            E_AUTH,
            f"the phone send bridge at {url} rejected the bridge token: {message}",
            how_to_fix=how_to_fix,
            context={"bridge_url": url, "bridge_status": status},
        )
    raise PolsiaError(
        code,
        f"the phone send bridge at {url} refused the send: {message}",
        how_to_fix=how_to_fix,
        context={"bridge_url": url, "bridge_status": status},
    )


async def _send_via_bridge(target: str, body: str, url: str, timeout: float) -> SendResult:
    """Validate the bridge configuration AT CALL TIME, then POST and map the answer."""
    token = os.environ.get(PHONE_SEND_BRIDGE_TOKEN_ENV, "").strip()
    if not token:
        raise PolsiaError(
            E_CONFIG,
            f"{BRIDGE_URL_ENV} is set ({url}) but {PHONE_SEND_BRIDGE_TOKEN_ENV} is empty — the "
            "bridge transport will not POST a send without its token",
            how_to_fix=_BRIDGE_HINTS[E_CONFIG],
            context={"bridge_url": url, "env": PHONE_SEND_BRIDGE_TOKEN_ENV},
        )
    if len(token) < BRIDGE_TOKEN_MIN_CHARS:
        raise PolsiaError(
            E_CONFIG,
            f"{PHONE_SEND_BRIDGE_TOKEN_ENV} is {len(token)} characters; the bridge requires at "
            f"least {BRIDGE_TOKEN_MIN_CHARS}",
            how_to_fix=_BRIDGE_HINTS[E_CONFIG],
            context={"bridge_url": url, "env": PHONE_SEND_BRIDGE_TOKEN_ENV},
        )
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise PolsiaError(
            E_CONFIG,
            f"{BRIDGE_URL_ENV} {url!r} is not an http(s) URL with a host — the bridge transport "
            "refuses to guess",
            how_to_fix=_BRIDGE_HINTS[E_CONFIG],
            context={"bridge_url": url},
        )

    endpoint = _bridge_endpoint(url)
    try:
        status, payload = await asyncio.wait_for(
            asyncio.to_thread(
                _post_bridge, endpoint, token, {"to": target, "body": body}, timeout
            ),
            timeout=timeout,
        )
    except TimeoutError as exc:
        raise _bridge_timeout(url, timeout) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise _bridge_timeout(url, timeout) from exc
        raise _bridge_unreachable(url, exc.reason or exc) from exc
    except OSError as exc:
        raise _bridge_unreachable(url, exc) from exc
    return _map_bridge_reply(status, payload, url)

_HINTS = {
    E_PHONE_SEND_UNPAIRED: (
        "open Google Messages web in the automation Chrome (dedicated --user-data-dir) and pair it "
        "with the phone once; then retry"
    ),
    E_PHONE_UNAVAILABLE: (
        "launch the automation Chrome with --remote-debugging-port=9223 and the dedicated profile, "
        "confirm scripts/phone_send_driver.py exists, then retry"
    ),
}

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DIGITS = re.compile(r"\d+")


# ── typed results ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DriverRun:
    """One finished driver process — the exact shape the process boundary returns."""

    returncode: int
    stdout: str
    stderr: str = ""


@dataclass(frozen=True)
class SendResult:
    sent_at: str
    to_masked: str
    driver: str = DRIVER_NAME

    def payload(self) -> dict[str, str]:
        return {"sent_at": self.sent_at, "to_masked": self.to_masked, "driver": self.driver}


# ── validation ─────────────────────────────────────────────────────────────────────────────


def _invalid(message: str, how_to_fix: str, **context: Any) -> PolsiaError:
    return PolsiaError(E_VALIDATION, message, how_to_fix=how_to_fix, context=context)


def validate_target(raw: Any) -> str:
    """Canonicalise `to`: a positive thread id, or E.164 digits with an optional leading `+`.

    The ambiguity between "a short number" and "a small thread id" is resolved the same way the
    driver resolves it: `resolve_thread` tries an exact thread-id match FIRST, so returning the
    digits unchanged loses nothing (a `+` is preserved because it is part of the E.164 shape).
    """
    if raw is None or isinstance(raw, bool):
        raise _invalid("`to` is required: a positive thread id or E.164 digits", _TO_FIX, to=repr(raw))
    if isinstance(raw, int):
        if raw <= 0 or raw > THREAD_ID_MAX:
            raise _invalid(f"`to` must be a positive thread id (1..{THREAD_ID_MAX})", _TO_FIX, to=raw)
        return str(raw)
    if not isinstance(raw, str):
        raise _invalid(
            f"`to` must be a thread id or E.164 digits, not {type(raw).__name__}", _TO_FIX
        )
    text = raw.strip()
    if not text:
        raise _invalid("`to` is empty", _TO_FIX)
    if text.startswith("+"):
        digits = text[1:]
        if not digits.isdigit() or not E164_MIN_DIGITS <= len(digits) <= TO_MAX_DIGITS:
            raise _invalid(
                f"`to` {text!r} is not E.164: a + then {E164_MIN_DIGITS}..{TO_MAX_DIGITS} digits",
                _TO_FIX,
            )
        if digits.startswith("0"):
            raise _invalid(f"`to` {text!r} is not E.164: the number cannot start with 0", _TO_FIX)
        return "+" + digits
    if not text.isdigit():
        raise _invalid(
            f"`to` {text!r} must be digits only (no spaces, dashes or parentheses)", _TO_FIX
        )
    if len(text) >= E164_MIN_DIGITS:
        if len(text) <= TO_MAX_DIGITS and not text.startswith("0"):
            return text
        raise _invalid(
            f"`to` {text!r} is neither a positive thread id nor E.164 digits", _TO_FIX
        )
    if text.startswith("0"):
        raise _invalid(f"`to` {text!r} is not a positive thread id", _TO_FIX)
    return text


_TO_FIX = "send a thread id from GET /api/v1/phone/summary, or E.164 digits like +15550104477"


def validate_body(raw: Any) -> str:
    if raw is None or isinstance(raw, bool) or not isinstance(raw, str):
        raise _invalid(
            f"`body` must be a string, not {type(raw).__name__}", f"send {BODY_MIN}..{BODY_MAX} characters"
        )
    if not raw.strip():
        raise _invalid("`body` is empty", f"send {BODY_MIN}..{BODY_MAX} characters of text")
    if len(raw) > BODY_MAX:
        raise _invalid(
            f"`body` is {len(raw)} characters; this build sends at most {BODY_MAX}",
            f"shorten the message to {BODY_MAX} characters or fewer",
            length=len(raw),
        )
    match = _CONTROL_CHARS.search(raw)
    if match:
        raise _invalid(
            f"`body` carries a control character (U+{ord(match.group(0)):04X}); it would not survive the page",
            "remove control characters (newlines and tabs are allowed)",
        )
    return raw


# ── masking ─────────────────────────────────────────────────────────────────────────────────


def mask_number(value: Any) -> str:
    """Keep only the last four digits (a `+` prefix is preserved); never return the raw number."""
    raw = "" if value is None else str(value)
    digits = "".join(_DIGITS.findall(raw))
    if not digits:
        return ""
    keep = digits[-4:] if len(digits) > 4 else digits
    prefix = "+" if raw.strip().startswith("+") else ""
    return prefix + "•" * max(0, len(digits) - len(keep)) + keep


def mask_target(raw: Any) -> str:
    """What an audit row may say about `to`: `thread #N`, or a masked number."""
    text = "" if raw is None else str(raw).strip()
    digits = "".join(_DIGITS.findall(text))
    if digits and not text.startswith("+") and len(digits) < E164_MIN_DIGITS:
        return f"thread #{digits}"
    return mask_number(text)


# ── the process boundary ───────────────────────────────────────────────────────────────────


def driver_executable() -> Path:
    """Resolve the driver path (env override honoured, missing file reported by `send`)."""
    override = os.environ.get("POLSIA_PHONE_SEND_DRIVER", "").strip()
    return Path(override) if override else DRIVER_PATH


def driver_argv(
    mode: str,
    *,
    to: str | None = None,
    body_file: Path | None = None,
    port: int = DEFAULT_PORT,
    executable: str | None = None,
    driver: Path | None = None,
) -> list[str]:
    """The argv the driver is started with — a LIST, so nothing is ever shell-interpreted."""
    argv = [executable or sys.executable, str(driver or driver_executable()), f"--{mode}"]
    if to is not None:
        argv += ["--to", to]
    if body_file is not None:
        argv += ["--body-file", str(body_file)]
    argv += ["--port", str(port), "--json"]
    return argv


async def _run_driver(argv: list[str], timeout: float) -> DriverRun:
    """Start the driver, read its one JSON line, and guarantee the child does not outlive us."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    return DriverRun(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
    )


# ── the JSON protocol ──────────────────────────────────────────────────────────────────────


def parse_driver_stdout(returncode: int, stdout: str, stderr: str = "") -> SendResult:
    """Turn the driver's one-line envelope into a `SendResult`, or refuse BY NAME."""
    text = (stdout or "").strip()
    if not text:
        raise PolsiaError(
            E_PHONE_UNAVAILABLE,
            f"the phone send driver exited {returncode} without a JSON result",
            how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
            context={"exit_code": returncode, "stderr_tail": (stderr or "")[-400:]},
        )
    try:
        payload = json.loads(text.splitlines()[-1])
    except json.JSONDecodeError as exc:
        raise PolsiaError(
            E_PHONE_UNAVAILABLE,
            f"the phone send driver printed something that is not its JSON envelope "
            f"({type(exc).__name__}: {exc})",
            how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
            context={"exit_code": returncode, "stdout_tail": text[-400:]},
        ) from exc
    if not isinstance(payload, dict):
        raise PolsiaError(
            E_PHONE_UNAVAILABLE,
            "the phone send driver answered a non-object JSON payload",
            how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
            context={"exit_code": returncode},
        )

    if payload.get("ok") is True:
        if returncode != 0:
            raise PolsiaError(
                E_PHONE_UNAVAILABLE,
                f"the phone send driver claimed ok but exited {returncode}",
                how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
                context={"exit_code": returncode},
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise PolsiaError(
                E_PHONE_UNAVAILABLE,
                "the phone send driver answered ok without its data object",
                how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
            )
        sent_at = data.get("sent_at")
        to_masked = data.get("to_masked")
        driver_name = data.get("driver")
        if driver_name != DRIVER_NAME:
            raise PolsiaError(
                E_PHONE_UNAVAILABLE,
                f"the phone send result came from an unrecognised driver ({driver_name!r})",
                how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
                context={"driver": driver_name},
            )
        if not isinstance(sent_at, str) or not sent_at.strip():
            raise PolsiaError(
                E_PHONE_UNAVAILABLE,
                "the phone send driver did not report a send time — no send is claimed",
                how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
            )
        if not isinstance(to_masked, str) or not to_masked.strip():
            raise PolsiaError(
                E_PHONE_UNAVAILABLE,
                "the phone send driver did not report a masked recipient — no send is claimed",
                how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
            )
        return SendResult(sent_at=sent_at, to_masked=to_masked.strip(), driver=DRIVER_NAME)

    error = payload.get("error")
    if payload.get("ok") is False and isinstance(error, dict):
        code = error.get("code")
        message = error.get("message") or "the phone send driver refused the send"
        how_to_fix = error.get("how_to_fix") or ""
        # Bound ONCE: `isinstance(error.get("context"), dict)` narrows only the expression mypy
        # can see, not a second `.get()` call on the next line — the old one-liner's type stayed
        # `Any | dict[Any, Any] | None` and failed the `dict[str, Any]` parameter below.  The
        # values are identical; only the annotation is new.
        raw_context = error.get("context")
        context: dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}
        if not isinstance(code, str) or code not in DRIVER_CODES:
            context = {**context, "driver_code": code}
            code = E_PHONE_UNAVAILABLE
        return _raise_driver_refusal(code, message, how_to_fix, context, returncode)

    raise PolsiaError(
        E_PHONE_UNAVAILABLE,
        "the phone send driver answered neither ok nor a named refusal",
        how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
        context={"exit_code": returncode},
    )


def _raise_driver_refusal(
    code: str, message: str, how_to_fix: str, context: dict[str, Any], returncode: int
) -> SendResult:
    raise PolsiaError(
        code,
        str(message),
        how_to_fix=how_to_fix or _HINTS.get(code, ""),
        context={**context, "driver_exit": returncode},
    )


# ── orchestration ─────────────────────────────────────────────────────────────────────────


async def send(
    raw_to: Any,
    raw_body: Any,
    *,
    sandbox: bool | None = None,
    timeout: float | None = None,
) -> SendResult:
    """Run the ordered gate and return the ONE result the driver actually observed.

    `sandbox`/`timeout` exist for tests and for a future caller that already resolved policy; the
    route passes neither, so the live `settings.sandbox_mode` is the gate.
    """
    if settings.sandbox_mode if sandbox is None else sandbox:
        raise PolsiaError(
            E_SANDBOX_BLOCKED,
            "phone send refused: SANDBOX_MODE is on, so no real SMS is sent from this deployment",
            how_to_fix=ERROR_HINTS.get(
                E_SANDBOX_BLOCKED, "set SANDBOX_MODE=false once the phone pairing is configured"
            ),
            context={"sandbox_mode": True},
        )

    target = validate_target(raw_to)
    body = validate_body(raw_body)

    bridged = bridge_url()
    if bridged:
        # The bridge is a TRANSPORT, never a policy bypass: sandbox + validation already ran.
        return await _send_via_bridge(
            target, body, bridged, BRIDGE_TIMEOUT_S if timeout is None else timeout
        )

    driver = driver_executable()
    if not driver.is_file():
        raise PolsiaError(
            E_PHONE_UNAVAILABLE,
            f"the phone send driver is missing: {driver}",
            how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
            context={"driver_path": str(driver)},
        )

    limit = SEND_TIMEOUT_S if timeout is None else timeout
    try:
        fd, raw_path = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".txt")
    except OSError as exc:
        raise PolsiaError(
            E_PHONE_UNAVAILABLE,
            f"the phone send body file could not be staged ({type(exc).__name__}: {exc})",
            how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
            context={"scratch_dir": tempfile.gettempdir()},
        ) from exc
    body_path = Path(raw_path)
    argv = driver_argv("send", to=target, body_file=body_path, driver=driver)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
        try:
            run = await asyncio.wait_for(_run_driver(argv, limit), timeout=limit)
        except TimeoutError as exc:
            raise PolsiaError(
                E_PHONE_UNAVAILABLE,
                f"the phone send driver timed out after {limit:g}s — nothing is claimed as sent",
                how_to_fix=_HINTS[E_PHONE_UNAVAILABLE],
                context={"timeout_s": limit},
            ) from exc
    finally:
        try:
            body_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - scratch-dir cleanup is the only caller
            logger.warning("could not remove the phone send body file %s", body_path)
    return parse_driver_stdout(run.returncode, run.stdout, run.stderr)


def http_refusal(exc: PolsiaError) -> HTTPException:
    """The coded HTTPException for a send refusal (same detail shape `deps.as_http_exception` builds)."""
    status = SEND_STATUS_BY_CODE.get(exc.code) or http_status_for(exc.code)
    return HTTPException(
        status_code=status,
        detail={
            "code": exc.code,
            "message": exc.message,
            "how_to_fix": exc.how_to_fix or ERROR_HINTS.get(exc.code, ""),
            "context": exc.context,
        },
        headers={"X-Polsia-Error-Code": exc.code},
    )


__all__ = [
    "BODY_MAX",
    "BRIDGE_TIMEOUT_S",
    "BRIDGE_URL_ENV",
    "DRIVER_NAME",
    "DRIVER_PATH",
    "DriverRun",
    "E_PHONE_SEND_UNPAIRED",
    "PHONE_SEND_BRIDGE_TOKEN_ENV",
    "SEND_STATUS_BY_CODE",
    "SendResult",
    "bridge_url",
    "driver_argv",
    "driver_executable",
    "http_refusal",
    "mask_number",
    "mask_target",
    "parse_driver_stdout",
    "send",
    "validate_body",
    "validate_target",
]
