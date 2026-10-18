#!/usr/bin/env python3
"""`scripts/phone_send_driver.py` — the CDP driver behind `POST /api/v1/phone/send`.

WHAT THIS IS
    A standalone, stdlib-only program that talks the Chrome DevTools Protocol to a Chrome started
    with `--remote-debugging-port=<port> --user-data-dir=<a DEDICATED automation dir>` and, when the
    owner's Google Messages web session is actually paired, sends ONE SMS through that page.

WHAT THIS IS NOT
    It is not the policy gate. `services/phone_send_service.py` owns the sandbox refusal and the
    body validation; this driver is only reached after both passed. It is also not a mock: there is
    no "pretend send" branch anywhere in this file. Every success it prints was read back out of the
    live page (the composer emptied AND the outgoing bubble carries the text). If that read-back
    cannot be done, it refuses and says so — it never reports a send it did not observe.

INVARIANTS (the safety of this lane lives here)
    * `--check` is READ-ONLY: it lists tabs, evaluates a DOM snapshot and prints JSON. It never
      clicks, types, or navigates.
    * `--send` requires BOTH `--to` and `--body-file`, and a paired live session. There is no code
      path from `--check`, from a fixture, or from a missing argument to a click on Send.
    * `--fixture` mode (used by the test suite) parses a local HTML file and is refused for `--send`
      before any browser code is imported. The HTML fixture is a TEST FIXTURE, never product data.
    * The driver NEVER touches the owner's live Chrome profile: the profile dir is the launcher's
      responsibility and the default port is 9223, not Chrome's 9222 (the profile-debug default).
    * The message body is read from a FILE (argv is world-readable in a process list) and is never
      logged. Numbers are printed masked.

PROTOCOL
    stdout is exactly one JSON object, always:
        {"ok": true,  "data": {...}}
        {"ok": false, "error": {"code", "message", "how_to_fix", "context": {}}}
    exit 0 = ok; 2 = usage; 3 = unpaired; 4 = unavailable (CDP/DOM/read-back); 5 = validation
    (target not found among the open threads, bad fixture, body file unreadable).

CDP CLIENT
    Chrome's websocket endpoint is driven with a ~90-line RFC 6455 client built on `socket` — this
    keeps the deployment dependency-free (`websockets` is not a declared backend requirement) and
    the frame handling small enough to audit.
"""
from __future__ import annotations

# ruff: noqa: UP031  — the JS templates below are percent-substituted on purpose: they contain
# `{...}` object literals, so `str.format`/f-strings cannot carry them.
import base64
import hashlib
import json
import os
import re
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar

DRIVER_NAME = "cdp-google-messages"
DEFAULT_PORT = 9223
DEFAULT_TIMEOUT_S = 30.0
MESSAGES_PREFIX = "https://messages.google.com/web"
#: Paths that mean "this Chrome has the site open but the session is not paired yet" — observed
#: live on 2026-10-18: an unpaired automation profile lands on `/web/welcome`.  Checked before the
#: DOM so the reason names the real state instead of "no conversation list".
WELCOME_MARKERS = ("/web/welcome", "/web/pair")
CDP_HOST = "127.0.0.1"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNPAIRED = 3
EXIT_UNAVAILABLE = 4
EXIT_VALIDATION = 5

#: Roles -> ordered CSS candidates.  The FIRST selector that matches wins, in both the live JS
#: snapshot and the offline `FixtureDom`, so the fixture test pins the exact strings production uses.
#: House rule: no whitespace inside `[...]` (the offline matcher splits descendant combinators on
#: spaces); keep attribute values single-token or hyphenated.
SELECTORS: dict[str, list[str]] = {
    "thread_list": ["mw-conversation-list", "mws-conversation-list", "[aria-label*=\"Conversations\"]"],
    "thread_item": ["mw-thread-item", "a[role=\"listitem\"]", "[role=\"listitem\"]"],
    "thread_name": [".name", ".thread-name", "[data-e2e-thread-name]"],
    "thread_number": [".number", ".snippet-number", "[data-e2e-thread-number]"],
    "pairing": ["mw-pairing", "mw-qr-code", "[data-e2e=\"pairing-screen\"]"],
    "composer": [
        "div[contenteditable=\"true\"][role=\"textbox\"]",
        "mw-message-input div[contenteditable=\"true\"]",
        "textarea[aria-label*=\"message\" i]",
    ],
    "send_button": [
        "button[aria-label=\"Send message\"]",
        "mw-send-button button",
        "button[data-e2e=\"send\"]",
    ],
    "header_title": ["mw-conversation-header .name", "#conversation-header .name", "header .name"],
    "outgoing": ["mw-message-bubble.outgoing", ".outgoing mws-message-wrapper", "[data-e2e=\"outgoing-bubble\"]"],
}

