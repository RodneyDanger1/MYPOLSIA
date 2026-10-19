"""Wave V25 / lane L3 — the Render deploy adapter for a generated site.

RED leg: this file is written BEFORE `app/services/deploy_service.py`, the two deploy routes and
the `site_service` deploy-state helpers exist, so it fails on the missing module — the honest RED
state for a new capability.  Raw output goes to `wave-v25/l3-deploy/raw/red-backend.txt`.

What each group proves (the brief's frozen interface):

  * **Dry run is the default and it mutates NOTHING** — no HTTP request leaves the process (the
    mocked transport below fails the test if it is asked for one) and `meta.json` is byte-identical
    afterwards.
  * **The plan is the exact call** — method, URL, body and header NAMES; the credential value is
    redacted (`Bearer …`), never echoed, and the raw key never appears anywhere in the response.
  * **No key / no service id is the named `E_CONFIG`** — before any network work, in both modes.
    A dry run of a call that can never run would be a false impression, so dry run refuses too.
  * **Live deploy** goes through `httpx.MockTransport` — *labelled mocked transport: this is a
    fake Render, not a live call* — and:
      - success is claimed ONLY when Render returned a deploy id.  A 2xx without an `id` is the
        named `E_UPSTREAM`, and the persisted record says failed.
      - 5xx / timeout / transport error -> `E_UPSTREAM`; 401/403/404 -> `E_CONFIG` (the owner's
        credential/service is the thing to fix).
      - `last_deploy` (status + url + at + id + error) is persisted into the site's `meta.json`,
        and every pre-existing meta key survives.
  * **`GET .../deploy`** answers `last_deploy: null` when a site has never been deployed and the
    named `E_NOT_FOUND` for an unknown slug.  It never calls Render.
  * **The HTTP surface**: POST body/query `dry_run` (query wins, default TRUE), owner auth, and the
    unknown-slug refusal on the deploy request is `E_VALIDATION` as the brief names it.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.core.errors import E_CONFIG, E_NOT_FOUND, E_UPSTREAM, E_VALIDATION, PolsiaError
from app.services import deploy_service, site_service

API_KEY = "rnd_v25_test_key_do_not_leak"
SERVICE_ID = "srv-v25test"

#: Every test's site, written the way `generate_site` leaves it: two files plus `meta.json`.
META = {"slug": "landing-page", "title": "Landing page", "created_at": "2026-10-19T12:00:00+00:00",
        "bytes": 4321}


def _write_site(root: Path, slug: str = "landing-page", meta: dict | None = None) -> Path:
    directory = root / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    (directory / "styles.css").write_text("body{}", encoding="utf-8")
    (directory / "meta.json").write_text(
        json.dumps(meta or {**META, "slug": slug}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return directory


@pytest.fixture
def configured(monkeypatch):
    """The owner's credential, as the app's secret surface presents it."""
    monkeypatch.setenv(deploy_service.RENDER_API_KEY_ENV, API_KEY)
    monkeypatch.setenv(deploy_service.RENDER_SERVICE_ID_ENV, SERVICE_ID)
    return API_KEY


@pytest.fixture
def unconfigured(monkeypatch):
    monkeypatch.delenv(deploy_service.RENDER_API_KEY_ENV, raising=False)
    monkeypatch.delenv(deploy_service.RENDER_SERVICE_ID_ENV, raising=False)
    from app.config import settings

    monkeypatch.setattr(settings, "render_api_key", "")
    monkeypatch.setattr(settings, "render_service_id", "")


class Recorder:
    """A labelled MOCKED Render transport: a fake, never a live call.

    It records every request it is handed and answers from `responder`; the default responder
    raises so a test that does not expect network work cannot silently pass by touching it.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responder = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responder is None:  # pragma: no cover - a test bug, surfaced loudly
            raise AssertionError(f"unexpected Render call: {request.method} {request.url}")
        return self.responder(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def _deploy_ok(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/deploys"):
        return httpx.Response(
            201,
            json={"id": "dep-abc123", "status": "created", "triggeredBy": "manual"},
        )
    return httpx.Response(200, json={"id": SERVICE_ID, "serviceDetails": {"url": "shop.onrender.com"}})


# ── 1. dry run: default, exact plan, zero mutation ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_returns_the_exact_planned_call(
    configured, tmp_path: Path
) -> None:
    recorder = Recorder()
    site_dir = _write_site(tmp_path)
    before = (site_dir / "meta.json").read_bytes()

    result = await deploy_service.deploy_site(
        "landing-page", root=tmp_path, transport=recorder.transport
    )

    assert result["dry_run"] is True
    assert result["configured"] is True
    plan = result["plan"]
    assert plan["method"] == "POST"
    assert plan["url"] == f"https://api.render.com/v1/services/{SERVICE_ID}/deploys"
    assert plan["body"] == {"clearCache": "do_not_clear"}
    assert set(plan["headers"]) == {"authorization", "accept", "content-type"}
    assert plan["headers"]["authorization"].startswith("Bearer ")
    assert API_KEY not in plan["headers"]["authorization"]
    # Nothing was sent and nothing was written: a dry run is a preview, not a deploy.
    assert recorder.requests == []
    assert (site_dir / "meta.json").read_bytes() == before
    # The response never echoes the credential.
    assert API_KEY not in json.dumps(result)
    assert result["deploy_id"] is None and result["at"] is None


@pytest.mark.asyncio
async def test_dry_run_on_a_never_deployed_site_leaves_no_last_deploy_record(
    configured, tmp_path: Path
) -> None:
    recorder = Recorder()
    _write_site(tmp_path)
    await deploy_service.deploy_site("landing-page", root=tmp_path, transport=recorder.transport)
    state = deploy_service.deploy_state("landing-page", root=tmp_path)
    assert state["last_deploy"] is None


# ── 2. configuration refusals ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_no_api_key_is_the_named_config_refusal_in_both_modes(
    configured, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(deploy_service.RENDER_API_KEY_ENV, raising=False)
    from app.config import settings

    monkeypatch.setattr(settings, "render_api_key", "")
    recorder = Recorder()
    _write_site(tmp_path)
    for dry_run in (True, False):
        with pytest.raises(PolsiaError) as excinfo:
            await deploy_service.deploy_site(
                "landing-page", dry_run=dry_run, root=tmp_path, transport=recorder.transport
            )
        assert excinfo.value.code == E_CONFIG
        assert deploy_service.RENDER_API_KEY_ENV in excinfo.value.message
        assert excinfo.value.context["missing"] == [deploy_service.RENDER_API_KEY_ENV]
        assert recorder.requests == []


@pytest.mark.asyncio
async def test_a_key_without_a_service_id_is_the_named_config_refusal(
    configured, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(deploy_service.RENDER_SERVICE_ID_ENV, raising=False)
    from app.config import settings

    monkeypatch.setattr(settings, "render_service_id", "")
    recorder = Recorder()
    _write_site(tmp_path)
    with pytest.raises(PolsiaError) as excinfo:
        await deploy_service.deploy_site("landing-page", root=tmp_path, transport=recorder.transport)
    assert excinfo.value.code == E_CONFIG
    assert deploy_service.RENDER_SERVICE_ID_ENV in excinfo.value.message
    assert excinfo.value.context["missing"] == [deploy_service.RENDER_SERVICE_ID_ENV]


@pytest.mark.asyncio
async def test_the_secret_never_appears_in_a_refusal(configured, monkeypatch, tmp_path: Path) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused for test", request=request)

    recorder = Recorder()
    recorder.responder = boom
    _write_site(tmp_path)
    with pytest.raises(PolsiaError) as excinfo:
        await deploy_service.deploy_site(
            "landing-page", dry_run=False, root=tmp_path, transport=recorder.transport
        )
    assert excinfo.value.code == E_UPSTREAM
    assert API_KEY not in excinfo.value.message
    assert API_KEY not in json.dumps(excinfo.value.context)


# ── 3. slug refusal ─────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_unknown_slug_on_a_deploy_request_is_the_named_validation_refusal(
    configured, tmp_path: Path
) -> None:
    recorder = Recorder()
    with pytest.raises(PolsiaError) as excinfo:
        await deploy_service.deploy_site("never-built", root=tmp_path, transport=recorder.transport)
    assert excinfo.value.code == E_VALIDATION
    assert excinfo.value.context["slug"] == "never-built"
    assert recorder.requests == []


# ── 4. live deploy through the LABELLED MOCKED transport ────────────────────────────────────
@pytest.mark.asyncio
async def test_a_live_deploy_persists_the_render_deploy_id_status_url_and_stamp(
    configured, tmp_path: Path
) -> None:
    """MOCKED TRANSPORT — a fake Render, not a live network call."""
    recorder = Recorder()
    recorder.responder = _deploy_ok
    site_dir = _write_site(tmp_path)

    result = await deploy_service.deploy_site(
        "landing-page", dry_run=False, root=tmp_path, transport=recorder.transport,
        at="2026-10-19T15:00:00+00:00",
    )

    assert result["dry_run"] is False
    assert result["deploy_id"] == "dep-abc123"
    assert result["status"] == "created"
    assert result["state"] == "submitted"  # Render accepted it; it is NOT live yet
    assert result["url"] == "https://shop.onrender.com"
    assert result["at"] == "2026-10-19T15:00:00+00:00"

    # The POST carried the bearer credential and the documented body, to the documented path.
    post = next(req for req in recorder.requests if req.method == "POST")
    assert post.url == httpx.URL(f"https://api.render.com/v1/services/{SERVICE_ID}/deploys")
    assert post.headers["authorization"] == f"Bearer {API_KEY}"
    assert json.loads(post.content) == {"clearCache": "do_not_clear"}

    meta = json.loads((site_dir / "meta.json").read_text(encoding="utf-8"))
    # Every pre-existing key survives; the deploy facts are additive.
    assert meta["slug"] == "landing-page" and meta["title"] == "Landing page"
    assert meta["created_at"] == META["created_at"] and meta["bytes"] == META["bytes"]
    assert meta["last_deploy"] == {
        "at": "2026-10-19T15:00:00+00:00",
        "state": "submitted",
        "status": "created",
        "deploy_id": "dep-abc123",
        "url": "https://shop.onrender.com",
        "error": None,
    }
    # And the credential is nowhere in the public meta file.
    assert API_KEY not in json.dumps(meta)


@pytest.mark.asyncio
async def test_a_2xx_without_a_deploy_id_is_never_success(configured, tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.responder = lambda request: httpx.Response(201, json={"status": "created"})
    site_dir = _write_site(tmp_path)

    with pytest.raises(PolsiaError) as excinfo:
        await deploy_service.deploy_site(
            "landing-page", dry_run=False, root=tmp_path, transport=recorder.transport
        )
    assert excinfo.value.code == E_UPSTREAM
    assert "deploy id" in excinfo.value.message

    meta = json.loads((site_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["last_deploy"]["state"] == "failed"
    assert meta["last_deploy"]["deploy_id"] is None
    assert meta["last_deploy"]["error"]["code"] == E_UPSTREAM


@pytest.mark.asyncio
async def test_an_upstream_5xx_is_retold_as_the_named_upstream_refusal(
    configured, tmp_path: Path
) -> None:
    recorder = Recorder()
    recorder.responder = lambda request: httpx.Response(503, text="render is down")
    site_dir = _write_site(tmp_path)

    with pytest.raises(PolsiaError) as excinfo:
        await deploy_service.deploy_site(
            "landing-page", dry_run=False, root=tmp_path, transport=recorder.transport
        )
    assert excinfo.value.code == E_UPSTREAM
    assert "503" in excinfo.value.message
    meta = json.loads((site_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["last_deploy"]["state"] == "failed"
    assert "503" in meta["last_deploy"]["error"]["message"]


@pytest.mark.asyncio
async def test_a_timeout_is_the_named_upstream_refusal_and_is_recorded_as_unknown_outcome(
    configured, tmp_path: Path
) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    recorder = Recorder()
    recorder.responder = slow
    site_dir = _write_site(tmp_path)

    with pytest.raises(PolsiaError) as excinfo:
        await deploy_service.deploy_site(
            "landing-page", dry_run=False, root=tmp_path, transport=recorder.transport
        )
    assert excinfo.value.code == E_UPSTREAM
    assert "timed out" in excinfo.value.message.lower()
    meta = json.loads((site_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["last_deploy"]["state"] == "failed"
    # A timeout may mean Render ACCEPTED the deploy: the record must not pretend otherwise.
    assert "may have accepted" in meta["last_deploy"]["error"]["message"].lower()


@pytest.mark.asyncio
async def test_a_rejected_credential_is_the_named_config_refusal(configured, tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.responder = lambda request: httpx.Response(401, json={"message": "invalid token"})
    _write_site(tmp_path)
    with pytest.raises(PolsiaError) as excinfo:
        await deploy_service.deploy_site(
            "landing-page", dry_run=False, root=tmp_path, transport=recorder.transport
        )
    assert excinfo.value.code == E_CONFIG
    assert deploy_service.RENDER_API_KEY_ENV in excinfo.value.message


@pytest.mark.asyncio
async def test_an_unknown_service_id_is_the_named_config_refusal(configured, tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.responder = lambda request: httpx.Response(404, json={"message": "service not found"})
    _write_site(tmp_path)
    with pytest.raises(PolsiaError) as excinfo:
        await deploy_service.deploy_site(
            "landing-page", dry_run=False, root=tmp_path, transport=recorder.transport
        )
    assert excinfo.value.code == E_CONFIG
    assert deploy_service.RENDER_SERVICE_ID_ENV in excinfo.value.message


@pytest.mark.asyncio
async def test_a_failed_service_url_read_does_not_invent_a_url(configured, tmp_path: Path) -> None:
    """The deploy id is the proof; a URL that could not be read is `null`, never guessed."""

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deploys"):
            return httpx.Response(201, json={"id": "dep-abc123", "status": "created"})
        return httpx.Response(500, text="service read failed")

    recorder = Recorder()
    recorder.responder = responder
    _write_site(tmp_path)
    result = await deploy_service.deploy_site(
        "landing-page", dry_run=False, root=tmp_path, transport=recorder.transport
    )
    assert result["deploy_id"] == "dep-abc123"
    assert result["url"] is None


# ── 5. the persisted read ───────────────────────────────────────────────────────────────────
def test_deploy_state_is_null_for_a_site_that_never_deployed(configured, tmp_path: Path) -> None:
    _write_site(tmp_path)
    state = deploy_service.deploy_state("landing-page", root=tmp_path)
    assert state["slug"] == "landing-page"
    assert state["configured"] is True
    assert state["missing"] == []
    assert state["service_id"] == SERVICE_ID
    assert state["last_deploy"] is None


def test_deploy_state_of_an_unknown_slug_is_the_named_not_found(tmp_path: Path) -> None:
    with pytest.raises(PolsiaError) as excinfo:
        deploy_service.deploy_state("not-there", root=tmp_path)
    assert excinfo.value.code == E_NOT_FOUND
    assert excinfo.value.context["slug"] == "not-there"


def test_deploy_state_reports_what_is_missing_without_raising(unconfigured, tmp_path: Path) -> None:
    _write_site(tmp_path)
    state = deploy_service.deploy_state("landing-page", root=tmp_path)
    assert state["configured"] is False
    assert state["missing"] == [
        deploy_service.RENDER_API_KEY_ENV,
        deploy_service.RENDER_SERVICE_ID_ENV,
    ]
    assert state["last_deploy"] is None


def test_a_corrupt_last_deploy_record_is_reported_as_absent_not_as_a_crash(
    configured, tmp_path: Path
) -> None:
    _write_site(tmp_path, meta={**META, "last_deploy": "not-an-object"})
    state = deploy_service.deploy_state("landing-page", root=tmp_path)
    assert state["last_deploy"] is None


# ── 6. the HTTP surface ────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_post_deploy_defaults_to_a_dry_run(
    api_client, auth_headers, configured, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    _write_site(tmp_path)
    resp = await api_client.post("/api/v1/sites/landing-page/deploy", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["plan"]["method"] == "POST"
    assert body["plan"]["url"].endswith(f"/services/{SERVICE_ID}/deploys")
    assert body["deploy_id"] is None
    assert API_KEY not in resp.text


@pytest.mark.asyncio
async def test_the_query_flag_wins_over_the_body_flag(
    api_client, auth_headers, configured, tmp_path: Path, monkeypatch
) -> None:
    """`?dry_run=true` with `{"dry_run": false}` must stay a dry run — no network in tests."""
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    _write_site(tmp_path)
    resp = await api_client.post(
        "/api/v1/sites/landing-page/deploy?dry_run=true",
        json={"dry_run": False},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["dry_run"] is True


@pytest.mark.asyncio
async def test_post_deploy_without_credentials_is_503_e_config(
    api_client, auth_headers, unconfigured, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    _write_site(tmp_path)
    resp = await api_client.post("/api/v1/sites/landing-page/deploy", headers=auth_headers)
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == E_CONFIG
    assert resp.headers["X-Polsia-Error-Code"] == E_CONFIG


@pytest.mark.asyncio
async def test_post_deploy_for_an_unknown_slug_is_422_e_validation(
    api_client, auth_headers, configured, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    resp = await api_client.post("/api/v1/sites/never-built/deploy", headers=auth_headers)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == E_VALIDATION


@pytest.mark.asyncio
async def test_get_deploy_state_is_null_before_the_first_deploy(
    api_client, auth_headers, configured, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    _write_site(tmp_path)
    resp = await api_client.get("/api/v1/sites/landing-page/deploy", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "slug": "landing-page",
        "configured": True,
        "missing": [],
        "service_id": SERVICE_ID,
        "last_deploy": None,
    }


@pytest.mark.asyncio
async def test_get_deploy_state_for_an_unknown_slug_is_404_e_not_found(
    api_client, auth_headers, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    resp = await api_client.get("/api/v1/sites/never-built/deploy", headers=auth_headers)
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == E_NOT_FOUND


@pytest.mark.asyncio
async def test_get_deploy_state_reports_last_deploy_written_by_the_service(
    api_client, auth_headers, configured, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    _write_site(tmp_path)
    site_service.record_deploy(
        "landing-page",
        {
            "at": "2026-10-19T15:00:00+00:00",
            "state": "submitted",
            "status": "created",
            "deploy_id": "dep-abc123",
            "url": "https://shop.onrender.com",
            "error": None,
        },
        root=tmp_path,
    )
    resp = await api_client.get("/api/v1/sites/landing-page/deploy", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["last_deploy"]["deploy_id"] == "dep-abc123"


@pytest.mark.asyncio
async def test_deploy_routes_require_the_owner_key(api_client, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    _write_site(tmp_path)
    for method, path in (("post", "/api/v1/sites/landing-page/deploy"),
                         ("get", "/api/v1/sites/landing-page/deploy")):
        resp = await getattr(api_client, method)(path)
        assert resp.status_code == 401, (method, resp.text)
        assert resp.json()["error"]["code"] == "E_AUTH"
