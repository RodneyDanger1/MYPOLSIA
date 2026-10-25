"""Domain and link trust checks (V29/L6) — keyless, permanently free, sync.

Three sources, no key, no quota, no credit card:

  * **Cloudflare DNS-over-HTTPS** (`https://cloudflare-dns.com/dns-query`, `Accept:
    application/dns-json`) for A/AAAA.  The DNS **RCODE lives in the body** (`Status`): `0` is
    NOERROR, `3` is NXDOMAIN.  An HTTP 200 says nothing about the name — parsing the status code
    alone would report every dead domain as healthy (measured 2026-10-20, plan §2.3).
  * **A direct TLS handshake** (`ssl.create_default_context`) for the peer certificate's
    `notAfter`.  A certificate failure is `tls.ok=false` with its reason — NEVER an exception out
    of this module, because a badge that crashes the route is worse than a badge that says so.
  * **Internet Archive Wayback** (`https://archive.org/wayback/available`) for a snapshot of a
    dead link.  An empty `archived_snapshots` object is `available: false`, not an error.

Two entry points:

* `check_domain(host)` — DoH A/AAAA + TLS.  Each leg's failure is its OWN field on a served
  result (`dns.reason`, `tls.reason`), so one dead source cannot take the answer with it.
* `check_link(url)` — HEAD (GET on `405`) with manual redirects (max 3), revalidating **every**
  hop: http(s) only, ports 80/443 only, and no private/loopback/link-local/reserved/multicast/
  unspecified address.  That is the SSRF guard: a public URL that 302s to `127.0.0.1` or
  `169.254.169.254` is refused before the second request is ever sent.  Redirects are followed by
  hand (`follow_redirects=False`) precisely so the guard can see every `Location`.

Neither leg dials a NAME.  `check_domain`'s TLS socket targets the DoH answer the module's own
predicate vetted, and `check_link`'s HTTP socket targets the answer `_resolve_public` returned —
both keep the name for the `Host` header and for TLS SNI (`server_hostname`), so the certificate
is still verified against the host while the TCP dial goes to an address a guard approved.  A
second resolution at connect time is how a vetted public name reaches a private address (F-27).

Two exported helpers for callers that fetch for themselves (`web_fetch`, CONTRACTS §4.15):

* `resolve_egress(host, port)` — the ONE egress decision: the port must be 80/443 and every
  address the name resolves to (Cloudflare DoH, never the OS resolver) must be globally routable
  (`is_global`, so RFC 6598 shared space and multicast are refused).  A mixed answer is refused
  whole.
* `pinned_http_get(url, *, timeout, max_bytes, headers=None)` — GET through one of those
  addresses, `Host`/SNI preserved, no redirect following, and a body past `max_bytes` refused by
  name instead of truncated.

`check_domain` and `check_link` are cached in-process for 1 h; a result that carries a failed leg (a
DNS transport error, a partial family, or any TLS failure) is NEVER stored, so a recovered source is
visible on the next call.  A cache hit keeps the ORIGINAL `checked_at` (provenance is never
restamped) and sets `cached: true`.  No result is invented: `archived: null` means "the archive could
not be asked", never "there is no snapshot".

Sync on purpose: the V29 tool runners are sync (`tool_registry._RUNNERS`), and the routes call
these through `starlette.concurrency.run_in_threadpool`.
"""
from __future__ import annotations

import ipaddress
import math
import re
import socket
import ssl
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.core.errors import E_EGRESS_BLOCKED, E_UPSTREAM, E_VALIDATION, PolsiaError
from app.core.timeutil import now_utc

DOH_URL = "https://cloudflare-dns.com/dns-query"
WAYBACK_URL = "https://archive.org/wayback/available"
#: The page the badge/route asks for; a real contact string is kinder to the archive and to DoH.
USER_AGENT = "MYPOLSIA/1.0 (+site and link health check; one request per hour per target)"

#: The named refusal of `resolve_egress` / `pinned_http_get` (CONTRACTS §4.15, frozen).
#: Registered in `app/core/errors.py` (one code, one home) — a bare IP or a name that resolves
#: into private/loopback/link-local/RFC 6598 space, or a non-web port, raises it.

TIMEOUT_S = 8.0
TLS_TIMEOUT_S = 8.0
TLS_PORT = 443
CACHE_TTL_S = 3600.0
CACHE_MAX_ENTRIES = 256
MAX_LINK_CHARS = 2048
MAX_REDIRECTS = 3
ALLOWED_PORTS = (80, 443)
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

#: A bare hostname: at least two labels, no scheme/path/port/userinfo, ASCII letters-digit-dash
#: labels.  An IP literal deliberately does NOT match (`_host` is for domains the badge shows).
HOST_RE = re.compile(r"(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}")

#: The HTTP client factory — the house test seam (V29/L3 `_CLIENT`).  Tests pass a
#: `httpx.MockTransport`; production never notices.
_CLIENT = httpx.Client

