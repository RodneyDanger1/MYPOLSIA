"""WAVE 6 / F-39 — the generated-site static route must not publish files that are not pages.

THE DEFECT (visuals `VISUALS-FINDINGS-W5.md` §4, measured live on `7b81f56`):
`GET /sites/<slug>/.fixture.json` answered **200 with the marker body** on all 17 legacy site
directories (the brand site, which has no marker, 404'd).  The mount was a bare
`StaticFiles(directory=..., html=True)`, so it served *every* file in a site directory: the dotfile
markers the lanes use to mark owner data, and — by the same rule — any future fixture/debug file the
moment it was written.  The marker body even named internal process files
(`taskmaster/REPORT-W4.md`) on a public page.

WHAT THIS PINS (the fix is a serving decision, not an edit to owner data):

  * a dotfile (`.fixture.json`) is a **named 404** (`E_NOT_FOUND` envelope +
    `X-Meridian-Error-Code`) — the file stays on disk, its bytes are never served;
  * a dot-DIRECTORY is refused the same way, at any depth;
  * a file outside the generator's own file set (`debug-notes.json`, `404.html`, `notes.txt`) is
    refused the same way — the route serves a generated site, not an arbitrary directory dump;
  * the generator's real file set still serves exactly as before: the site root (index.html via
    `html=True`), `index.html`, `about.html`, `contact.html`, `styles.css`, `sitemap.xml`,
    `meta.json` (the storefront contract §9 serves `meta.json`);
  * the directory-URL redirect still works (`/sites/<slug>` -> `/sites/<slug>/`);
  * an unknown site is still a plain 404 (no regression);
  * HEAD on a refused path answers 404 with no body.

The last test asserts the markers are STILL ON DISK after the refusals: the wave-6 job keeps the
markers (they move only with the owner's disposition), so a fix that "works" by deleting them is not
a fix and must fail this suite.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import site_service

SLUG = "refusal-probe"
MARKER_NOTE = "legacy generated-site fixture from a pre-wave-1 wave; kept on purpose"
GENERATED_FILES = {
    "index.html": "<!doctype html><html><body><h1>Refusal probe</h1></body></html>",
    "about.html": "<!doctype html><html><body><h1>About</h1></body></html>",
    "contact.html": "<!doctype html><html><body><h1>Contact</h1></body></html>",
    "styles.css": "body { color: #111; }",
    "sitemap.xml": '<?xml version="1.0"?><urlset><url><loc>http://test/sites/x/</loc></url></urlset>',
    "meta.json": json.dumps({"slug": SLUG, "title": "Refusal probe", "bytes": 1}),
}


def _site_root(tmp_path: Path) -> Path:
    """A site directory shaped like a real one, plus the files F-39 is about."""
    directory = tmp_path / SLUG
    directory.mkdir(parents=True)
    for name, content in GENERATED_FILES.items():
        (directory / name).write_text(content, encoding="utf-8")
    (directory / ".fixture.json").write_text(
        json.dumps({"fixture": True, "note": MARKER_NOTE}), encoding="utf-8"
    )
    (directory / "debug-notes.json").write_text('{"debug": true}', encoding="utf-8")
    (directory / "404.html").write_text("<!doctype html><title>stray 404 page</title>", encoding="utf-8")
    (directory / ".internal").mkdir()
    (directory / ".internal" / "secret.txt").write_text("SECRET-INTERNAL-BYTES", encoding="utf-8")
    return directory


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    from app.main import create_app

    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path)
    return TestClient(create_app())


def _assert_named_404(resp, *, path: str) -> dict:
    """A refusal is the product envelope, names the code, and never a served body."""
    assert resp.status_code == 404, (path, resp.status_code, resp.text[:200])
    assert resp.headers.get("X-Meridian-Error-Code") == "E_NOT_FOUND"
    body = resp.json()
    assert body["ok"] is False
    error = body["error"]
    assert error["code"] == "E_NOT_FOUND"
    assert error["message"].strip() != ""
    assert error["how_to_fix"].strip() != ""
    # The context echoes the mount path the visitor used (Starlette's get_path drops a trailing
    # slash before the decision runs, so the comparison is slash-insensitive).
    assert error["context"]["path"].rstrip("/") == path.rstrip("/")
    assert "index.html" in error["context"]["served_files"]
    return error


# ── 1. dotfiles are refused by name (the F-39 regression) ─────────────────────────────────────
def test_a_fixture_marker_is_a_named_404_and_its_bytes_are_not_served(tmp_path: Path, monkeypatch) -> None:
    _site_root(tmp_path)
    client = _client(tmp_path, monkeypatch)

    resp = client.get(f"/sites/{SLUG}/.fixture.json")
    error = _assert_named_404(resp, path=f"/sites/{SLUG}/.fixture.json")
    assert error["context"]["reason"] == "dotfile"
    assert "dot" in error["message"].lower()
    assert MARKER_NOTE not in resp.text, "the marker body must never reach a visitor"
    assert "fixture" not in resp.text.lower() or "fixture.json" in resp.text.lower()


def test_a_dot_directory_is_refused_at_any_depth(tmp_path: Path, monkeypatch) -> None:
    _site_root(tmp_path)
    client = _client(tmp_path, monkeypatch)

    for path in (
        f"/sites/{SLUG}/.internal/secret.txt",
        f"/sites/{SLUG}/.internal/",
        "/sites/.hidden-marker",
    ):
        resp = client.get(path)
        _assert_named_404(resp, path=path)
        # The dotted file's own bytes never ride the refusal body.
        assert "SECRET-INTERNAL-BYTES" not in resp.text, path


def test_a_non_page_file_is_refused_by_name(tmp_path: Path, monkeypatch) -> None:
    _site_root(tmp_path)
    client = _client(tmp_path, monkeypatch)

    for name in ("debug-notes.json", "404.html", "notes.txt", "archive.zip"):
        path = f"/sites/{SLUG}/{name}"
        error = _assert_named_404(client.get(path), path=path)
        assert error["context"]["reason"] == "not_a_site_file", path
        # The refusal names the generator's own file set instead of a vague "not found".
        assert "index.html" in error["message"] or "index.html" in error["how_to_fix"], path


# ── 2. the generated site still serves exactly as before ─────────────────────────────────────
def test_every_generated_file_still_serves(tmp_path: Path, monkeypatch) -> None:
    _site_root(tmp_path)
    client = _client(tmp_path, monkeypatch)

    root = client.get(f"/sites/{SLUG}/")
    assert root.status_code == 200
    assert "Refusal probe" in root.text

    assert client.get(f"/sites/{SLUG}/index.html").text == GENERATED_FILES["index.html"]
    for name in ("about.html", "contact.html"):
        resp = client.get(f"/sites/{SLUG}/{name}")
        assert resp.status_code == 200, name
        assert resp.headers["content-type"].startswith("text/html"), name
    css = client.get(f"/sites/{SLUG}/styles.css")
    assert css.status_code == 200 and css.text == GENERATED_FILES["styles.css"]
    sitemap = client.get(f"/sites/{SLUG}/sitemap.xml")
    assert sitemap.status_code == 200 and "xml" in sitemap.headers["content-type"]
    # STOREFRONT-CONTRACT §9 names meta.json as part of the served surface (the census scans it).
    meta = client.get(f"/sites/{SLUG}/meta.json")
    assert meta.status_code == 200 and json.loads(meta.text)["slug"] == SLUG


def test_the_directory_url_still_redirects_to_its_trailing_slash(tmp_path: Path, monkeypatch) -> None:
    _site_root(tmp_path)
    client = _client(tmp_path, monkeypatch)

    resp = client.get(f"/sites/{SLUG}", follow_redirects=False)
    assert resp.status_code in (307, 308)
    assert resp.headers["location"].endswith(f"/sites/{SLUG}/")


def test_an_unknown_site_is_still_a_plain_404(tmp_path: Path, monkeypatch) -> None:
    _site_root(tmp_path)
    client = _client(tmp_path, monkeypatch)

    assert client.get("/sites/no-such-site/").status_code == 404


def test_head_on_a_refused_path_is_a_404_with_no_body(tmp_path: Path, monkeypatch) -> None:
    _site_root(tmp_path)
    client = _client(tmp_path, monkeypatch)

    resp = client.head(f"/sites/{SLUG}/.fixture.json")
    assert resp.status_code == 404
    assert resp.headers.get("X-Meridian-Error-Code") == "E_NOT_FOUND"
    assert resp.content == b""


# ── 3. the refusal is a SERVING decision: the markers stay on disk ───────────────────────────
def test_the_marker_survives_every_refusal(tmp_path: Path, monkeypatch) -> None:
    directory = _site_root(tmp_path)
    client = _client(tmp_path, monkeypatch)

    for path in (
        f"/sites/{SLUG}/.fixture.json",
        f"/sites/{SLUG}/debug-notes.json",
        f"/sites/{SLUG}/.internal/secret.txt",
    ):
        assert client.get(path).status_code == 404, path

    assert (directory / ".fixture.json").is_file(), "the marker must stay on disk (owner disposition)"
    assert json.loads((directory / ".fixture.json").read_text(encoding="utf-8"))["note"] == MARKER_NOTE
    assert (directory / "debug-notes.json").is_file()
    assert (directory / ".internal" / "secret.txt").is_file()


# ── 4. the pure decision, including traversal shapes the URL layer normalises ─────────────────
@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (".fixture.json", "dotfile"),
        (f"{SLUG}/.fixture.json", "dotfile"),
        (f"{SLUG}/.internal/x", "dotfile"),
        # `..` is normalised by Starlette's get_path before this function sees it; the raw shapes
        # still resolve to a dotfile component here, so the decision is safe on its own.
        (f"{SLUG}/../.fixture.json", "dotfile"),
        ("..\\..\\.fixture.json", "dotfile"),
        (f"{SLUG}/debug.json", "not_a_site_file"),
        (f"{SLUG}/meta.json/extra", "not_a_site_file"),
        (f"{SLUG}/sub/index.html", "not_a_site_file"),
        # The generator's own shapes are allowed.
        (f"{SLUG}", None),
        ("", None),
        (".", None),
        (f"{SLUG}/index.html", None),
        (f"{SLUG}/about.html", None),
        (f"{SLUG}/contact.html", None),
        (f"{SLUG}/styles.css", None),
        (f"{SLUG}/sitemap.xml", None),
        (f"{SLUG}/meta.json", None),
    ],
)
def test_the_serving_decision_is_pure_and_pinned(path: str, reason: str | None) -> None:
    from app.core.site_static import site_path_refusal

    assert site_path_refusal(path) == reason


# ── 5. the allowlist and the generator's output are pinned together ──────────────────────────
def test_the_served_file_set_is_exactly_what_the_generator_writes() -> None:
    """The serving allowlist is a deliberate list, so drift between it and the renderer fails HERE.

    `site_service.PAGES` + `STYLES_FILE` + `SITEMAP_FILE` are the files `generate_site` writes, and
    `meta.json` is the record every site directory carries (the same file the storefront contract §9
    says the mount serves).  A new generated file must be added to `SERVED_SITE_FILES` in the same
    change that starts writing it — otherwise the next site 404s on a page the owner can see was
    written.
    """
    from app.core.site_static import SERVED_SITE_FILES

    generated = set(site_service.PAGES) | {
        site_service.STYLES_FILE,
        site_service.SITEMAP_FILE,
        "meta.json",
    }
    assert set(SERVED_SITE_FILES) == generated
    assert len(SERVED_SITE_FILES) == len(set(SERVED_SITE_FILES)), "no duplicate entries"
