"""V29 / L1 — the backend remainders, closed RED-first (T1.1–T1.4).

Every leg below was written against the BASE tree (`a825355`), where it fails for the reason the
task predicts; the lane's `red/<task>.txt` holds that run and `green/<task>.txt` the run after the
minimal fix.  No product code was written before these tests existed.

T1.1 `test_probe_never_reads_past_the_cap`
     The probe must stop READING at `MAX_PROBE_BYTES`, not buffer the whole body and then compare
     its length.  A `MockTransport` body cannot prove that — it materialises the response — so the
     double is a COUNTING async stream: what fails the old `response.content` implementation is
     the number of bytes the code PULLS (300 KiB), not the size it reports afterwards.

T1.2 `test_delete_route_runs_in_a_worker_thread`
     Deleting a site tree is blocking filesystem work; the route must hand it to
     `run_in_threadpool` instead of stalling the event loop (the `/ops` cleanup route is the
     precedent this pins).

T1.3 `test_blank_status_is_refused_on_create` / `..._on_patch`
     `""`/whitespace is caller error: `E_VALIDATION` with `context.field == "status"`.
     `null`/omitted keeps meaning "unrecorded" (and an explicit `null` still clears on PATCH).

T1.4 `test_cleanup_result_serves_truncation_flags`
     The retention name lists are capped at 50 while the counts stay exact, and the two flags that
     SAY SO must survive the served result — the response model used to drop them, so the panel
     could not tell a complete list from a capped one.

DOUBLES: only the outbound HTTP transport for T1.1 (the same labelled `_TRANSPORT` seam
`tests/unit/test_v27_integrations_probe.py` uses) and throwaway workspace directories.
"""
from __future__ import annotations

import os
import threading
import time

import httpx
import pytest

from app.core.errors import E_UPSTREAM, E_VALIDATION, PolsiaError
from app.services import integration_status_service

#: Clearly-fake, ASCII, NOT shaped like a provider credential (nothing here can be mistaken for a
#: live key, and `sk-`/`ghp_` style prefixes are avoided so the campaign's secret sweep stays quiet).
FAKE_PROBE_TOKEN = "v29-fake-probe-token-do-not-use"

CLEANUP_ROUTE = "/api/v1/ops/run-workspace-cleanup"

#: The seven keys the cleanup contract has always served; the truncation flags are OPTIONAL and
#: appear only when the corresponding name list was capped.
CLEANUP_KEYS = {
    "runs_dir", "count", "kept", "removed", "skipped", "policy", "applied",
}


# ── T1.1 — the probe reads AT MOST the cap ────────────────────────────────────────────────────


class CountingStream(httpx.AsyncByteStream):
    """LABELLED TEST DOUBLE — 300 KiB of body that RECORDS how much of it gets pulled."""

    CHUNK = 1024
    CHUNKS = 300

    def __init__(self) -> None:
        self.yielded = 0

    async def __aiter__(self):  # noqa: ANN204 — an async generator, not a coroutine
        for _ in range(self.CHUNKS):
            self.yielded += self.CHUNK
            yield b"x" * self.CHUNK


async def test_probe_never_reads_past_the_cap(monkeypatch):
    """`E_UPSTREAM` past the cap — and the stream is abandoned, not drained first."""
    stream = CountingStream()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    monkeypatch.setattr(integration_status_service, "_TRANSPORT", transport, raising=True)

    with pytest.raises(PolsiaError) as caught:
        await integration_status_service._probe_json(
            "github", "https://api.github.com/user", FAKE_PROBE_TOKEN
        )

    cap = integration_status_service.MAX_PROBE_BYTES
    assert caught.value.code == E_UPSTREAM
    assert str(cap) in caught.value.message
    assert stream.yielded <= cap + CountingStream.CHUNK, (
        f"the probe pulled {stream.yielded} bytes from a {CountingStream.CHUNK * CountingStream.CHUNKS}"
        f"-byte body; the cap is {cap}, so it must abort within one chunk of it"
    )


# ── T1.2 — the delete route does its filesystem work off the event loop ───────────────────────


async def test_delete_route_runs_in_a_worker_thread(api_client, auth_headers, monkeypatch):
    """The service call must run in a worker thread — deleting a tree blocks."""
    from app.api.v1 import sites as sites_route

    seen: dict[str, object] = {}

    def fake_delete(slug: str) -> dict[str, object]:
        seen["slug"] = slug
        seen["thread"] = threading.current_thread()
        return {"slug": slug, "deleted": True}

    monkeypatch.setattr(sites_route, "delete_site", fake_delete)

    response = await api_client.delete("/api/v1/sites/v29-thread-probe", headers=auth_headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"slug": "v29-thread-probe", "deleted": True}
    assert seen["slug"] == "v29-thread-probe"
    assert seen["thread"] is not threading.main_thread(), (
        "the site delete ran on the event loop's own thread; a blocking tree removal belongs in "
        "`run_in_threadpool` (the /ops cleanup route precedent)"
    )


