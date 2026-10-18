"""WAVE V17 / lane L2 — the REAL phone SEND path: `POST /api/v1/phone/send`.

RED-FIRST: written before `services/phone_send_service.py`, before the route, before
`scripts/phone_send_driver.py`.  At HEAD this module cannot import its subject and every pin below
fails with `ModuleNotFoundError`; the fix is the implementation, not the test.

FROZEN INTERFACE (wave-v17/V17-PLAN.md)
    POST /api/v1/phone/send   key-auth   {"to": "<thread_id:int | E.164 digits>", "body": "1..640"}
    success -> 200 {"ok": true, "data": {sent_at, to_masked, driver: "cdp-google-messages"}}
    refusals BY NAME (+ `X-Polsia-Error-Code`):
        403 E_SANDBOX_BLOCKED        (settings.sandbox_mode — the shipped default)
        409 E_PHONE_SEND_UNPAIRED    (no paired Google Messages web session)
        503 E_PHONE_UNAVAILABLE      (driver missing / CDP unreachable / timeout / unparsable output)
        422 E_VALIDATION             (malformed `to` or `body`)
    Audit: exactly ONE `activity_log` row per attempt — sent or refused — and the row never
    carries the body text or an unmasked number.

The boundary is `phone_send_service._run_driver` (the real subprocess call).  Every route test here
REPLACES that boundary with a fake process result — the PRODUCT path (policy gate -> validation ->
driver availability -> subprocess argv -> JSON protocol -> typed result) stays real.  The sandbox
pin is POISONED on purpose: `_run_driver` raises if touched, so "sandbox refuses before any driver
work" is proven by the attempt, not by a call count.

The second half of the file pins the DRIVER's own logic offline: argparse/usage, thread matching,
selector-map coverage and a real snapshot parse against a local HTML fixture
(`tests/fixtures/gm_messages_web_send.html` — TEST FIXTURE, not product markup; the product drives
the live site over CDP).  `--send` against a fixture is refused by construction: the fixture mode is
read-only, so no test in this file can send anything anywhere.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.core.errors import E_PHONE_UNAVAILABLE, E_SANDBOX_BLOCKED

REPO_ROOT = Path(__file__).resolve().parents[4]
DRIVER_FILE = REPO_ROOT / "scripts" / "phone_send_driver.py"
FIXTURE_PAIRED = Path(__file__).resolve().parents[2] / "fixtures" / "gm_messages_web_send.html"
FIXTURE_PAIRING = Path(__file__).resolve().parents[2] / "fixtures" / "gm_messages_web_pairing.html"
SEND_URL = "/api/v1/phone/send"
BODY = "Running ten minutes late — see you at the café."


def _intact_service():
    """Import the subject LAZILY so the RED leg fails here, naming the missing module."""
    from app.services import phone_send_service as service

    return service


def _load_driver():
    """Load `scripts/phone_send_driver.py` by path — `scripts/` is not an importable package."""
    assert DRIVER_FILE.is_file(), f"the driver is missing: {DRIVER_FILE}"
    spec = importlib.util.spec_from_file_location("phone_send_driver_v17", DRIVER_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # `from __future__ import annotations` + @dataclass: dataclasses resolves the defining module
    # through sys.modules, so register it before executing (a path-loaded module is not automatic).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _ok_driver(sent_at: str = "2026-10-18T12:34:56-05:00", to_masked: str = "•••• 4477") -> str:
    return json.dumps(
        {
            "ok": True,
            "data": {"sent_at": sent_at, "to_masked": to_masked, "driver": "cdp-google-messages"},
        }
    )


def _refused_driver(code: str, message: str = "refused by the driver") -> str:
    return json.dumps(
        {"ok": False, "error": {"code": code, "message": message, "how_to_fix": "pair the phone"}}
    )


@pytest.fixture
def live_gates(monkeypatch):
    """Sandbox OFF + the real driver file present — the state every non-sandbox pin runs in."""
    monkeypatch.setattr(settings, "sandbox_mode", False)
    service = _intact_service()
    monkeypatch.setattr(service, "DRIVER_PATH", DRIVER_FILE)
    assert DRIVER_FILE.is_file()
    return service


@pytest.fixture
def driver_says(monkeypatch, live_gates):
    """Replace the subprocess boundary; return the list of argv the service built."""

    def install(returncode: int = 0, stdout: str = "", stderr: str = "") -> list[list[str]]:
        calls: list[list[str]] = []

        async def fake_run(argv, timeout):  # noqa: ANN001 - exact boundary shape
            calls.append(list(argv))
            assert isinstance(argv, list), "the driver boundary must take an argv LIST"
            assert timeout > 0
            return live_gates.DriverRun(returncode=returncode, stdout=stdout, stderr=stderr)

        monkeypatch.setattr(live_gates, "_run_driver", fake_run)
        return calls

    return install


async def _audit_rows(async_db_session):
    from app.models.report import ActivityLog

    rows = (await async_db_session.execute(select(ActivityLog))).scalars().all()
    return [row for row in rows if row.action == "phone_send"]


async def _post(api_client, auth_headers, payload):
    return await api_client.post(SEND_URL, headers=auth_headers, json=payload)


async def test_a_refused_attempts_audit_row_is_committed_before_the_refusal_raises(mock_redis):
    """The refusal path raises AFTER writing its row — so the helper itself must COMMIT it.

    Found live (V17 drill): the sandbox 403 answered correctly with header + envelope, yet ZERO
    `phone_send` rows existed in the database after minutes of refusals.  Root cause: `get_db`
    auto-commits on success and ROLLS BACK on exception; `_audit_send_attempt` only ever `add`ed,
    so every refused attempt's audit was erased when the route raised.  Every unit pin missed it
    because `api_client` overrides `get_db` with the test's own session, which never rolls back —
    this pin uses TWO real sessions on one shared in-memory engine: the writer does what the
    dependency does on a refusal (rollback), the reader must still see the row.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    import app.models  # noqa: F401  — registers every table on Base.metadata
    from app.api.v1 import phone
    from app.core.database import Base
    from app.core.errors import E_SANDBOX_BLOCKED, PolsiaError
    from app.models.report import ActivityLog

    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    refusal = PolsiaError(E_SANDBOX_BLOCKED, "drill refusal")
    async with Session() as writer:
        await phone._audit_send_attempt(writer, {"to": "11", "body": "x" * 7}, None, refusal)
        await writer.rollback()  # exactly what get_db does when the route raises
    async with Session() as reader:
        rows = (
            (await reader.execute(select(ActivityLog).where(ActivityLog.action == "phone_send")))
            .scalars()
            .all()
        )
    await engine.dispose()

    assert len(rows) == 1, "the refused attempt's audit row must survive the request rollback"
    assert refusal.code in (rows[0].summary or ""), "the row names the refusal code"
    assert "x" * 7 not in (rows[0].detail or {}), "the body text is never in the audit row"