#: `key -> (monotonic stamp, result)`.  Process-global by design; cleared by tests.
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


# ── cache ────────────────────────────────────────────────────────────────────────────────────


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _CACHE.get(key)
    if hit is None:
        return None
    if time.monotonic() - hit[0] >= CACHE_TTL_S:
        _CACHE.pop(key, None)
        return None
    # A copy: callers must never be able to mutate a cached result.
    return dict(hit[1])


def _cache_put(key: str, result: dict[str, Any]) -> None:
    _CACHE[key] = (time.monotonic(), dict(result))
    if len(_CACHE) > CACHE_MAX_ENTRIES:
        # FIFO: the oldest insertion goes first.  No TTL scan on every write.
        _CACHE.pop(next(iter(_CACHE)))


# ── validation ───────────────────────────────────────────────────────────────────────────────


def _host(value: Any) -> str:
    """A bare hostname or the named `E_VALIDATION` — no scheme, path, port or userinfo."""
    def refuse(reason: str) -> PolsiaError:
        return PolsiaError(
            E_VALIDATION,
            f"domain_health: {value!r} is not a bare hostname ({reason})",
            how_to_fix='pass {"host": "example.com"} — letters, digits, dots and dashes only',
            context={"field": "host", "host": value if isinstance(value, str) else None,
                     "reason": reason},
        )

    if not isinstance(value, str):
        raise refuse("not a string")
    text = value.strip()
    if not text:
        raise refuse("empty")
    if len(text) > 253:
        raise refuse("longer than 253 characters")
    if "://" in text:
        raise refuse("a scheme is not allowed")
    if "/" in text or "?" in text or "#" in text:
        raise refuse("no path, query or fragment")
    if "@" in text:
        raise refuse("no sign-in part")
    if ":" in text:
        raise refuse("no port")
    name = text.lower()
    if not HOST_RE.fullmatch(name):
        raise refuse("expected labels like example.com")
    return name


def _port_of(parsed: Any) -> int:
    try:
        port = parsed.port
    except ValueError as exc:
        raise PolsiaError(
            E_VALIDATION,
            f"link_status: {parsed.netloc!r} carries an invalid port",
            how_to_fix="use a URL on port 80 or 443",
            context={"field": "url"},
        ) from exc
    if port is None:
        return 443 if parsed.scheme == "https" else 80
    return int(port)


def _link_target(value: Any) -> str:
    """A safe-to-check http(s) URL, or the named `E_VALIDATION`."""
    def refuse(reason: str) -> PolsiaError:
        return PolsiaError(
            E_VALIDATION,
            f"link_status: {value!r} is not a checkable URL ({reason})",
            how_to_fix='pass {"url": "https://example.com/page"} — public http(s) only',
            context={"field": "url", "reason": reason},
        )

    if not isinstance(value, str):
        raise refuse("not a string")
    text = value.strip()
    if not text:
        raise refuse("empty")
    if len(text) > MAX_LINK_CHARS:
        raise refuse(f"longer than {MAX_LINK_CHARS} characters")
    # `urllib.parse` accepts ASCII control characters, but `httpx` refuses to build a request
    # for them (`httpx.InvalidURL`, which is NOT an `httpx.HTTPError` and so escapes every
    # handler below).  Refuse here, BEFORE the DoH lookups: the character can never be sent, so
    # it is the caller's `E_VALIDATION`, not an internal failure.  `strip()` above already
    # removed surrounding whitespace, matching the owner route's own rule.
    for position, char in enumerate(text):
        if ord(char) < 0x20 or ord(char) == 0x7F:
            raise PolsiaError(
                E_VALIDATION,
                "link_status: the url contains a control character that cannot be sent over HTTP",
                how_to_fix=(
                    "remove or percent-encode the character (a URL cannot carry raw control "
                    "characters), then retry"
                ),
                context={
                    "field": "url",
                    "position": position,
                    "codepoint": f"U+{ord(char):04X}",
                },
            )
    try:
        parsed = urlparse(text)
    except ValueError as exc:
        raise refuse("unparseable") from exc
    if parsed.scheme not in ("http", "https"):
        raise refuse("only http:// and https:// are checkable")
    if not parsed.hostname:
        raise refuse("no hostname")
    if parsed.username or parsed.password:
        raise refuse("credentials in a URL are refused")
    port = _port_of(parsed)
    if port not in ALLOWED_PORTS:
        raise PolsiaError(
            E_VALIDATION,
            f"link_status: port {port} on {parsed.hostname} is not allowed — only port 80 or 443 "
            "is checked",
            how_to_fix="check the URL on its public 80/443 address",
            context={"field": "url", "port": port, "host": parsed.hostname},
        )
    return text


# ── the SSRF guard ───────────────────────────────────────────────────────────────────────────

#: RFC 6598 shared address space: neither `is_private` nor `is_reserved`, and not globally
#: routable — it carries Alibaba Cloud's instance-metadata service at 100.100.100.200.  Named
#: here so the refusal says what the range actually is instead of calling it "reserved".
SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


