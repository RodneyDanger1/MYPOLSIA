#!/usr/bin/env python3
"""`scripts/phone_send_bridge.py` — the HOST COMPANION bridge for phone send.

WHAT THIS IS
    A tiny, stdlib-only HTTP server that runs ON THE HOST (where the automation Chrome and the
    paired Google Messages web session live) and exposes ONE capability to a containerized
    backend: `POST /send`.  It wraps the SAME `scripts/phone_send_driver.py` the host-run backend
    uses — there is no second send path, no fixture branch and no pretend success.

WHY IT EXISTS
    `USAGE.md` §5 documented a real limitation: inside Docker the backend cannot reach the host's
    Chrome DevTools port, so phone send only worked when the API ran on the host.  This bridge
    closes that gap without widening the app's own attack surface: the app keeps its sandbox gate
    and input validation, and the bridge adds an independent token gate on the host side.

SECURITY POSTURE (read before changing a default)
    * Default bind is `127.0.0.1` — a bridge that is not explicitly exposed cannot be reached from
      a container, another machine or the LAN.
    * `--host 0.0.0.0` (or a docker bridge address) is the ONLY way a container reaches it, and
      that is exactly when the token + a host firewall rule become mandatory.  `startup_lines`
      prints that warning; the docs repeat it.
    * The token comes from `PHONE_SEND_BRIDGE_TOKEN` ONLY.  It is NEVER an argv flag: argv is
      world-readable in a process list.  It must be >= 24 chars and is compared with
      `hmac.compare_digest` so a wrong token cannot be discovered by timing.
    * No token (or a short one) = the bridge REFUSES TO START.  OFF BY DEFAULT means "no token, no
      server", not "server with an open door".
    * Request bodies are capped at 8 KiB; the driver has a hard timeout; every refusal names its
      code.  Neither the token nor the message body is ever logged — the access log carries only
      method, path and status.

PROTOCOL
    GET  /health           -> 200 {"status":"ok","paired":bool,"version":str}
    POST /send             -> 200 {"status":"sent","sent_at","to_masked","driver"}
        X-Bridge-Token       401 {"status":"refused","code":"E_AUTH","message"}
        {"to","body"}        409/422/503 {"status":"refused","code","message","how_to_fix"}
    Anything else          405 on /send for non-POST, 404 for unknown paths.

    The `/send` refusals mirror `services.phone_send_service`: E_AUTH, E_VALIDATION,
    E_PHONE_SEND_UNPAIRED, E_PHONE_UNAVAILABLE — so the backend maps them onto the SAME typed
    `PolsiaError` codes it already ships.

RUN
    PHONE_SEND_BRIDGE_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')" \\
        python scripts/phone_send_bridge.py --port 9227
    curl -s http://127.0.0.1:9227/health
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

logger = logging.getLogger("phone_send_bridge")

VERSION = "1.0"
DRIVER_NAME = "cdp-google-messages"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9227
DEFAULT_DRIVER_PORT = 9223
#: The bridge's own request deadline.  The plan caps it at 90 s; the driver gets 85 so the bridge
#: always answers (with a named refusal) before a client's 90 s patience runs out.
REQUEST_TIMEOUT_S = 85.0
HEALTH_TIMEOUT_S = 15.0
MAX_BODY_BYTES = 8 * 1024
MAX_TOKEN_CHARS = 512
TOKEN_ENV = "PHONE_SEND_BRIDGE_TOKEN"
TOKEN_MIN_CHARS = 24
TEMP_PREFIX = "polsia-phone-bridge-"

E_AUTH = "E_AUTH"
E_CONFIG = "E_CONFIG"
E_NOT_FOUND = "E_NOT_FOUND"
E_PHONE_SEND_UNPAIRED = "E_PHONE_SEND_UNPAIRED"
E_PHONE_UNAVAILABLE = "E_PHONE_UNAVAILABLE"
E_VALIDATION = "E_VALIDATION"

BODY_MAX = 640
E164_MIN_DIGITS = 8
TO_MAX_DIGITS = 15
THREAD_ID_MAX = 2**31 - 1

DRIVER_CODES = frozenset({E_PHONE_SEND_UNPAIRED, E_PHONE_UNAVAILABLE, E_VALIDATION})
STATUS_BY_CODE = {
    E_AUTH: 401,
    E_VALIDATION: 422,
    E_PHONE_SEND_UNPAIRED: 409,
    E_PHONE_UNAVAILABLE: 503,
    E_CONFIG: 503,
    E_NOT_FOUND: 404,
}
HINTS = {
    E_AUTH: "send the exact PHONE_SEND_BRIDGE_TOKEN value in the X-Bridge-Token header",
    E_CONFIG: "start the bridge with PHONE_SEND_BRIDGE_TOKEN set (>= 24 chars), then retry",
    E_VALIDATION: "send {\"to\": \"<thread_id | E.164 digits>\", \"body\": \"1..640 chars\"}",
    E_PHONE_SEND_UNPAIRED: "open Google Messages web in the automation Chrome and pair it once",
    E_PHONE_UNAVAILABLE: "start the automation Chrome with --remote-debugging-port=9223 and retry",
}

DRIVER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "phone_send_driver.py"

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DIGITS = re.compile(r"\d+")


# ── typed refusals ──────────────────────────────────────────────────────────────────────────


class BridgeConfigError(Exception):
    """A configuration error the process cannot start with (missing/short token, bad flag)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class BridgeRefusal(Exception):
    """A named refusal answered to one request; `status` is the HTTP status it maps to."""

    def __init__(self, code: str, message: str, how_to_fix: str = "", status: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.how_to_fix = how_to_fix or HINTS.get(code, "")
        self.status = status or STATUS_BY_CODE.get(code, 500)

    def payload(self) -> dict[str, str]:
        return {
            "status": "refused",
            "code": self.code,
            "message": self.message,
            "how_to_fix": self.how_to_fix,
        }


@dataclass(frozen=True)
class Options:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    driver_port: int = DEFAULT_DRIVER_PORT
    driver: Path | None = None


@dataclass(frozen=True)
class DriverRun:
    returncode: int
    stdout: str
    stderr: str = ""


# ── configuration ──────────────────────────────────────────────────────────────────────────


def is_loopback(host: str) -> bool:
    """True when the bind address cannot be reached from another machine."""
    value = (host or "").strip().lower()
    return value in {"localhost", "::1"} or value.startswith("127.")


def parse_args(argv: list[str]) -> Options:
    """Hand-rolled so every mistake is a named error and `--token` is refused explicitly."""
    host = DEFAULT_HOST
    port = DEFAULT_PORT
    driver_port = DEFAULT_DRIVER_PORT
    driver: Path | None = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--host":
            index += 1
            if index >= len(argv):
                raise BridgeConfigError(E_CONFIG, "--host needs an address (default 127.0.0.1)")
            host = argv[index]
        elif arg == "--port":
            index += 1
            try:
                port = int(argv[index])
            except (IndexError, ValueError) as exc:
                raise BridgeConfigError(E_CONFIG, f"--port needs an integer ({exc})") from exc
            if not 1 <= port <= 65535:
                raise BridgeConfigError(E_CONFIG, "--port must be 1..65535")
        elif arg == "--driver-port":
            index += 1
            try:
                driver_port = int(argv[index])
            except (IndexError, ValueError) as exc:
                raise BridgeConfigError(E_CONFIG, f"--driver-port needs an integer ({exc})") from exc
            if not 1 <= driver_port <= 65535:
                raise BridgeConfigError(E_CONFIG, "--driver-port must be 1..65535")
        elif arg == "--driver":
            index += 1
            if index >= len(argv):
                raise BridgeConfigError(E_CONFIG, "--driver needs a path")
            driver = Path(argv[index])
        elif arg in ("--token", "--bridge-token"):
            raise BridgeConfigError(
                E_CONFIG,
                f"the token is read from {TOKEN_ENV} only — argv is world-readable in a process "
                "list, so this flag is refused by design",
            )
        elif arg in ("-h", "--help"):
            raise BridgeConfigError(
                E_CONFIG,
                "usage: phone_send_bridge.py [--host 127.0.0.1] [--port 9227] "
                "[--driver-port 9223] [--driver PATH]  (token from PHONE_SEND_BRIDGE_TOKEN)",
            )
        else:
            raise BridgeConfigError(E_CONFIG, f"unknown argument: {arg}")
        index += 1
    return Options(host=host, port=port, driver_port=driver_port, driver=driver)


def token_from_env() -> str:
    return os.environ.get(TOKEN_ENV, "").strip()


def driver_path(options: Options) -> Path:
    return options.driver or Path(os.environ.get("POLSIA_PHONE_SEND_DRIVER", "").strip() or DRIVER_PATH)


def startup_lines(options: Options, token: str) -> list[str]:
    """The banner printed at startup.  It must never contain the token value."""
    lines = [
        f"phone send bridge {VERSION} listening on http://{options.host}:{options.port}",
        f"driver: {driver_path(options)} (CDP port {options.driver_port})",
        f"token: from {TOKEN_ENV} ({len(token)} characters, never logged)",
    ]
    if not is_loopback(options.host):
        lines.append(
            "WARNING: bound to a NON-LOOPBACK interface. Any machine that can reach this port can "
            "attempt a send. The bridge token is mandatory, and a host firewall rule limiting this "
            "port to the Docker bridge subnet (or the single client host) is mandatory too."
        )
    return lines


# ── the driver boundary ────────────────────────────────────────────────────────────────────


def _run_driver(argv: list[str], timeout: float) -> DriverRun:
    """Run the driver once.  THE test boundary — the only stubbed function in this file's tests."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeRefusal(
            E_PHONE_UNAVAILABLE,
            f"the phone send driver timed out after {timeout:g}s — nothing is claimed as sent",
        ) from exc
    except OSError as exc:
        raise BridgeRefusal(
            E_PHONE_UNAVAILABLE,
            f"the phone send driver could not be started ({type(exc).__name__}: {exc})",
        ) from exc
    return DriverRun(
        returncode=completed.returncode,
        stdout=completed.stdout.decode("utf-8", "replace"),
        stderr=completed.stderr.decode("utf-8", "replace"),
    )


def _driver_argv(mode: str, *, driver: Path, port: int, to: str | None = None,
                 body_file: Path | None = None) -> list[str]:
    argv = [sys.executable, str(driver), f"--{mode}"]
    if to is not None:
        argv += ["--to", to]
    if body_file is not None:
        argv += ["--body-file", str(body_file)]
    return argv + ["--port", str(port), "--json"]


def _parse_driver(returncode: int, stdout: str, stderr: str = "") -> dict[str, Any]:
    """The driver's one-line envelope -> a `sent` dict, or a named refusal.  Same rules as the app."""
    text = (stdout or "").strip()
    if not text:
        raise BridgeRefusal(
            E_PHONE_UNAVAILABLE,
            f"the phone send driver exited {returncode} without a JSON result",
        )
    try:
        payload = json.loads(text.splitlines()[-1])
    except json.JSONDecodeError as exc:
        raise BridgeRefusal(
            E_PHONE_UNAVAILABLE,
            f"the phone send driver printed something that is not its JSON envelope "
            f"({type(exc).__name__})",
        ) from exc
    if not isinstance(payload, dict):
        raise BridgeRefusal(E_PHONE_UNAVAILABLE, "the phone send driver answered a non-object payload")

    if payload.get("ok") is True:
        if returncode != 0:
            raise BridgeRefusal(
                E_PHONE_UNAVAILABLE, f"the phone send driver claimed ok but exited {returncode}"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BridgeRefusal(E_PHONE_UNAVAILABLE, "the phone send driver answered ok without data")
        sent_at = data.get("sent_at")
        to_masked = data.get("to_masked")
        driver_name = data.get("driver")
        if driver_name != DRIVER_NAME:
            raise BridgeRefusal(
                E_PHONE_UNAVAILABLE,
                f"the phone send result came from an unrecognised driver ({driver_name!r})",
            )
        if not isinstance(sent_at, str) or not sent_at.strip():
            raise BridgeRefusal(
                E_PHONE_UNAVAILABLE,
                "the phone send driver did not report a send time — no send is claimed",
            )
        if not isinstance(to_masked, str) or not to_masked.strip():
            raise BridgeRefusal(
                E_PHONE_UNAVAILABLE,
                "the phone send driver did not report a masked recipient — no send is claimed",
            )
        return {
            "status": "sent",
            "sent_at": sent_at,
            "to_masked": to_masked.strip(),
            "driver": DRIVER_NAME,
        }

    error = payload.get("error")
    if payload.get("ok") is False and isinstance(error, dict):
        code = error.get("code")
        message = error.get("message") or "the phone send driver refused the send"
        if not isinstance(code, str) or code not in DRIVER_CODES:
            raise BridgeRefusal(
                E_PHONE_UNAVAILABLE,
                f"the phone send driver refused with an unrecognised code ({code!r})",
            )
        raise BridgeRefusal(code, str(message), how_to_fix=str(error.get("how_to_fix") or ""))

    raise BridgeRefusal(
        E_PHONE_UNAVAILABLE,
        "the phone send driver answered neither ok nor a named refusal",
    )


# ── request validation ────────────────────────────────────────────────────────────────────


def _validate_target(raw: Any) -> str:
    if raw is None or isinstance(raw, bool):
        raise BridgeRefusal(E_VALIDATION, "`to` is required: a positive thread id or E.164 digits")
    if isinstance(raw, int):
        if not 1 <= raw <= THREAD_ID_MAX:
            raise BridgeRefusal(E_VALIDATION, f"`to` must be a positive thread id (1..{THREAD_ID_MAX})")
        return str(raw)
    if not isinstance(raw, str):
        raise BridgeRefusal(E_VALIDATION, f"`to` must be a string or integer, not {type(raw).__name__}")
    text = raw.strip()
    if not text:
        raise BridgeRefusal(E_VALIDATION, "`to` is empty")
    if text.startswith("+"):
        digits = text[1:]
        if not digits.isdigit() or not E164_MIN_DIGITS <= len(digits) <= TO_MAX_DIGITS:
            raise BridgeRefusal(E_VALIDATION, f"`to` {text!r} is not E.164 (+ then 8..15 digits)")
        if digits.startswith("0"):
            raise BridgeRefusal(E_VALIDATION, f"`to` {text!r} is not E.164: cannot start with 0")
        return "+" + digits
    if not text.isdigit():
        raise BridgeRefusal(E_VALIDATION, f"`to` {text!r} must be digits only")
    if len(text) >= E164_MIN_DIGITS:
        if len(text) <= TO_MAX_DIGITS and not text.startswith("0"):
            return text
        raise BridgeRefusal(E_VALIDATION, f"`to` {text!r} is neither a thread id nor E.164 digits")
    if text.startswith("0"):
        raise BridgeRefusal(E_VALIDATION, f"`to` {text!r} is not a positive thread id")
    return text


def _validate_body(raw: Any) -> str:
    if not isinstance(raw, str):
        raise BridgeRefusal(E_VALIDATION, f"`body` must be a string, not {type(raw).__name__}")
    if not raw.strip():
        raise BridgeRefusal(E_VALIDATION, "`body` is empty")
    if len(raw) > BODY_MAX:
        raise BridgeRefusal(
            E_VALIDATION, f"`body` is {len(raw)} characters; this bridge sends at most {BODY_MAX}"
        )
    match = _CONTROL_CHARS.search(raw)
    if match:
        raise BridgeRefusal(
            E_VALIDATION,
            f"`body` carries a control character (U+{ord(match.group(0)):04X})",
        )
    return raw


def _validate_payload(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise BridgeRefusal(E_VALIDATION, "the request body must be a JSON object with `to` and `body`")
    return _validate_target(payload.get("to")), _validate_body(payload.get("body"))


# ── HTTP handler ──────────────────────────────────────────────────────────────────────────


class BridgeHandler(BaseHTTPRequestHandler):
    server: BridgeServer
    protocol_version = "HTTP/1.1"

    #: Only method/path/status ever reach the log — never headers (the token lives there) or bodies.
    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("bridge %s %s", getattr(self, "command", "?"), getattr(self, "path", "?"))

    def log_error(self, fmt: str, *args: Any) -> None:
        logger.warning("bridge %s %s: %s", getattr(self, "command", "?"), getattr(self, "path", "?"), fmt)

    # ── verbs ──
    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/health":
            self._health()
        elif self.path.split("?", 1)[0] == "/send":
            self._method_not_allowed()
        else:
            self._refuse(BridgeRefusal(E_NOT_FOUND, f"no route for GET {self.path}"))

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/send":
            self._send()
        elif path == "/health":
            self._method_not_allowed()
        else:
            self._refuse(BridgeRefusal(E_NOT_FOUND, f"no route for POST {self.path}"))

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:
        self._method_not_allowed()

    def do_HEAD(self) -> None:
        self._method_not_allowed()

    # ── responses ──
    def _write_json(self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)
        self.close_connection = True
        logger.debug("bridge %s %s -> %s", self.command, self.path.split("?", 1)[0], status)

    def _refuse(self, refusal: BridgeRefusal, headers: dict[str, str] | None = None) -> None:
        self._write_json(refusal.status, refusal.payload(), headers)

    def _method_not_allowed(self) -> None:
        self._refuse(
            BridgeRefusal(
                E_VALIDATION,
                f"method {self.command} is not allowed here; use POST /send or GET /health",
                status=405,
            ),
            {"Allow": "GET, POST"},
        )

    def _token_ok(self) -> bool:
        provided = self.headers.get("X-Bridge-Token")
        if not provided or len(provided) > MAX_TOKEN_CHARS:
            return False
        return hmac.compare_digest(provided.encode("utf-8"), str(self.server.bridge_token).encode("utf-8"))

    # ── health ──
    def _health(self) -> None:
        paired = False
        driver = self.server.bridge_driver
        if driver.is_file():
            try:
                run = _run_driver(
                    _driver_argv("check", driver=driver, port=self.server.bridge_driver_port),
                    min(HEALTH_TIMEOUT_S, REQUEST_TIMEOUT_S),
                )
                if run.returncode == 0:
                    payload = json.loads((run.stdout or "").strip().splitlines()[-1])
                    paired = payload.get("ok") is True and payload.get("data", {}).get("paired") is True
            except (BridgeRefusal, json.JSONDecodeError, IndexError, AttributeError, OSError):
                logger.info("bridge health: driver check did not report a paired session")
        self._write_json(200, {"status": "ok", "paired": bool(paired), "version": VERSION})

    # ── send ──
    def _send(self) -> None:
        # ORDER MATTERS (Windows): read/drain the request body BEFORE any refusal.  Answering
        # while the client is still writing its body surfaces as ConnectionAbortedError /
        # WinError 10053 instead of the intended 401/413 (measured in the V19 gate).
        try:
            raw = self._read_raw_body()
        except BridgeRefusal as refusal:
            self._refuse(refusal)
            return
        if not self._token_ok():
            logger.warning("bridge rejected a send without the configured token")
            self._refuse(BridgeRefusal(E_AUTH, "missing or invalid X-Bridge-Token"))
            return
        try:
            target, body = _validate_payload(self._json_from(raw))
            result = self._drive_send(target, body)
        except BridgeRefusal as refusal:
            self._refuse(refusal)
            return
        self._write_json(200, result)

    def _read_raw_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise BridgeRefusal(E_VALIDATION, "a Content-Length header is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise BridgeRefusal(E_VALIDATION, f"Content-Length is not an integer ({exc})") from exc
        if length < 1:
            raise BridgeRefusal(E_VALIDATION, "the request body is empty")
        if length > MAX_BODY_BYTES:
            # Drain a bounded head so the 413 is DELIVERED rather than reset mid-write on Windows.
            self._drain(min(length, MAX_BODY_BYTES + 1))
            raise BridgeRefusal(
                E_VALIDATION,
                f"the request body is {length} bytes; this bridge accepts at most {MAX_BODY_BYTES}",
                status=413,
            )
        return self.rfile.read(length)

    def _drain(self, count: int) -> None:
        if count <= 0:
            return
        try:
            self.rfile.read(count)
        except OSError:  # the peer already went away — the refusal is still the honest answer
            pass

    @staticmethod
    def _json_from(raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeRefusal(
                E_VALIDATION, f"the request body is not JSON ({type(exc).__name__})", status=400
            ) from exc

    def _drive_send(self, target: str, body: str) -> dict[str, Any]:
        driver = self.server.bridge_driver
        if not driver.is_file():
            raise BridgeRefusal(E_PHONE_UNAVAILABLE, f"the phone send driver is missing: {driver}")
        try:
            fd, raw_path = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".txt")
        except OSError as exc:
            raise BridgeRefusal(
                E_PHONE_UNAVAILABLE, f"the body file could not be staged ({type(exc).__name__}: {exc})"
            ) from exc
        body_path = Path(raw_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(body)
            run = _run_driver(
                _driver_argv("send", driver=driver, port=self.server.bridge_driver_port,
                             to=target, body_file=body_path),
                REQUEST_TIMEOUT_S,
            )
        finally:
            try:
                body_path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - scratch cleanup only
                logger.warning("could not remove the phone send body file")
        return _parse_driver(run.returncode, run.stdout, run.stderr)


# ── server ────────────────────────────────────────────────────────────────────────────────


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], token: str, driver_port: int, driver: Path) -> None:
        super().__init__(address, BridgeHandler)
        self.bridge_token = token
        self.bridge_driver_port = driver_port
        self.bridge_driver = driver


def build_server(host: str, port: int, *, token: str, driver_port: int = DEFAULT_DRIVER_PORT,
                 driver: Path | None = None) -> BridgeServer:
    """Validate the token posture, then bind.  Raises `BridgeConfigError` when the bridge is OFF."""
    value = (token or "").strip()
    if len(value) < TOKEN_MIN_CHARS:
        raise BridgeConfigError(
            E_CONFIG,
            f"{TOKEN_ENV} is missing or shorter than {TOKEN_MIN_CHARS} characters — the bridge "
            "stays OFF (nothing was started)",
        )
    return BridgeServer((host, port), token=value, driver_port=driver_port,
                        driver=driver or driver_path(Options()))


# ── main ──────────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        options = parse_args(list(sys.argv[1:] if argv is None else argv))
        token = token_from_env()
        server = build_server(options.host, options.port, token=token,
                              driver_port=options.driver_port, driver=driver_path(options))
    except BridgeConfigError as exc:
        logger.error("%s", exc.message)
        return 2
    except OSError as exc:
        logger.error("could not bind the bridge: %s", exc)
        return 2
    for line in startup_lines(options, token):
        logger.info("%s", line)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("bridge stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