# ── T1.3 — a blank prospect status is caller error ───────────────────────────────────────────


async def test_blank_status_is_refused_on_create(api_client, auth_headers):
    """`{"status": "   "}` is a 422 `E_VALIDATION` naming the field — and no row is written."""
    response = await api_client.post(
        "/api/v1/prospects",
        json={"email": "v29-blank-create@example.com", "status": "   "},
        headers=auth_headers,
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == E_VALIDATION
    assert error["context"]["field"] == "status"

    listed = await api_client.get(
        "/api/v1/prospects", params={"search": "v29-blank-create@example.com"}, headers=auth_headers
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == [], "a refused create must never leave a row behind"


async def test_blank_status_is_refused_on_patch(api_client, auth_headers):
    """The PATCH path refuses a blank too; `null` still clears, a word still stores."""
    created = await api_client.post(
        "/api/v1/prospects",
        json={"email": "v29-blank-patch@example.com", "status": "new"},
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    prospect_id = created.json()["id"]

    refused = await api_client.patch(
        f"/api/v1/prospects/{prospect_id}", json={"status": "  "}, headers=auth_headers
    )
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == E_VALIDATION
    assert error["context"]["field"] == "status"

    unchanged = await api_client.get(f"/api/v1/prospects/{prospect_id}", headers=auth_headers)
    assert unchanged.json()["status"] == "new", "a refused patch must not touch the stored word"

    cleared = await api_client.patch(
        f"/api/v1/prospects/{prospect_id}", json={"status": None}, headers=auth_headers
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["status"] is None, "an explicit null means 'unrecorded', not a refusal"

    stored = await api_client.patch(
        f"/api/v1/prospects/{prospect_id}", json={"status": "  contacted  "}, headers=auth_headers
    )
    assert stored.status_code == 200, stored.text
    assert stored.json()["status"] == "contacted", "a real word is trimmed and stored"


# ── T1.4 — the truncation flags survive the served result ────────────────────────────────────


def _seed_expired_runs(root, expired: int, fresh: int, strays: int) -> None:
    """`expired` old run dirs, `fresh` recent ones and `strays` non-directories under `runs/`."""
    from celery_app.tasks import maintenance as maintenance_tasks

    runs = root / "runs"
    runs.mkdir(parents=True)
    old = time.time() - (maintenance_tasks.RUN_RETENTION_MAX_AGE_HOURS + 6) * 3600.0
    for index in range(expired):
        directory = runs / f"run-v29-old-{index:03d}"
        directory.mkdir()
        os.utime(directory, (old, old))
    for index in range(fresh):
        (runs / f"run-v29-new-{index:03d}").mkdir()
    for index in range(strays):
        (runs / f"stray-v29-{index:03d}.txt").write_text("x", encoding="utf-8")


async def test_cleanup_result_serves_truncation_flags(
    api_client, auth_headers, async_db_session, tmp_path
):
    """73 removals + 61 skips: counts exact, names capped at 50, both flags served as True."""
    from celery_app.tasks import maintenance as maintenance_tasks

    root = tmp_path / "workspace"
    _seed_expired_runs(root, expired=73, fresh=20, strays=61)

    outcome = maintenance_tasks.run_workspace_cleanup(apply=False, workspace_root=root)
    cap = maintenance_tasks.RUN_WORKSPACE_CLEANUP_NAME_CAP
    assert outcome["count"] == 73, "the COUNT is always exact"
    assert len(outcome["removed"]) == cap, "the served name list is capped"
    assert outcome["removed_truncated"] is True
    assert len(outcome["skipped"]) == cap
    assert outcome["skipped_truncated"] is True

    entry = await maintenance_tasks.record_run_workspace_cleanup(
        async_db_session, outcome, trigger="owner"
    )
    await async_db_session.commit()
    assert int(entry.id) > 0

    served = await api_client.get(CLEANUP_ROUTE, headers=auth_headers)
    assert served.status_code == 200, served.text
    result = served.json()["data"]["result"]
    assert result["count"] == 73
    assert len(result["removed"]) == cap
    assert result["removed_truncated"] is True, "the served result dropped the truncation flag"
    assert result["skipped_truncated"] is True


async def test_a_complete_cleanup_result_still_serves_exactly_seven_keys(
    api_client, auth_headers, tmp_path
):
    """No cap happened, so no flag is claimed: absent means "complete list", never `false`."""
    root = tmp_path / "workspace"
    _seed_expired_runs(root, expired=3, fresh=22, strays=2)

    response = await api_client.post(
        CLEANUP_ROUTE, json={"workspace_root": str(root)}, headers=auth_headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == CLEANUP_KEYS, "an uncapped result must keep the seven-key shape"