def _address_label(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """The reason an address is not public, or None when it is.  Order matters for the message."""
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_reserved:
        return "reserved"
    if ip.is_private:
        return "private"
    if ip.version == 4 and ip in SHARED_ADDRESS_SPACE:
        return "shared (RFC 6598)"
    if not ip.is_global:
        return "reserved"
    return None


def _public_address(text: str, host: str) -> str:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError as exc:
        raise PolsiaError(
            E_UPSTREAM,
            f"link_status: the DNS answer {text!r} for {host} is not an IP address",
            how_to_fix="retry; a resolver returned a non-address answer",
            context={"field": "url", "host": host},
        ) from exc
    label = _address_label(ip)
    if label is not None:
        raise PolsiaError(
            E_VALIDATION,
            f"link_status: the host {host} is a {label} address ({ip}) — non-public addresses "
            "are refused",
            how_to_fix="check a public URL; loopback, private, link-local, multicast, shared and "
                       "reserved hosts are never fetched",
            context={"field": "url", "host": host, "address": str(ip), "reason": label},
        )
    return str(ip)


def _vetted_addresses(answers: Iterable[str], host: str) -> list[str]:
    """The IP answers in `answers`, every one of them vetted public.

    A non-address answer (a CNAME target, an empty string) is skipped — it is not a destination —
    while ANY non-public IP refuses the whole answer set (`_public_address` raises).
    """
    vetted: list[str] = []
    for text in answers:
        try:
            ipaddress.ip_address(text)
        except ValueError:
            continue  # a CNAME or other non-address answer: not a destination
        vetted.append(_public_address(text, host))
    return vetted


def _resolve_public(host: str, port: int) -> list[str]:
    """Every address `host` resolves to, refusing the whole link if ANY of them is not public.

    Hostnames go through the SAME Cloudflare DoH seam the domain check uses: no OS resolver, no
    second provider, and one place to stub.  An IP literal skips the lookup.
    """
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return [_public_address(host, host)]

    addresses: list[str] = []
    for rrtype in ("A", "AAAA"):
        answer, status, error = _doh_leg(host, rrtype)
        if error is not None:
            raise PolsiaError(
                E_UPSTREAM,
                f"link_status: the DNS {rrtype} lookup for {host} failed: {error}",
                how_to_fix="retry; Cloudflare DoH is temporarily unavailable",
                context={"field": "url", "host": host, "port": port,
                         "leg": f"dns-{rrtype.lower()}"},
            )
        if status == 3:
            raise PolsiaError(
                E_UPSTREAM,
                f"link_status: {host} does not resolve (NXDOMAIN)",
                how_to_fix="check the hostname; nothing answers for it",
                context={"field": "url", "host": host, "port": port, "status": 3},
            )
        if status != 0:
            raise PolsiaError(
                E_UPSTREAM,
                f"link_status: the DNS {rrtype} lookup for {host} answered RCODE {status}",
                how_to_fix="retry; if it persists the name is misconfigured",
                context={"field": "url", "host": host, "port": port, "status": status},
            )
        addresses.extend(_vetted_addresses(answer, host))
    if not addresses:
        raise PolsiaError(
            E_UPSTREAM,
            f"link_status: {host} has no A or AAAA address",
            how_to_fix="check the hostname; it resolves to no address",
            context={"field": "url", "host": host},
        )
    return addresses


def _egress_host(value: Any) -> str:
    """A bare host or IP literal to resolve, or the named `E_EGRESS_BLOCKED` refusal."""
    def refuse(reason: str) -> PolsiaError:
        return PolsiaError(
            E_EGRESS_BLOCKED,
            f"egress: {value!r} is not a host to resolve ({reason})",
            how_to_fix="pass a bare hostname or IP literal — no scheme, path, port or sign-in part",
            context={"host": value if isinstance(value, str) else None, "reason": reason},
        )

    if not isinstance(value, str):
        raise refuse("not a string")
    text = value.strip()
    if not text:
        raise refuse("empty")
    try:
        # An IP literal IS its own destination; the address predicate still vets it below.
        return str(ipaddress.ip_address(text))
    except ValueError:
        pass
    if len(text) > 253:
        raise refuse("longer than 253 characters")
    if "://" in text:
        raise refuse("a scheme is not allowed")
    for char, reason in (("/", "no path"), ("?", "no query"), ("#", "no fragment"),
                         ("@", "no sign-in part"), (":", "no port — pass it as `port`")):
        if char in text:
            raise refuse(reason)
    for char in text:
        if char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F:
            raise refuse("a space or control character cannot be part of a host")
    return text.lower()


def resolve_egress(host: str, port: int) -> list[str]:
    """Every address `host` may be dialled at on `port`, or the named `E_EGRESS_BLOCKED`.

    The ONE egress decision (CONTRACTS §4.15), so `web_fetch` and this module cannot disagree:
    the port must be a web port (`ALLOWED_PORTS`), and every address the name resolves to — via
    Cloudflare DoH, never the OS resolver — must be globally routable.  `is_global` is the
    positive question, and the special ranges are denied FIRST so the refusal can name them and
    so multicast (`is_global` is True for `224.0.0.1`) stays refused (A2A1-01).

    A mixed answer is refused whole: a name that returns one public and one shared address has no
    safe destination.  The returned addresses are what a pinned socket must dial; nothing here
    re-resolves at connect time (F-27), and a refusal is never an empty list (fail closed).
    """
    name = _egress_host(host)
    if port not in ALLOWED_PORTS:
        raise PolsiaError(
            E_EGRESS_BLOCKED,
            f"egress: port {port!r} on {name!r} is not allowed — only port 80 or 443 may be "
            "dialled",
            how_to_fix="use the host's public 80/443 address",
            context={"host": name, "port": port, "allowed_ports": list(ALLOWED_PORTS)},
        )
    try:
        addresses = _resolve_public(name, int(port))
    except PolsiaError as exc:
        # One code for the caller to catch, with the cause kept in `context["refused_by"]`: a
        # private answer and a dead resolver both mean "no vetted address, so nothing is dialled".
        raise PolsiaError(
            E_EGRESS_BLOCKED,
            f"egress: refusing {name!r}:{port} — {exc.message}",
            how_to_fix=exc.how_to_fix,
            context={**exc.context, "host": name, "port": port, "refused_by": exc.code},
        ) from exc
    return list(dict.fromkeys(addresses))


# ── Cloudflare DoH ──────────────────────────────────────────────────────────────────────────


def _doh(host: str, rrtype: str) -> dict[str, Any]:
    """One Cloudflare DoH answer: `{"status": <RCODE>, "answers": [<data>, ...]}`."""
    try:
        with _CLIENT(timeout=TIMEOUT_S, follow_redirects=True) as client:
            response = client.get(
                DOH_URL,
                params={"name": host, "type": rrtype},
                headers={"Accept": "application/dns-json", "User-Agent": USER_AGENT},
            )
    except httpx.TimeoutException as exc:
        raise PolsiaError(
            E_UPSTREAM,
            f"domain_health: the DNS lookup for {host} timed out after {TIMEOUT_S:g}s",
            how_to_fix="retry once; Cloudflare DoH is slow or unreachable",
            context={"host": host, "leg": "dns", "type": rrtype},
        ) from exc
    except httpx.HTTPError as exc:
        raise PolsiaError(
            E_UPSTREAM,
            f"domain_health: the DNS transport failed for {host}: {type(exc).__name__}",
            how_to_fix="check egress to cloudflare-dns.com and retry",
            context={"host": host, "leg": "dns", "type": rrtype},
        ) from exc
    if response.status_code != 200:
        raise PolsiaError(
            E_UPSTREAM,
            f"domain_health: the DNS resolver answered HTTP {response.status_code} for {host}",
            how_to_fix="retry; Cloudflare DoH is temporarily unavailable",
            context={"host": host, "leg": "dns", "status_code": response.status_code},
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise PolsiaError(
            E_UPSTREAM,
            f"domain_health: the DNS resolver returned non-JSON for {host}",
            how_to_fix="retry; the resolver is misbehaving",
            context={"host": host, "leg": "dns"},
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("Status"), int):
        raise PolsiaError(
            E_UPSTREAM,
            f"domain_health: the DNS answer for {host} carried no Status",
            how_to_fix="retry; the resolver answered outside its documented shape",
            context={"host": host, "leg": "dns"},
        )
    answers: list[str] = []
    raw = payload.get("Answer")
    if isinstance(raw, list):
        for record in raw:
            data = record.get("data") if isinstance(record, dict) else None
            if isinstance(data, str) and data:
                answers.append(data)
    return {"status": int(payload["Status"]), "answers": answers}


def _doh_leg(host: str, rrtype: str) -> tuple[list[str], int | None, str | None]:
    """`_doh` with its refusal flattened into a field: (answers, status, error message)."""
    try:
        answer = _doh(host, rrtype)
    except PolsiaError as exc:
        return [], None, exc.message
    if not isinstance(answer, dict):
        return [], None, "the DNS answer was not an object"
    status = answer.get("status")
    if not isinstance(status, int):
        return [], None, "the DNS answer carried no numeric status"
    raw = answer.get("answers")
    answers = [text for text in raw if isinstance(text, str)] if isinstance(raw, list) else []
    return answers, int(status), None


# ── TLS ─────────────────────────────────────────────────────────────────────────────────────


def _read_cert(host: str, address: str) -> dict[str, Any]:
    """The peer certificate of `host:443`, fetched from `address` — the ONLY function that dials.

    `address` is the vetted A/AAAA answer the caller already holds; the socket dials THAT, while
    TLS still verifies the certificate against `host` (`server_hostname`).  Dialling the hostname
    instead let the OS resolver pick an address no guard had seen (F-27): a name whose DoH answer
    is loopback was connected to over port 443 on this host.  `address` is required — there is no
    spelling of this call that dials an unvetted name.
    """
    context = ssl.create_default_context()
    with (
        socket.create_connection((address, TLS_PORT), timeout=TLS_TIMEOUT_S) as raw,
        context.wrap_socket(raw, server_hostname=host) as tls,
    ):
        cert = tls.getpeercert()
    return cert if isinstance(cert, dict) else {}


def _cert_fields(cert: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """`{"ok", "not_after", "days_left", "reason"}` from a peercert dict — pure, testable."""
    not_after = cert.get("notAfter") if isinstance(cert, dict) else None
    if not isinstance(not_after, str) or not not_after:
        return {
            "ok": False,
            "not_after": None,
            "days_left": None,
            "reason": "the peer certificate carried no notAfter field",
        }
    try:
        expiry = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), UTC)
    except (ValueError, OverflowError, OSError):
        return {
            "ok": False,
            "not_after": None,
            "days_left": None,
            "reason": f"unreadable certificate notAfter {not_after!r}",
        }
    remaining = (expiry - (now or now_utc())).total_seconds()
    return {
        "ok": True,
        "not_after": expiry.isoformat(),
        "days_left": math.floor(remaining / 86400.0),
        "reason": None,
    }


def _tls(host: str, address: str) -> dict[str, Any]:
    """The TLS leg, dialled at `address` with `host` as the verified name.

    EVERY failure is a returned field — never an exception.
    """
    try:
        cert = _read_cert(host, address)
    except ssl.SSLCertVerificationError as exc:
        message = getattr(exc, "verify_message", "") or type(exc).__name__
        return {"ok": False, "not_after": None, "days_left": None,
                "reason": f"certificate verification failed: {message}"}
    except ssl.SSLError as exc:
        detail = getattr(exc, "reason", None) or type(exc).__name__
        return {"ok": False, "not_after": None, "days_left": None,
                "reason": f"TLS handshake failed: {detail}"}
    except TimeoutError:
        return {"ok": False, "not_after": None, "days_left": None,
                "reason": f"the TLS handshake timed out after {TLS_TIMEOUT_S:g}s"}
    except OSError as exc:
        return {"ok": False, "not_after": None, "days_left": None,
                "reason": f"the TLS connection failed: {type(exc).__name__}"}
    return _cert_fields(cert)


def _tls_unreachable(reason: str) -> dict[str, Any]:
    """The TLS field when there is no address to dial — stated, not left blank."""
    return {"ok": False, "not_after": None, "days_left": None, "reason": reason}


# ── check_domain ────────────────────────────────────────────────────────────────────────────


def check_domain(host: str) -> dict[str, Any]:
    """DNS (A/AAAA) + TLS health for one bare hostname.  Cached 1 h; failures are fields."""
    name = _host(host)
    key = f"domain:{name}"
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "cached": True}

    a, a_status, a_error = _doh_leg(name, "A")
    aaaa: list[str] = []
    aaaa_status: int | None = None
    aaaa_error: str | None = None
    if a_error is None and a_status == 0:
        # Only a NOERROR A answer is worth an AAAA query; NXDOMAIN/SERVFAIL answer for both.
        aaaa, aaaa_status, aaaa_error = _doh_leg(name, "AAAA")

    dns_status = a_status if a_error is None else None
    dns_reason: str | None = None
    reason: str | None = None
    if a_error is not None:
        dns_reason = a_error
        reason = a_error
    elif a_status == 3:
        dns_reason = "NXDOMAIN"
        reason = f"{name} does not resolve (NXDOMAIN)"
    elif a_status != 0:
        dns_reason = f"DNS RCODE {a_status}"
        reason = f"the DNS resolver answered RCODE {a_status} for {name}"
    elif aaaa_error is not None:
        dns_reason = f"AAAA: {aaaa_error}"
    elif aaaa_status not in (None, 0):
        dns_reason = f"AAAA RCODE {aaaa_status}"

    has_address = bool(a or aaaa)
    if a_error is not None:
        tls = _tls_unreachable("no address to connect to (the DNS lookup failed)")
    elif a_status != 0:
        tls = _tls_unreachable(
            "no address to connect to (NXDOMAIN)" if a_status == 3
            else f"no address to connect to (RCODE {a_status})"
        )
    elif not has_address:
        tls = _tls_unreachable("no address to connect to (the name has no A/AAAA answer)")
    else:
        try:
            # The answers are vetted with the module's own predicate BEFORE any socket exists, and
            # the socket then dials one of THOSE addresses — never the name (F-27).
            vetted = _vetted_addresses((*a, *aaaa), name)
        except PolsiaError as exc:
            # The contract of this module: a refusal is a FIELD, never an exception.  Nothing is
            # dialled — a private answer is not "unreachable", it is out of policy.
            tls = _tls_unreachable(f"refused: {exc.message}")
        else:
            tls = (
                _tls(name, vetted[0]) if vetted
                else _tls_unreachable("no address to connect to (the name has no A/AAAA answer)")
            )

    if reason is None and tls["ok"] is not True:
        reason = tls["reason"]

    result = {
        "host": name,
        "dns": {"a": a, "aaaa": aaaa, "status": dns_status, "reason": dns_reason},
        "tls": tls,
        "reason": reason,
        "checked_at": now_utc().isoformat(),
        "cached": False,
    }
    # A FAILURE IS NEVER A CACHED ANSWER.  `_doh_leg` flattens a resolver failure into a field
    # (instead of raising, so the route still serves), and `_tls` does the same for a handshake
    # failure/timeout; caching either would pin an outage for the full hour and hide a recovered
    # source.  So only a fully clean measurement is stored: no DNS reason (a failed or partial
    # leg, or NXDOMAIN, is re-measured next call) and a positive TLS reading.  A `tls.ok=false`
    # result cannot say whether it was a certificate fact or a transport failure, and guessing
    # wrong in the cache is the more expensive mistake.
    if dns_reason is None and tls["ok"] is True:
        _cache_put(key, result)
    return result