# ── auth + the policy gate ──────────────────────────────────────────────────────────────────────


async def test_send_requires_the_api_key(api_client):
    resp = await api_client.post(SEND_URL, json={"to": "11", "body": BODY})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "E_AUTH"


async def test_sandbox_blocks_before_any_driver_work(api_client, auth_headers, monkeypatch):
    """Sandbox ON (the shipped default) refuses 403 BY NAME — and never reaches the driver.

    Poisoned boundary + a deliberately missing driver path: if the service consulted either one
    before the policy gate the answer would be E_PHONE_UNAVAILABLE/500 instead of E_SANDBOX_BLOCKED.
    """
    service = _intact_service()
    monkeypatch.setattr(settings, "sandbox_mode", True)
    monkeypatch.setattr(service, "DRIVER_PATH", Path("C:/definitely/not/a/driver.py"))

    async def poisoned(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("sandbox must refuse before the driver boundary is touched")

    monkeypatch.setattr(service, "_run_driver", poisoned)

    resp = await _post(api_client, auth_headers, {"to": "11", "body": BODY})

    assert resp.status_code == 403
    body = resp.json()
    assert set(body) == {"ok", "error"}
    assert body["ok"] is False
    assert body["error"]["code"] == E_SANDBOX_BLOCKED
    assert resp.headers.get("X-Polsia-Error-Code") == E_SANDBOX_BLOCKED
    assert "SANDBOX_MODE" in body["error"]["how_to_fix"]


async def test_sandbox_refusal_writes_one_audit_row_without_values(
    api_client, auth_headers, monkeypatch, async_db_session
):
    service = _intact_service()
    monkeypatch.setattr(settings, "sandbox_mode", True)

    async def poisoned(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("driver touched while sandboxed")

    monkeypatch.setattr(service, "_run_driver", poisoned)
    resp = await _post(api_client, auth_headers, {"to": "+15550104477", "body": BODY})
    assert resp.status_code == 403

    rows = await _audit_rows(async_db_session)
    assert len(rows) == 1, "a refused attempt is still an attempt: exactly one audit row"
    row = rows[0]
    assert row.agent_type == "system"
    assert "E_SANDBOX_BLOCKED" in row.summary
    serialized = json.dumps(row.detail or {})
    assert BODY not in serialized
    assert "010-4477" not in serialized
    assert "5550104477" not in serialized


# ── driver availability / CDP state ───────────────────────────────────────────────────────────


async def test_missing_driver_is_a_named_503_without_spawning(
    api_client, auth_headers, live_gates, monkeypatch, tmp_path
):
    monkeypatch.setattr(live_gates, "DRIVER_PATH", tmp_path / "gone.py")

    async def poisoned(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("a missing driver must be refused before the boundary")

    monkeypatch.setattr(live_gates, "_run_driver", poisoned)
    resp = await _post(api_client, auth_headers, {"to": "11", "body": BODY})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == E_PHONE_UNAVAILABLE
    assert resp.headers.get("X-Polsia-Error-Code") == E_PHONE_UNAVAILABLE
    assert "driver is missing" in resp.json()["error"]["message"]


async def test_unpaired_session_is_409_named(
    api_client, auth_headers, driver_says, async_db_session
):
    driver_says(3, _refused_driver("E_PHONE_SEND_UNPAIRED", "no paired Google Messages session"))
    resp = await _post(api_client, auth_headers, {"to": "11", "body": BODY})

    assert resp.status_code == 409
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_PHONE_SEND_UNPAIRED"
    assert resp.headers.get("X-Polsia-Error-Code") == "E_PHONE_SEND_UNPAIRED"
    assert body["error"]["how_to_fix"], "a refusal must say how to fix it"
    rows = await _audit_rows(async_db_session)
    assert len(rows) == 1 and "E_PHONE_SEND_UNPAIRED" in rows[0].summary


async def test_cdp_unreachable_is_503(api_client, auth_headers, driver_says):
    driver_says(4, _refused_driver(E_PHONE_UNAVAILABLE, "CDP is not reachable on 127.0.0.1:9223"))
    resp = await _post(api_client, auth_headers, {"to": "11", "body": BODY})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == E_PHONE_UNAVAILABLE
    assert "9223" in resp.json()["error"]["message"]


async def test_driver_timeout_is_503(api_client, auth_headers, live_gates, monkeypatch):
    import asyncio

    monkeypatch.setattr(live_gates, "SEND_TIMEOUT_S", 0.05)

    async def slow(argv, timeout):  # noqa: ANN001
        await asyncio.sleep(1.0)
        raise AssertionError("timeout must cut the wait")

    monkeypatch.setattr(live_gates, "_run_driver", slow)
    resp = await _post(api_client, auth_headers, {"to": "11", "body": BODY})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == E_PHONE_UNAVAILABLE
    assert "timed out" in resp.json()["error"]["message"].lower()


async def test_garbage_driver_output_is_503(api_client, auth_headers, driver_says):
    driver_says(0, "Segmentation fault (not JSON)")
    resp = await _post(api_client, auth_headers, {"to": "11", "body": BODY})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == E_PHONE_UNAVAILABLE


async def test_success_claims_exactly_what_the_driver_verified(
    api_client, auth_headers, driver_says, async_db_session
):
    calls = driver_says(0, _ok_driver())
    resp = await _post(api_client, auth_headers, {"to": "+15550104477", "body": BODY})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"ok", "data"}
    assert body["ok"] is True
    data = body["data"]
    assert set(data) == {"sent_at", "to_masked", "driver"}
    assert data["driver"] == "cdp-google-messages"
    assert data["to_masked"] == "•••• 4477"
    assert data["sent_at"] == "2026-10-18T12:34:56-05:00"
    assert BODY not in resp.text, "the body is never echoed back"

    # argv: a list, the same interpreter running this suite, the driver script, --send, the target
    # and a body FILE — the message text never rides argv (a process list is a public log).
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0].endswith("python.exe") or "python" in Path(argv[0]).name
    assert argv[1].endswith("phone_send_driver.py")
    assert "--send" in argv and "--to" in argv
    assert argv[argv.index("--to") + 1] == "+15550104477"
    assert "--body-file" in argv
    assert not any(BODY in part for part in argv)

    rows = await _audit_rows(async_db_session)
    assert len(rows) == 1
    assert "sent" in rows[0].summary.lower()
    assert BODY not in json.dumps(rows[0].detail or {})
    assert "5550104477" not in json.dumps(rows[0].detail or {})


async def test_send_passes_a_private_body_file_and_cleans_it_up(
    api_client, auth_headers, live_gates, monkeypatch
):
    seen: dict[str, object] = {}

    async def fake_run(argv, timeout):  # noqa: ANN001
        body_file = Path(argv[argv.index("--body-file") + 1])
        seen["exists_during"] = body_file.is_file()
        seen["content"] = body_file.read_text(encoding="utf-8")
        seen["path"] = body_file
        return live_gates.DriverRun(0, _ok_driver(), "")

    monkeypatch.setattr(live_gates, "_run_driver", fake_run)
    resp = await _post(api_client, auth_headers, {"to": "12", "body": BODY})

    assert resp.status_code == 200
    assert seen["exists_during"] is True
    assert seen["content"] == BODY
    assert not Path(seen["path"]).exists(), "the body file must not outlive the attempt"


# ── validation table ───────────────────────────────────────────────────────────────────────────

BAD_TARGETS = [
    None,
    "",
    "   ",
    "abc",
    "0",
    "-3",
    "+",
    "+0123",
    "12 34",
    "+1 (555) 010-4477",
    "1234567890123456",  # 16 digits — beyond E.164
    True,
    3.5,
    ["11"],
]

BAD_BODIES = [
    None,
    "",
    "   \n\t  ",
    641 * "a",
    "hello\x00world",
    "bell\x07",
    ["not", "a", "string"],
    {"not": "a string"},
    42,
]


@pytest.mark.parametrize("payload", [{"to": t, "body": BODY} for t in BAD_TARGETS])
async def test_invalid_target_is_a_named_422(api_client, auth_headers, live_gates, monkeypatch, payload):
    async def poisoned(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("validation must refuse before the driver boundary")

    monkeypatch.setattr(live_gates, "_run_driver", poisoned)
    resp = await _post(api_client, auth_headers, payload)

    assert resp.status_code == 422, f"accepted a malformed target: {payload['to']!r}"
    assert resp.json()["error"]["code"] == "E_VALIDATION"
    assert resp.headers.get("X-Polsia-Error-Code") == "E_VALIDATION"


@pytest.mark.parametrize("body", BAD_BODIES)
async def test_invalid_body_is_a_named_422(api_client, auth_headers, live_gates, monkeypatch, body):
    async def poisoned(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("validation must refuse before the driver boundary")

    monkeypatch.setattr(live_gates, "_run_driver", poisoned)
    resp = await _post(api_client, auth_headers, {"to": "11", "body": body})

    assert resp.status_code == 422, f"accepted a malformed body: {body!r}"
    assert resp.json()["error"]["code"] == "E_VALIDATION"


async def test_641_characters_is_refused_at_exactly_the_declared_cap(
    api_client, auth_headers, live_gates, monkeypatch
):
    assert len(640 * "a") == 640
    calls = []

    async def fake_run(argv, timeout):  # noqa: ANN001
        calls.append(argv)
        return live_gates.DriverRun(0, _ok_driver(), "")

    monkeypatch.setattr(live_gates, "_run_driver", fake_run)
    ok_resp = await _post(api_client, auth_headers, {"to": "11", "body": 640 * "a"})
    assert ok_resp.status_code == 200, "640 characters is the inclusive cap"
    assert calls


async def test_missing_fields_are_a_named_422(api_client, auth_headers, live_gates):
    resp = await _post(api_client, auth_headers, {})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "E_VALIDATION"


async def test_thread_target_is_accepted_and_masked_in_the_audit(
    api_client, auth_headers, driver_says, async_db_session
):
    driver_says(0, _ok_driver(to_masked="Dana W••••"))
    resp = await _post(api_client, auth_headers, {"to": 11, "body": "hi"})
    assert resp.status_code == 200
    rows = await _audit_rows(async_db_session)
    assert len(rows) == 1
    assert "thread #11" in json.dumps(rows[0].detail or {})


# ── the service's own pure helpers ────────────────────────────────────────────────────────────


def test_mask_number_keeps_only_the_last_four_digits():
    service = _intact_service()
    assert service.mask_number("+15550104477") == "+•••••••4477"
    assert service.mask_number("(555) 010-4477") == "••••••4477"
    assert service.mask_number("4477") == "4477"
    assert service.mask_number("") == ""
    assert service.mask_target(11) == "thread #11"
    assert service.mask_target("11") == "thread #11"
    masked = service.mask_target("+15550104477")
    assert "5550104477" not in masked and masked.endswith("4477")


def test_driver_argv_is_a_list_and_carries_no_message_text(tmp_path):
    service = _intact_service()
    argv = service.driver_argv("send", to="+15550104477", body_file=tmp_path / "b.txt")
    assert isinstance(argv, list)
    assert argv[-2:] == ["--send", "--to"] or "--send" in argv
    assert "+15550104477" in argv
    assert str(tmp_path / "b.txt") in argv
    assert "; rm -rf" not in " ".join(argv)


def test_the_boundary_never_uses_a_shell():
    source = Path(_intact_service().__file__).read_text(encoding="utf-8")
    assert "create_subprocess_exec" in source
    assert "shell=True" not in source
    assert "os.system" not in source


def test_parse_driver_stdout_rejects_a_foreign_driver_name():
    service = _intact_service()
    forged = json.dumps(
        {"ok": True, "data": {"sent_at": "now", "to_masked": "x", "driver": "other"}}
    )
    with pytest.raises(Exception) as info:
        service.parse_driver_stdout(0, forged)
    assert getattr(info.value, "code", "") == E_PHONE_UNAVAILABLE


def test_parse_driver_stdout_maps_a_nonzero_exit_without_json():
    service = _intact_service()
    with pytest.raises(Exception) as info:
        service.parse_driver_stdout(7, "")
    assert getattr(info.value, "code", "") == E_PHONE_UNAVAILABLE
    assert info.value.context.get("exit_code") == 7


# ── the driver, offline: fixture DOM + pure logic ────────────────────────────────────────────


def test_fixtures_are_labelled_test_fixtures():
    for path in (FIXTURE_PAIRED, FIXTURE_PAIRING):
        assert path.is_file(), f"missing fixture {path}"
        head = path.read_text(encoding="utf-8")[:400].lower()
        assert "test fixture" in head, f"{path.name} must label itself a test fixture"


def test_driver_check_against_the_paired_fixture(capsys):
    driver = _load_driver()
    code = driver.main(["--check", "--fixture", str(FIXTURE_PAIRED)])
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["ok"] is True
    data = out["data"]
    assert data["cdp_reachable"] is False, "a fixture check must never claim a live CDP connection"
    assert data["paired"] is True
    assert data["source"] == "fixture"
    assert data["thread_count"] == 2
    assert [t["name"] for t in data["threads"]] == ["Dana Whitfield", "Mitch Alvarez"]
    assert data["composer_present"] is True
    assert data["send_button_present"] is True


def test_driver_check_against_the_pairing_fixture(capsys):
    driver = _load_driver()
    code = driver.main(["--check", "--fixture", str(FIXTURE_PAIRING)])
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["data"]["paired"] is False
    assert out["data"]["pairing_visible"] is True
    assert out["data"]["thread_count"] == 0


def test_driver_refuses_to_send_from_a_fixture(capsys, tmp_path):
    driver = _load_driver()
    body = tmp_path / "body.txt"
    body.write_text("hello", encoding="utf-8")
    code = driver.main(
        ["--send", "--fixture", str(FIXTURE_PAIRED), "--to", "11", "--body-file", str(body)]
    )
    out = json.loads(capsys.readouterr().out)

    assert code != 0
    assert out["ok"] is False
    assert out["error"]["code"] == "E_VALIDATION"
    assert "sent" not in json.dumps(out), "a fixture refusal must not claim a send"


def test_driver_usage_errors_are_named_json(capsys):
    driver = _load_driver()
    code = driver.main(["--send", "--to", "11"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert out["ok"] is False and out["error"]["code"] == "E_VALIDATION"

    code = driver.main(["--nonsense"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and out["error"]["code"] == "E_VALIDATION"


def test_driver_selector_map_is_complete_and_css_shaped():
    driver = _load_driver()
    selectors = driver.SELECTORS
    for role in (
        "thread_list",
        "thread_item",
        "thread_name",
        "thread_number",
        "pairing",
        "composer",
        "send_button",
        "header_title",
        "outgoing",
    ):
        assert role in selectors, f"the selector map lost the {role!r} role"
        assert isinstance(selectors[role], list) and selectors[role]
        for css in selectors[role]:
            assert isinstance(css, str) and css.strip()
            assert "{" not in css and "}" not in css, "a selector is not a stylesheet block"


def test_every_selector_role_resolves_against_the_paired_fixture():
    driver = _load_driver()
    dom = driver.FixtureDom(FIXTURE_PAIRED.read_text(encoding="utf-8"))
    for role in ("thread_list", "thread_item", "thread_name", "composer", "send_button",
                 "header_title", "outgoing"):
        assert dom.pick(role), f"{role} matched nothing in the fixture"


def test_resolve_thread_by_thread_id_number_and_suffix():
    driver = _load_driver()
    threads = [
        {"index": 0, "thread_id": "11", "name": "Dana", "number": "+1 (555) 010-4477"},
        {"index": 1, "thread_id": "12", "name": "Mitch", "number": "+1 (555) 010-9931"},
    ]
    assert driver.resolve_thread(threads, "11")["index"] == 0
    assert driver.resolve_thread(threads, "+15550109931")["index"] == 1
    assert driver.resolve_thread(threads, "5550104477")["index"] == 0
    assert driver.resolve_thread(threads, "4477") is None, "a 4-digit suffix is not a target"
    assert driver.resolve_thread(threads, "13") is None
    assert driver.resolve_thread([], "11") is None


def test_driver_masks_numbers_it_reports():
    driver = _load_driver()
    assert driver.mask_number("+1 (555) 010-4477").endswith("4477")
    assert "5550104477" not in driver.mask_number("+15550104477")
    assert driver.mask_number(None) == ""


def test_is_paired_names_the_welcome_flow_as_unpaired():
    driver = _load_driver()
    welcome = {
        "url": "https://messages.google.com/web/welcome",
        "pairing_visible": False,
        "thread_list_present": False,
    }
    assert driver.is_paired(welcome, require_url=True) is False
    assert "welcome" in driver._unpaired_reason(welcome)
    paired_shape = {
        "url": "https://messages.google.com/web/conversations",
        "pairing_visible": False,
        "thread_list_present": True,
    }
    assert driver.is_paired(paired_shape, require_url=True) is True


def test_driver_never_sends_from_check_mode():
    source = DRIVER_FILE.read_text(encoding="utf-8")
    # The only click on the send button lives behind the live `--send` branch; the fixture branch
    # returns before any CDP/send code. This pin is textual on purpose: it names the invariant.
    assert "fixture" in source and "mode == \"send\"" in source
