"""V29 follow-up G — site integrity: a delete that fails must not destroy the site's record.

Written RED-first against `9d427c7`:

**D-G1 `test_a_failed_delete_keeps_the_site_readable_and_retryable`.**  `delete_site` walks the
site's tree in `os.scandir` order, so `meta.json` — the site's ONLY record — was removed in the
middle of the walk.  When a later entry cannot be removed (measured on Windows: an open handle
makes `DeleteFile` fail with `WinError 32`), the refusal says `E_STORAGE` "make sure the site
directory is writable and retry", but by then the site is already gone from `list_sites`, a
single-slug read is `E_NOT_FOUND`, and the retry the message asks for is refused — while the one
stray file left behind makes `POST /sites` with that slug an `E_VALIDATION` collision.  The slug
is unrecoverable through the API.  The fix removes `meta.json` LAST, so a partial failure leaves
a readable, retryable site.

The remaining tests pin behaviour the V27/V29 waves already promise, so a future refactor cannot
quietly return it:

* `..._refuses_a_slug_that_never_existed` — DELETE of an unknown slug is the named `E_NOT_FOUND`
  (404) and removes nothing, not even a sibling site's files;
* `..._delete_route_keeps_the_event_loop_serving` — tree removal is blocking work, so the route
  must hand it to a worker thread (`run_in_threadpool`): the loop keeps scheduling while it runs.
  (V29 pinned thread identity; this pins the property that identity exists for.)

DOUBLES: the slow-delete test replaces `app.api.v1.sites.delete_site` with a LABELLED test double.
Nothing else is mocked — real directories under `tmp_path`, the real `site_service`.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from app.core.errors import E_NOT_FOUND, E_STORAGE, PolsiaError
from app.services import site_service

#: A file name that sorts AFTER `meta.json` in any deterministic directory walk, so the walk
#: removes `meta.json` before it reaches this entry on the pre-fix code.
LOCKED_NAME = "zzz-locked.bin"


def _write_site(root: Path, slug: str = "locked-site") -> Path:
    """A complete site directory (the V24 key set) with one file named to sort last."""
    site = root / slug
    site.mkdir(parents=True)
    for name in ("index.html", "styles.css", LOCKED_NAME):
        (site / name).write_text(name, encoding="utf-8")
    (site / "meta.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Locked",
                "created_at": "2026-01-01T00:00:00+00:00",
                "bytes": 1,
            }
        ),
        encoding="utf-8",
    )
    return site


@pytest.mark.skipif(
    os.name != "nt",
    reason=(
        "the injected failure is Windows' file-lock semantics: an open handle without "
        "FILE_SHARE_DELETE makes DeleteFile fail (WinError 32). POSIX can always unlink an open "
        "file, so the failure this test injects cannot be produced there."
    ),
)
def test_a_failed_delete_keeps_the_site_readable_and_retryable(tmp_path) -> None:
    """A delete that cannot finish must leave the site's record, so the retry can succeed."""
    root = tmp_path / "sites"
    site = _write_site(root)

    handle = (site / LOCKED_NAME).open("rb")
    try:
        with pytest.raises(PolsiaError) as excinfo:
            site_service.delete_site("locked-site", root=root)
    finally:
        handle.close()

    refusal = excinfo.value
    assert refusal.code == E_STORAGE
    assert "retry" in refusal.how_to_fix, "the refusal tells the owner to retry"

    # `meta.json` is the site's only record: a failed removal must not destroy it before the
    # entries that actually failed.  Otherwise the site is gone from every read while its files
    # (and the slug) are still on disk.
    listed = site_service.list_sites(root=root)
    assert [row["slug"] for row in listed] == ["locked-site"], (
        "the failed delete removed meta.json, so the site vanished from the list while its other "
        "files stayed behind"
    )

    # The retry the refusal asks for is therefore possible, and it finishes the job.
    assert site_service.delete_site("locked-site", root=root) == {
        "slug": "locked-site",
        "deleted": True,
    }
    assert not site.exists()


async def test_delete_refuses_a_slug_that_never_existed(api_client, auth_headers, tmp_path,
                                                        monkeypatch) -> None:
    """An unknown slug is a named 404 and the request removes nothing else."""
    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path / "sites")
    site = _write_site(tmp_path / "sites", "kept-site")

    response = await api_client.delete("/api/v1/sites/never-existed", headers=auth_headers)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == E_NOT_FOUND
    assert response.json()["error"]["context"]["slug"] == "never-existed"
    assert response.headers["X-Polsia-Error-Code"] == E_NOT_FOUND
    assert (site / "meta.json").is_file(), "a refused delete must not touch another site"


async def test_delete_route_keeps_the_event_loop_serving(
    api_client, auth_headers, monkeypatch
) -> None:
    """Deleting a tree blocks; the route must keep the loop free while it runs."""
    from app.api.v1 import sites as sites_route

    def slow_delete(slug: str) -> dict[str, object]:
        """LABELLED TEST DOUBLE: a delete that takes real, blocking wall-clock time."""
        time.sleep(0.25)
        return {"slug": slug, "deleted": True}

    monkeypatch.setattr(sites_route, "delete_site", slow_delete)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    counter = asyncio.create_task(ticker())
    try:
        response = await api_client.delete("/api/v1/sites/slow-delete", headers=auth_headers)
    finally:
        counter.cancel()

    assert response.status_code == 200, response.text
    assert response.json() == {"slug": "slow-delete", "deleted": True}
    # 250 ms of blocking work with the loop free is ~25 ticks; an on-loop delete freezes the
    # ticker for the whole call and leaves 0-1.  Five is far from both numbers on either side.
    assert ticks >= 5, (
        f"the event loop only advanced {ticks} times during a 250 ms delete; the tree removal is "
        "running ON the loop instead of in `run_in_threadpool`"
    )