# ── pinned HTTP (CONTRACTS §4.15) ───────────────────────────────────────────────────────────


def _host_header(host: str, port: int, scheme: str) -> str:
    """The `Host` value for a pinned request: the NAME, with the port only when not default."""
    literal = f"[{host}]" if ":" in host else host
    default = 443 if scheme == "https" else 80
    return literal if port == default else f"{literal}:{port}"


def _sni_extension(scheme: str, host: str) -> dict[str, Any]:
    """httpcore verifies TLS against `sni_hostname` (or the URL host) — keep the NAME."""
    return {"sni_hostname": host} if scheme == "https" else {}


def _pinned_headers(
    headers: dict[str, str] | None, host: str, port: int, scheme: str
) -> dict[str, str]:
    """The caller's headers plus OUR `Host`: the pin owns that field, so a caller may not set it.

    A caller-supplied `Host` that disagreed with the pin would send one name on the wire while the
    certificate was verified against another, so it is refused by name rather than silently
    replaced.
    """
    if headers is not None and any(key.lower() == "host" for key in headers):
        raise PolsiaError(
            E_VALIDATION,
            "pinned_http_get: the Host header is derived from the url and may not be passed",
            how_to_fix="drop the Host header from `headers`; the pin sets it from the url",
            context={"field": "headers", "header": "Host"},
        )
    merged = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        merged.update(headers)
    merged["Host"] = _host_header(host, port, scheme)
    return merged