REQUIRED_ROLES = tuple(SELECTORS)


# ── envelope ───────────────────────────────────────────────────────────────────────────────────


class UsageError(Exception):
    """A malformed invocation — answered as E_VALIDATION with exit 2."""


class DriverRefusal(Exception):
    def __init__(self, code: str, message: str, how_to_fix: str = "", context: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.how_to_fix = how_to_fix
        self.context = context or {}

    @property
    def exit_code(self) -> int:
        return {
            "E_PHONE_SEND_UNPAIRED": EXIT_UNPAIRED,
            "E_PHONE_UNAVAILABLE": EXIT_UNAVAILABLE,
            "E_VALIDATION": EXIT_VALIDATION,
        }.get(self.code, EXIT_UNAVAILABLE)


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def ok_envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": data}


def error_envelope(code: str, message: str, how_to_fix: str = "", **context: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": code, "message": message, "how_to_fix": how_to_fix, "context": context},
    }


def unavailable(message: str, how_to_fix: str = "", **context: Any) -> DriverRefusal:
    return DriverRefusal("E_PHONE_UNAVAILABLE", message, how_to_fix, context)


def unpaired(message: str, how_to_fix: str = "", **context: Any) -> DriverRefusal:
    return DriverRefusal("E_PHONE_SEND_UNPAIRED", message, how_to_fix, context)


def invalid(message: str, how_to_fix: str = "", **context: Any) -> DriverRefusal:
    return DriverRefusal("E_VALIDATION", message, how_to_fix, context)


# ── the JS the live page is asked (built from SELECTORS — one source of truth) ────────────────

_SELECTOR_JS = json.dumps(SELECTORS, ensure_ascii=False)

_HELPERS_JS = """
  const S = %s;
  const pick = (list) => { for (const sel of list) { const el = document.querySelector(sel); if (el) return el; } return null; };
  const pickAll = (list) => { for (const sel of list) { const els = document.querySelectorAll(sel); if (els.length) return Array.from(els); } return []; };
  const pickIn = (root, list) => { if (!root) return null; for (const sel of list) { const el = root.querySelector(sel); if (el) return el; } return null; };
  const txt = (el) => (el ? (el.textContent || "").trim() : "");
""" % _SELECTOR_JS

SNAPSHOT_JS = """(() => {
%s
  const items = pickAll(S.thread_item);
  const list = pick(S.thread_list);
  const pairing = pick(S.pairing);
  const threads = items.map((item, index) => ({
    index,
    thread_id: item.getAttribute("data-thread-id") || null,
    name: txt(pickIn(item, S.thread_name)),
    number: txt(pickIn(item, S.thread_number)) || item.getAttribute("data-number") || null,
  }));
  return {
    url: location.href,
    title: document.title,
    pairing_visible: !!pairing,
    thread_list_present: !!list,
    thread_count: threads.length,
    threads,
    composer_present: !!pick(S.composer),
    send_button_present: !!pick(S.send_button),
    header: txt(pick(S.header_title)) || null,
  };
})()""" % _HELPERS_JS


def open_thread_js(index: int) -> str:
    return """(function (index) {
%s
  const items = pickAll(S.thread_item);
  if (!items[index]) return { opened: false, reason: "no thread at index " + index, count: items.length };
  items[index].click();
  return { opened: true, count: items.length };
})(%d)""" % (_HELPERS_JS, int(index))


COMPOSER_JS = """(() => {
%s
  const box = pick(S.composer);
  if (!box) return { focused: false, reason: "no composer" };
  box.focus();
  const active = document.activeElement === box || (box.contains && box.contains(document.activeElement));
  return { focused: active, text: (box.value !== undefined ? box.value : (box.textContent || "")) };
})()""" % _HELPERS_JS

COMPOSER_TEXT_JS = """(() => {
%s
  const box = pick(S.composer);
  if (!box) return null;
  return box.value !== undefined ? box.value : (box.textContent || "");
})()""" % _HELPERS_JS

CLICK_SEND_JS = """(() => {
%s
  const button = pick(S.send_button);
  if (!button) return { clicked: false, reason: "no send button" };
  if (button.disabled) return { clicked: false, reason: "send button disabled" };
  button.click();
  return { clicked: true };
})()""" % _HELPERS_JS

LAST_OUTGOING_JS = """(() => {
%s
  const bubbles = pickAll(S.outgoing);
  if (!bubbles.length) return null;
  return txt(bubbles[bubbles.length - 1]);
})()""" % _HELPERS_JS

SEND_BUTTON_STATE_JS = """(() => {
%s
  const button = pick(S.send_button);
  return button ? { present: true, disabled: !!button.disabled } : { present: false, disabled: true };
})()""" % _HELPERS_JS

JS_EXPRESSIONS = {
    "snapshot": SNAPSHOT_JS,
    "composer": COMPOSER_JS,
    "composer_text": COMPOSER_TEXT_JS,
    "send": CLICK_SEND_JS,
    "outgoing": LAST_OUTGOING_JS,
    "send_button": SEND_BUTTON_STATE_JS,
}


# ── offline DOM (fixture) ────────────────────────────────────────────────────────────────────


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    text_parts: list[str] = field(default_factory=list)
    children: list[Node] = field(default_factory=list)
    parent: Node | None = None

    def attr(self, name: str) -> str | None:
        return self.attrs.get(name)


class _TreeBuilder(HTMLParser):
    VOID: ClassVar[set[str]] = {"br", "hr", "img", "input", "meta", "link", "source", "track", "area", "base", "col"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data.strip():
            self.stack[-1].text_parts.append(data)


_ATTR_TOKEN = re.compile(r"^([a-zA-Z_][\w:-]*)(?:([*^$]?=)(\"([^\"]*)\"|'([^']*)'))?$")


def _split_selector(selector: str) -> list[str]:
    """Split a selector on descendant combinators, ignoring spaces inside `[...]`/quotes.

    The one selector that needs this is `textarea[aria-label*="message" i]` — a CSS attribute
    flag contains a space that is NOT a combinator.
    """
    parts: list[str] = []
    current = ""
    depth = 0
    quote = ""
    for char in selector:
        if quote:
            current += char
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            current += char
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(0, depth - 1)
        if char.isspace() and depth == 0:
            if current:
                parts.append(current)
                current = ""
            continue
        current += char
    if current:
        parts.append(current)
    return parts


def _parse_compound(token: str) -> list[tuple[str, Any]]:
    """Parse one compound selector (`tag`, `.cls`, `#id`, `[attr]`, `[attr="v"]`, `[attr*="v" i]`)."""
    tests: list[tuple[str, Any]] = []
    i = 0
    if token and token[0] not in ".#[":
        match = re.match(r"[a-zA-Z][\w-]*", token)
        if not match:
            raise ValueError(f"unparsable selector: {token!r}")
        tests.append(("tag", match.group(0).lower()))
        i = match.end()
    while i < len(token):
        ch = token[i]
        if ch == ".":
            match = re.match(r"\.([\w-]+)", token[i:])
            if not match:
                raise ValueError(f"unparsable class selector: {token!r}")
            tests.append(("class", match.group(1)))
            i += match.end()
        elif ch == "#":
            match = re.match(r"#([\w-]+)", token[i:])
            if not match:
                raise ValueError(f"unparsable id selector: {token!r}")
            tests.append(("id", match.group(1)))
            i += match.end()
        elif ch == "[":
            end = token.find("]", i)
            if end < 0:
                raise ValueError(f"unterminated attribute selector: {token!r}")
            inner = token[i + 1 : end].strip()
            flag = ""
            if inner.endswith(" i"):
                inner, flag = inner[:-2].strip(), "i"
            match = _ATTR_TOKEN.match(inner)
            if not match:
                raise ValueError(f"unparsable attribute selector: {token!r}")
            name, op, _raw, double, single = match.group(1), match.group(2), match.group(3), match.group(4), match.group(5)
            value = double if double is not None else single
            tests.append(("attr", (name.lower(), op or "", value, flag)))
            i = end + 1
        else:
            raise ValueError(f"unparsable selector: {token!r}")
    return tests


def _matches(node: Node, tests: list[tuple[str, Any]]) -> bool:
    for kind, value in tests:
        if kind == "tag":
            if node.tag != value:
                return False
        elif kind == "class":
            if value not in (node.attr("class") or "").split():
                return False
        elif kind == "id":
            if node.attr("id") != value:
                return False
        else:
            name, op, expected, flag = value
            actual = node.attr(name)
            if actual is None:
                return False
            if op == "":
                continue
            lhs, rhs = actual, expected or ""
            if flag == "i":
                lhs, rhs = lhs.lower(), rhs.lower()
            if op == "=" and lhs != rhs:
                return False
            if op == "*=" and rhs not in lhs:
                return False
            if op == "^=" and not lhs.startswith(rhs):
                return False
            if op == "$=" and not lhs.endswith(rhs):
                return False
    return True


class FixtureDom:
    """A read-only DOM over a saved HTML file — enough CSS for `SELECTORS`, nothing more.

    Used by `--check --fixture`, i.e. by the test suite. It performs no I/O beyond parsing the file
    it was handed and it cannot click or type: `--send` refuses in fixture mode before this class is
    even reached (see `main`).
    """

    def __init__(self, html: str) -> None:
        builder = _TreeBuilder()
        builder.feed(html)
        builder.close()
        self.root = builder.root
        self._all = list(self._walk(self.root))

    def _walk(self, node: Node) -> Iterable[Node]:
        for child in node.children:
            yield child
            yield from self._walk(child)

    def _match_chain(self, node: Node, chain: list[list[tuple[str, Any]]]) -> bool:
        if not _matches(node, chain[-1]):
            return False
        ancestor = node.parent
        for compound in reversed(chain[:-1]):
            while ancestor is not None and not _matches(ancestor, compound):
                ancestor = ancestor.parent
            if ancestor is None:
                return False
            ancestor = ancestor.parent
        return True

    def query_all(self, selector: str) -> list[Node]:
        chain = [_parse_compound(part) for part in _split_selector(selector)]
        return [node for node in self._all if self._match_chain(node, chain)]

    def query(self, selector: str) -> Node | None:
        found = self.query_all(selector)
        return found[0] if found else None

    def pick(self, role: str) -> Node | None:
        for selector in SELECTORS[role]:
            found = self.query(selector)
            if found is not None:
                return found
        return None

    def pick_all(self, role: str) -> list[Node]:
        for selector in SELECTORS[role]:
            found = self.query_all(selector)
            if found:
                return found
        return []

    def pick_in(self, root: Node | None, role: str) -> Node | None:
        if root is None:
            return None
        for selector in SELECTORS[role]:
            chain = [_parse_compound(part) for part in _split_selector(selector)]
            for node in self._walk(root):
                if self._match_chain(node, chain) and self._within(node, root):
                    return node
        return None

    def _within(self, node: Node, root: Node) -> bool:
        parent = node.parent
        while parent is not None:
            if parent is root:
                return True
            parent = parent.parent
        return False

    def text(self, node: Node | None) -> str:
        if node is None:
            return ""
        parts = list(node.text_parts)
        for child in node.children:
            parts.append(self.text(child))
        return "".join(parts).strip()


def fixture_snapshot(path: Path) -> dict[str, Any]:
    """The same snapshot shape the live `SNAPSHOT_JS` returns, read from a saved HTML file."""
    try:
        html = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise invalid(
            f"the fixture {path} could not be read ({type(exc).__name__}: {exc})",
            how_to_fix="pass a readable saved-page HTML file to --fixture",
        ) from exc
    dom = FixtureDom(html)
    items = dom.pick_all("thread_item")
    threads = [
        {
            "index": index,
            "thread_id": item.attr("data-thread-id"),
            "name": dom.text(dom.pick_in(item, "thread_name")),
            "number": dom.text(dom.pick_in(item, "thread_number")) or item.attr("data-number"),
        }
        for index, item in enumerate(items)
    ]
    return {
        "url": None,
        "title": None,
        "pairing_visible": dom.pick("pairing") is not None,
        "thread_list_present": dom.pick("thread_list") is not None,
        "thread_count": len(threads),
        "threads": threads,
        "composer_present": dom.pick("composer") is not None,
        "send_button_present": dom.pick("send_button") is not None,
        "header": dom.text(dom.pick("header_title")) or None,
    }


# ── pure decisions ──────────────────────────────────────────────────────────────────────────


def is_paired(snapshot: dict[str, Any], *, require_url: bool) -> bool:
    """`paired` means: a live messages.google.com/web page, no pairing screen, a real thread list."""
    url = snapshot.get("url") or ""
    if require_url and any(marker in url for marker in WELCOME_MARKERS):
        return False
    if snapshot.get("pairing_visible"):
        return False
    if not snapshot.get("thread_list_present"):
        return False
    if require_url:
        return url.startswith(MESSAGES_PREFIX)
    return True


def digits_of(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def mask_number(value: Any) -> str:
    """Keep the country prefix and the last four digits; everything else becomes a bullet."""
    raw = "" if value is None else str(value)
    digits = digits_of(raw)
    if not digits:
        return ""
    keep = digits[-4:] if len(digits) > 4 else digits
    prefix = "+" if raw.strip().startswith("+") else ""
    return prefix + "•" * max(0, len(digits) - len(keep)) + keep


def mask_name(value: Any) -> str:
    return re.sub(r"\d", "•", str(value or "")).strip()


def resolve_thread(threads: list[dict[str, Any]], target: Any) -> dict[str, Any] | None:
    """Find the ONE open thread a `to` names: exact thread_id first, then an E.164 suffix match.

    Returns None for "no such thread" AND for "ambiguous" — the caller refuses with E_VALIDATION
    rather than guessing which person to text. The driver never starts a new conversation: a
    number with no open thread is outside what this build can send to.
    """
    raw = "" if target is None else str(target).strip()
    if not raw:
        return None
    wanted = digits_of(raw)
    if not wanted:
        return None
    for thread in threads:
        if str(thread.get("thread_id") or "").strip() == raw:
            return thread
    if len(wanted) < 8:
        return None
    matches = []
    for thread in threads:
        number = digits_of(thread.get("number"))
        if len(number) < 8:
            continue
        if number.endswith(wanted) or wanted.endswith(number):
            matches.append(thread)
    return matches[0] if len(matches) == 1 else None


def sender_masked(thread: dict[str, Any]) -> str:
    masked = mask_number(thread.get("number"))
    if masked:
        return masked
    return mask_name(thread.get("name")) or "unknown"


def validate_selector_map() -> None:
    for role in REQUIRED_ROLES:
        selectors = SELECTORS[role]
        if not selectors:
            raise ValueError(f"the {role!r} role has no selectors")
        for css in selectors:
            if not isinstance(css, str) or not css.strip():
                raise ValueError(f"the {role!r} role carries an empty selector")
            if "{" in css or "}" in css:
                raise ValueError(f"{css!r} is a stylesheet block, not a selector")
            for part in _split_selector(css):
                _parse_compound(part)


# ── a small, auditable CDP websocket client ─────────────────────────────────────────────────


class CdpError(Exception):
    pass


class CdpConnection:
    def __init__(self, ws_url: str, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        match = re.match(r"^ws://([^/:]+):(\d+)(/.*)?$", ws_url)
        if not match:
            raise CdpError(f"unusable websocket url: {ws_url!r}")
        host, port, path = match.group(1), int(match.group(2)), match.group(3) or "/"
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CdpError("CDP closed the socket during the websocket handshake")
            response += chunk
        if b"101" not in response.split(b"\r\n", 1)[0]:
            raise CdpError(f"CDP refused the websocket upgrade: {response.split(b'\r\n', 1)[0]!r}")
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if accept.encode() not in response:
            raise CdpError("CDP answered with the wrong Sec-WebSocket-Accept")
        self._next_id = 0

    # frame I/O ------------------------------------------------------------------------------
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _recv_exact(self, size: int) -> bytes:
        chunks = b""
        while len(chunks) < size:
            chunk = self.sock.recv(size - len(chunks))
            if not chunk:
                raise CdpError("CDP closed the websocket")
            chunks += chunk
        return chunks

    def _recv_frame(self) -> tuple[int, bytes]:
        first, second = self._recv_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        if opcode == 0x9:  # ping -> pong, then keep reading
            self._send_frame(0xA, payload)
            return self._recv_frame()
        return (opcode if fin else 0x0, payload)

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        self._send_frame(1, json.dumps({"id": message_id, "method": method, "params": params or {}}).encode())
        buffer = b""
        while True:
            opcode, payload = self._recv_frame()
            if opcode == 0x8:
                raise CdpError("CDP closed the websocket")
            if opcode == 0x0:
                buffer += payload
                continue
            buffer += payload
            try:
                message = json.loads(buffer.decode())
            except json.JSONDecodeError:
                continue
            buffer = b""
            if not isinstance(message, dict) or message.get("id") != message_id:
                continue  # an event, not our answer
            if "error" in message:
                raise CdpError(f"{method} failed: {message['error'].get('message')}")
            return message.get("result", {})

    def evaluate(self, expression: str) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": False},
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            raise CdpError(f"page evaluation raised: {detail.get('text') or detail}")
        return result.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ── live CDP ───────────────────────────────────────────────────────────────────────────────


def list_targets(port: int, timeout: float = 3.0) -> list[dict[str, Any]]:
    url = f"http://{CDP_HOST}:{port}/json/list"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise unavailable(
            f"CDP is not reachable on {CDP_HOST}:{port} ({type(exc).__name__}: {exc})",
            how_to_fix=(
                "launch the automation Chrome with "
                f"--remote-debugging-port={port} --user-data-dir=<dedicated automation profile>"
            ),
            port=port,
        ) from exc
    if not isinstance(payload, list):
        raise unavailable(f"CDP on {CDP_HOST}:{port} answered a non-list /json/list")
    return [target for target in payload if isinstance(target, dict)]


def messages_target(targets: list[dict[str, Any]]) -> dict[str, Any] | None:
    for target in targets:
        if target.get("type") == "page" and str(target.get("url", "")).startswith(MESSAGES_PREFIX):
            return target
    return None


def live_snapshot(port: int) -> tuple[dict[str, Any], str | None]:
    """Return (snapshot, ws_url).  Read-only: one `Runtime.evaluate` of the DOM snapshot."""
    targets = list_targets(port)
    target = messages_target(targets)
    if target is None:
        return (
            {
                "url": None,
                "title": None,
                "pairing_visible": False,
                "thread_list_present": False,
                "thread_count": 0,
                "threads": [],
                "composer_present": False,
                "send_button_present": False,
                "header": None,
            },
            None,
        )
    ws_url = target.get("webSocketDebuggerUrl")
    if not isinstance(ws_url, str) or not ws_url:
        raise unavailable("the messages.google.com tab exposes no webSocketDebuggerUrl")
    connection = CdpConnection(ws_url)
    try:
        snapshot = connection.evaluate(SNAPSHOT_JS)
    finally:
        connection.close()
    if not isinstance(snapshot, dict):
        raise unavailable("the messages.google.com page answered no DOM snapshot")
    return snapshot, ws_url


def check(port: int, fixture: Path | None) -> dict[str, Any]:
    if fixture is not None:
        snapshot = fixture_snapshot(fixture)
        paired = is_paired(snapshot, require_url=False)
        return {
            "cdp_reachable": False,
            "paired": paired,
            "url": None,
            "thread_count": snapshot["thread_count"],
            "threads": snapshot["threads"],
            "composer_present": snapshot["composer_present"],
            "send_button_present": snapshot["send_button_present"],
            "pairing_visible": snapshot["pairing_visible"],
            "header": snapshot.get("header"),
            "source": "fixture",
            "fixture": str(fixture),
            "reason": None if paired else _unpaired_reason(snapshot),
        }
    snapshot, _ws = live_snapshot(port)
    paired = is_paired(snapshot, require_url=True)
    return {
        "cdp_reachable": True,
        "paired": paired,
        "url": snapshot.get("url"),
        "thread_count": snapshot["thread_count"],
        "threads": snapshot["threads"],
        "composer_present": snapshot["composer_present"],
        "send_button_present": snapshot["send_button_present"],
        "pairing_visible": snapshot["pairing_visible"],
        "header": snapshot.get("header"),
        "source": "cdp",
        "reason": None if paired else _unpaired_reason(snapshot),
        "port": port,
    }


def _unpaired_reason(snapshot: dict[str, Any]) -> str:
    url = str(snapshot.get("url") or "")
    if any(marker in url for marker in WELCOME_MARKERS):
        return (
            "the Google Messages page is the welcome/pairing flow — this automation profile has "
            "not been paired with the phone"
        )
    if snapshot.get("url") is None and not snapshot.get("thread_list_present"):
        return "no messages.google.com/web tab is open in the automation Chrome"
    if snapshot.get("pairing_visible"):
        return "the Google Messages page is showing the pairing screen"
    if not snapshot.get("thread_list_present"):
        return "the Google Messages page has no conversation list"
    return "the page is not the messages.google.com/web app"


def _wait_for(predicate, *, timeout: float, interval: float = 0.15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def send(port: int, target: Any, body: str, timeout: float) -> dict[str, Any]:
    """Send ONE SMS through the paired page, then verify it in the DOM before claiming success."""
    targets = list_targets(port)
    page = messages_target(targets)
    if page is None:
        raise unpaired(
            "no messages.google.com/web tab is open in the automation Chrome",
            how_to_fix=(
                "launch the automation Chrome with the dedicated profile, open "
                "https://messages.google.com/web and pair it with the phone once"
            ),
        )
    ws_url = page.get("webSocketDebuggerUrl")
    if not isinstance(ws_url, str) or not ws_url:
        raise unavailable("the messages.google.com tab exposes no webSocketDebuggerUrl")

    connection = CdpConnection(ws_url, timeout=timeout)
    try:
        snapshot = connection.evaluate(SNAPSHOT_JS)
        if not isinstance(snapshot, dict):
            raise unavailable("the messages.google.com page answered no DOM snapshot")
        if not is_paired(snapshot, require_url=True):
            raise unpaired(
                _unpaired_reason(snapshot),
                how_to_fix="pair the phone: open Google Messages web in the automation Chrome and scan the QR code once",
            )
        thread = resolve_thread(snapshot["threads"], target)
        if thread is None:
            raise invalid(
                f"no open thread matches {mask_number(target) or mask_name(target) or 'that target'}",
                how_to_fix=(
                    "open the conversation in Google Messages web first; this driver sends into an "
                    "existing thread and never starts a new conversation"
                ),
                thread_count=snapshot["thread_count"],
            )

        opened = connection.evaluate(open_thread_js(int(thread["index"])))
        if not isinstance(opened, dict) or not opened.get("opened"):
            raise unavailable(f"the thread could not be opened ({opened!r})")

        def focus_composer() -> dict[str, Any] | None:
            state = connection.evaluate(COMPOSER_JS)
            return state if isinstance(state, dict) else None

        composer = _wait_for(focus_composer, timeout=min(5.0, timeout))
        if not composer or not composer.get("focused"):
            raise unavailable("the message composer did not take focus")

        connection.call("Input.insertText", {"text": body})

        def composer_matches() -> bool:
            value = connection.evaluate(COMPOSER_TEXT_JS)
            return isinstance(value, str) and value.strip() == body.strip()

        if not _wait_for(composer_matches, timeout=min(5.0, timeout)):
            raise unavailable(
                "the body did not land in the composer — nothing was sent",
                how_to_fix="check the automation Chrome window; the composer may be read-only while the phone is offline",
            )

        clicked = connection.evaluate(CLICK_SEND_JS)
        if not isinstance(clicked, dict) or not clicked.get("clicked"):
            raise unavailable(
                f"the send button could not be clicked ({clicked!r}) — nothing was sent"
            )

        def composer_cleared() -> bool:
            value = connection.evaluate(COMPOSER_TEXT_JS)
            return isinstance(value, str) and value.strip() == ""

        cleared = bool(_wait_for(composer_cleared, timeout=min(8.0, timeout)))
        outgoing = connection.evaluate(LAST_OUTGOING_JS)
        observed = isinstance(outgoing, str) and outgoing.strip() != ""
        matches = observed and (
            outgoing.strip().startswith(body.strip()[:24]) or body.strip().startswith(outgoing.strip()[:24])
        )
        if not (cleared and matches):
            raise unavailable(
                "the send click was issued but the page did not confirm it "
                f"(composer cleared: {cleared}, outgoing bubble matched: {matches}) — "
                "treat the message as possibly sent and check the phone before retrying",
                how_to_fix="open Google Messages and check the thread before retrying; this driver will not claim an unobserved send",
                composer_cleared=cleared,
                outgoing_matched=bool(matches),
            )
        return {
            "sent_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "to_masked": sender_masked(thread),
            "driver": DRIVER_NAME,
            "thread_id": thread.get("thread_id"),
            "verified": "composer-cleared+outgoing-bubble",
        }
    finally:
        connection.close()


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Options:
    mode: str
    fixture: Path | None = None
    to: str | None = None
    body_file: Path | None = None
    port: int = DEFAULT_PORT
    timeout: float = DEFAULT_TIMEOUT_S
    print_js: str | None = None


def parse_args(argv: list[str]) -> Options:
    """Hand-rolled so every mistake is a NAMED JSON refusal instead of argparse's usage prose."""
    mode: str | None = None
    fixture: Path | None = None
    to: str | None = None
    body_file: Path | None = None
    port = DEFAULT_PORT
    timeout = DEFAULT_TIMEOUT_S
    print_js: str | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--check", "--send"):
            if mode is not None:
                raise UsageError("choose exactly one of --check and --send")
            mode = arg[2:]
        elif arg == "--fixture":
            i += 1
            if i >= len(argv):
                raise UsageError("--fixture needs a path")
            fixture = Path(argv[i])
        elif arg == "--to":
            i += 1
            if i >= len(argv):
                raise UsageError("--to needs a thread id or an E.164 number")
            to = argv[i]
        elif arg == "--body-file":
            i += 1
            if i >= len(argv):
                raise UsageError("--body-file needs a path")
            body_file = Path(argv[i])
        elif arg == "--port":
            i += 1
            try:
                port = int(argv[i])
            except (IndexError, ValueError) as exc:
                raise UsageError(f"--port needs an integer ({exc})") from exc
            if not 1 <= port <= 65535:
                raise UsageError("--port must be 1..65535")
        elif arg == "--timeout":
            i += 1
            try:
                timeout = float(argv[i])
            except (IndexError, ValueError) as exc:
                raise UsageError(f"--timeout needs seconds ({exc})") from exc
            if timeout <= 0:
                raise UsageError("--timeout must be positive")
        elif arg == "--print-js":
            i += 1
            print_js = argv[i] if i < len(argv) else "all"
        elif arg == "--json":
            pass  # output is ALWAYS the JSON envelope; accepted so callers can be explicit
        elif arg in ("-h", "--help"):
            raise UsageError(
                "usage: phone_send_driver.py (--check | --send --to TARGET --body-file PATH) "
                "[--fixture PATH] [--port 9223] [--timeout SECONDS] [--print-js NAME|all]"
            )
        else:
            raise UsageError(f"unknown argument: {arg}")
        i += 1

    if print_js is not None:
        return Options(mode="print-js", print_js=print_js, port=port, timeout=timeout)
    if mode is None:
        raise UsageError("choose --check or --send")
    if mode == "send":
        if not to:
            raise UsageError("--send needs --to <thread id | E.164 digits>")
        if body_file is None:
            raise UsageError("--send needs --body-file <path>")
        if fixture is not None:
            raise UsageError(
                "--send refuses a --fixture: fixture mode is a read-only DOM check and can never send"
            )
    return Options(
        mode=mode, fixture=fixture, to=to, body_file=body_file, port=port, timeout=timeout
    )


def _read_body(path: Path) -> str:
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise invalid(
            f"the body file could not be read ({type(exc).__name__}: {exc})",
            how_to_fix="pass --body-file a readable UTF-8 file",
        ) from exc
    if not body.strip():
        raise invalid("the body file is empty", how_to_fix="write the message text into the file")
    return body


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        options = parse_args(argv)
    except UsageError as exc:
        _emit(error_envelope("E_VALIDATION", str(exc), how_to_fix="fix the invocation"))
        return EXIT_USAGE

    if options.mode == "print-js":
        wanted = options.print_js or "all"
        if wanted == "all":
            _emit(ok_envelope({"expressions": JS_EXPRESSIONS}))
        elif wanted in JS_EXPRESSIONS:
            _emit(ok_envelope({"name": wanted, "expression": JS_EXPRESSIONS[wanted]}))
        else:
            _emit(error_envelope("E_VALIDATION", f"no JS expression named {wanted!r}"))
            return EXIT_USAGE
        return EXIT_OK

    try:
        validate_selector_map()
        if options.mode == "check":
            _emit(ok_envelope(check(options.port, options.fixture)))
            return EXIT_OK
        assert options.to is not None and options.body_file is not None  # parse_args guarantees it
        body = _read_body(options.body_file)
        result = send(options.port, options.to, body, options.timeout)
        _emit(ok_envelope(result))
        return EXIT_OK
    except DriverRefusal as exc:
        _emit(error_envelope(exc.code, exc.message, exc.how_to_fix, **exc.context))
        return exc.exit_code
    except (CdpError, OSError, ValueError) as exc:
        _emit(
            error_envelope(
                "E_PHONE_UNAVAILABLE",
                f"the driver failed before it could report a send ({type(exc).__name__}: {exc})",
                how_to_fix="check the automation Chrome is running with --remote-debugging-port and is paired",
            )
        )
        return EXIT_UNAVAILABLE


if __name__ == "__main__":
    raise SystemExit(main())
