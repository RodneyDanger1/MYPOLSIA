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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.config import settings
from app.core.errors import (
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
        context = error.get("context") if isinstance(error.get("context"), dict) else {}
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
    "DRIVER_NAME",
    "DRIVER_PATH",
    "DriverRun",
    "E_PHONE_SEND_UNPAIRED",
    "SEND_STATUS_BY_CODE",
    "SendResult",
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