def _pinned_url(url: str, address: str) -> httpx.URL:
    """`url` with its HOST replaced by a vetted `address`; path, query and port untouched.

    The request therefore dials `address` (httpcore connects to `request.url.host`) while the
    `Host` header and `sni_hostname` still name the host, so a virtual host answers as itself and
    a second DNS resolution never happens at connect time (F-27).
    """
    try:
        return httpx.URL(url).copy_with(host=address)
    except (httpx.InvalidURL, TypeError, ValueError) as exc:
        # Defense in depth behind `_link_target`: httpx builds its own URL object and refuses
        # characters `urllib.parse` accepted, and `InvalidURL` is not an `httpx.HTTPError`.
        raise PolsiaError(
            E_VALIDATION,
            f"link_status: {url!r} cannot be sent over HTTP ({exc})",
            how_to_fix="remove the unsendable characters (percent-encode them) and retry",
            context={"field": "url", "leg": "http"},
        ) from exc


def _read_bounded(response: httpx.Response, *, max_bytes: int, url: str) -> bytes:
    """The body, refusing the moment it passes `max_bytes` — a refusal, never a truncation."""
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise PolsiaError(
                E_UPSTREAM,
                f"pinned_http_get: the response from {url} exceeded max_bytes={max_bytes} "
                f"({total} bytes read) — refused, not truncated",
                how_to_fix=(
                    f"raise max_bytes above {total} to accept this body, or fetch a lighter URL"
                ),
                context={"url": url, "reason": "body_exceeds_max_bytes",
                         "max_bytes": max_bytes, "bytes_read": total},
            )
        chunks.append(chunk)
    return b"".join(chunks)


def pinned_http_get(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """GET `url` over a socket pinned to an address `resolve_egress` vetted (CONTRACTS §4.15).

    The name is resolved ONCE (`resolve_egress`, DoH + `is_global` + the port allowlist) and the
    request then dials one of those addresses — `Host` and TLS SNI still name the host, so the
    certificate is verified against it.  Redirects are NOT followed: the caller re-checks every
    hop (`web_fetch` re-passes the floor per hop).  A body past `max_bytes` is a named refusal,
    never a truncated answer.  A refused connection on one vetted address moves to the next one
    in the same answer — still without resolving anything again.
    """
    if not isinstance(url, str) or not url.strip():
        raise PolsiaError(
            E_VALIDATION,
            "pinned_http_get: a non-empty url is required",
            how_to_fix='pass {"url": "https://..."}',
            context={"field": "url", "reason": "empty or not a string"},
        )
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise PolsiaError(
            E_VALIDATION,
            f"pinned_http_get: max_bytes must be a positive integer (got {max_bytes!r})",
            how_to_fix="pass a positive byte bound",
            context={"field": "max_bytes", "max_bytes": max_bytes},
        )
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise PolsiaError(
            E_VALIDATION,
            f"pinned_http_get: timeout must be a positive number of seconds (got {timeout!r})",
            how_to_fix="pass a positive timeout in seconds",
            context={"field": "timeout", "timeout": timeout},
        )
    target = url.strip()
    try:
        parsed = urlparse(target)
    except ValueError as exc:
        raise PolsiaError(
            E_VALIDATION,
            f"pinned_http_get: {target!r} is not a URL ({exc})",
            how_to_fix="fix the URL shape (scheme, host, port)",
            context={"field": "url", "leg": "http"},
        ) from exc
    if parsed.scheme not in ("http", "https"):
        raise PolsiaError(
            E_VALIDATION,
            f"pinned_http_get: {target!r} is not http(s) — only http:// and https:// are fetched",
            how_to_fix="use an absolute http:// or https:// URL",
            context={"field": "url", "leg": "http"},
        )
    if not parsed.hostname:
        raise PolsiaError(
            E_VALIDATION,
            f"pinned_http_get: {target!r} has no host to fetch",
            how_to_fix="pass an absolute http(s) URL",
            context={"field": "url", "leg": "http"},
        )
    if parsed.username or parsed.password:
        raise PolsiaError(
            E_VALIDATION,
            "pinned_http_get: credentials in a URL are refused",
            how_to_fix="move the credentials to the integration's own configuration",
            context={"field": "url", "leg": "http"},
        )
    host = parsed.hostname
    port = _port_of(parsed)
    request_headers = _pinned_headers(headers, host, port, parsed.scheme)
    addresses = resolve_egress(host, port)
    extensions = _sni_extension(parsed.scheme, host)
    failures: list[str] = []
    with _CLIENT(timeout=float(timeout), follow_redirects=False) as client:
        for address in addresses:
            pinned = _pinned_url(target, address)
            try:
                with client.stream("GET", pinned, headers=request_headers,
                                   extensions=extensions) as response:
                    body = _read_bounded(response, max_bytes=max_bytes, url=target)
                    return response.status_code, dict(response.headers), body
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # A connection the address refused says nothing about the others in the answer.
                failures.append(f"{address}: {type(exc).__name__}")
                continue
            except httpx.TimeoutException as exc:
                raise PolsiaError(
                    E_UPSTREAM,
                    f"pinned_http_get: {target} timed out after {float(timeout):g}s",
                    how_to_fix="retry once; the host is slow or unreachable",
                    context={"url": target, "leg": "http", "address": address},
                ) from exc
            except httpx.HTTPError as exc:
                raise PolsiaError(
                    E_UPSTREAM,
                    f"pinned_http_get: the request to {target} failed: {type(exc).__name__}",
                    how_to_fix="check the URL and retry; the host may refuse this client",
                    context={"url": target, "leg": "http", "address": address},
                ) from exc
    raise PolsiaError(
        E_UPSTREAM,
        f"pinned_http_get: no vetted address for {host} accepted a connection "
        f"({'; '.join(failures)})",
        how_to_fix="retry; the host may be down, or every vetted address refused the connection",
        context={"url": target, "host": host, "addresses": addresses, "failures": failures},
    )


# ── check_link ──────────────────────────────────────────────────────────────────────────────


def _fetch_headers(url: str, address: str) -> tuple[int, str | None]:
    """HEAD (GET on `405`) dialling `address`; returns (status, Location).  Body is never read.

    `address` is the vetted answer `_walk` already holds — the same pin as `pinned_http_get`, so
    the guard's answer and the socket's destination cannot differ (F-27).
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    headers = _pinned_headers({"Accept": "*/*"}, host, _port_of(parsed), parsed.scheme)
    pinned = _pinned_url(url, address)
    extensions = _sni_extension(parsed.scheme, host)
    try:
        with _CLIENT(timeout=TIMEOUT_S, follow_redirects=False) as client:
            status, location = _request_headers(client, "HEAD", pinned, headers, extensions)
            if status == 405:
                # Some servers refuse HEAD outright; the GET is the same resource, not a retry.
                status, location = _request_headers(client, "GET", pinned, headers, extensions)
    except httpx.TimeoutException as exc:
        raise PolsiaError(
            E_UPSTREAM,
            f"link_status: {url} timed out after {TIMEOUT_S:g}s",
            how_to_fix="retry once; the host is slow or unreachable",
            context={"url": url, "leg": "http", "address": address},
        ) from exc
    except httpx.HTTPError as exc:
        raise PolsiaError(
            E_UPSTREAM,
            f"link_status: the HTTP request to {url} failed: {type(exc).__name__}",
            how_to_fix="check the URL and retry; the host may refuse this client",
            context={"url": url, "leg": "http", "address": address},
        ) from exc
    return status, location


def _request_headers(
    client: httpx.Client,
    method: str,
    url: httpx.URL,
    headers: dict[str, str],
    extensions: dict[str, Any],
) -> tuple[int, str | None]:
    with client.stream(method, url, headers=headers, extensions=extensions) as response:
        return response.status_code, response.headers.get("location")


def _walk(target: str) -> tuple[int, str, list[str], str | None]:
    """Follow at most `MAX_REDIRECTS` manual redirects, revalidating each hop.

    Returns (status, final_url, visited, reason).  A hop that leaves http(s), changes to a
    non-80/443 port or lands on a non-public address is a named `E_VALIDATION` — the guard exists
    so a public URL cannot be used to reach the metadata service.
    """
    current = target
    visited: list[str] = []
    for _hop in range(MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        # The answers are the destination: `_fetch_headers` dials one of them, so the socket can
        # never reach an address this guard did not vet (F-27).
        addresses = _resolve_public(parsed.hostname or "", _port_of(parsed))
        status, location = _fetch_headers(current, addresses[0])
        if status not in REDIRECT_CODES or not location:
            return status, current, visited, None
        next_url = _link_target(urljoin(current, location))
        if next_url in visited or next_url == current:
            return status, current, visited, f"redirect loop at {next_url}"
        visited.append(current)
        if len(visited) > MAX_REDIRECTS:
            return status, current, visited, (
                f"more than {MAX_REDIRECTS} redirects — stopped at {current}"
            )
        current = next_url
    # Unreachable: the loop returns on the last iteration, but never fall off the end silently.
    return 0, current, visited, f"more than {MAX_REDIRECTS} redirects"


def _wayback(url: str) -> dict[str, Any]:
    """The closest snapshot of `url`.  An empty `archived_snapshots` is `available: false`."""
    try:
        with _CLIENT(timeout=TIMEOUT_S, follow_redirects=True) as client:
            response = client.get(
                WAYBACK_URL,
                params={"url": url},
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
    except httpx.TimeoutException as exc:
        raise PolsiaError(
            E_UPSTREAM,
            f"link_status: the Wayback API timed out after {TIMEOUT_S:g}s",
            how_to_fix="retry once; archive.org is slow",
            context={"url": url, "leg": "wayback"},
        ) from exc
    except httpx.HTTPError as exc:
        raise PolsiaError(
            E_UPSTREAM,
            f"link_status: the Wayback transport failed: {type(exc).__name__}",
            how_to_fix="retry once; archive.org may be unreachable",
            context={"url": url, "leg": "wayback"},
        ) from exc
    if response.status_code != 200:
        raise PolsiaError(
            E_UPSTREAM,
            f"link_status: the Wayback API answered HTTP {response.status_code}",
            how_to_fix="retry later; the live status above is unaffected",
            context={"url": url, "leg": "wayback", "status_code": response.status_code},
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise PolsiaError(
            E_UPSTREAM,
            "link_status: the Wayback API returned non-JSON",
            how_to_fix="retry later",
            context={"url": url, "leg": "wayback"},
        ) from exc
    snapshots = payload.get("archived_snapshots") if isinstance(payload, dict) else None
    closest = snapshots.get("closest") if isinstance(snapshots, dict) else None
    if not isinstance(closest, dict):
        return {"available": False, "url": None, "timestamp": None}
    snapshot_url = closest.get("url") if isinstance(closest.get("url"), str) else None
    timestamp = closest.get("timestamp") if isinstance(closest.get("timestamp"), str) else None
    available = closest.get("available") is True and snapshot_url is not None
    return {
        "available": available,
        "url": snapshot_url if available else None,
        "timestamp": timestamp if available else None,
    }


def check_link(url: str) -> dict[str, Any]:
    """The live status of one public URL plus its Wayback snapshot.  Cached 1 h."""
    target = _link_target(url)
    key = f"link:{target}"
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "cached": True}

    status, final_url, visited, reason = _walk(target)
    try:
        archived: dict[str, Any] | None = _wayback(target)
        archive_reason: str | None = None
    except PolsiaError as exc:
        # The archive is a SECOND opinion about a dead link.  Its outage must not falsify the live
        # status, so it degrades to "not measured" — never to a fabricated "not archived".
        archived = None
        archive_reason = exc.message
    result = {
        "url": target,
        "status": status,
        "final_url": final_url,
        "ok": reason is None and 200 <= status < 300,
        "archived": archived,
        "archive_reason": archive_reason,
        "redirects": visited,
        "reason": reason,
        "checked_at": now_utc().isoformat(),
        "cached": False,
    }
    _cache_put(key, result)
    return result


__all__ = [
    "check_domain",
    "check_link",
    "pinned_http_get",
    "resolve_egress",
    "CACHE_TTL_S",
    "DOH_URL",
    "E_EGRESS_BLOCKED",
    "WAYBACK_URL",
]
