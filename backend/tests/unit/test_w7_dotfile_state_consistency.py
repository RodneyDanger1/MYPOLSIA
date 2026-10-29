"""WAVE 7 / logic-debugger — the WAVE-6 DOTFILE REFUSAL: does the serving decision change any state?

Job 3's second half.  The wave-6 fix (`app/core/site_static.py`, F-39) turned the bare `/sites`
`StaticFiles` mount into an allowlist that answers a NAMED 404 for a dotfile / non-page path.  Its
own suite pins WHAT is refused and that the markers stay on disk.  This file asks the STATE-MACHINE
questions that a refusal introduces:

  D1  a batch of refusals (GET, HEAD, encoded dotfile, dot-directory, non-page) leaves the whole
      site directory BYTE-IDENTICAL — no file created, deleted, rewritten or chmod'ed
  D2  a repeated refusal is DETERMINISTIC (same status/code/reason/body) and never turns into a
      served body on a retry (no cached "first request wins" state)
  D3  a URL-encoded dotfile (`%2Efixture.json`) cannot walk past the allowlist
  D4  serving a real page before and after a refusal returns the SAME bytes (the refusal cannot
      poison the page path)
  D5  the refusal is not an EXISTENCE ORACLE: a present non-page file and an absent one produce the
      same envelope (same rule, same message, same served-file list)

No product file is edited by this lane's wave 7: the wave-6 bytes are what is being verified.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.services import site_service

SLUG = "state-probe"
MARKER_NOTE = "owner-data marker kept on purpose (wave-6 disposition)"
GENERATED = {
    "index.html": "<!doctype html><html><body><h1>State probe</h1></body></html>",
    "about.html": "<!doctype html><html><body><h1>About</h1></body></html>",
    "styles.css": "body { color: #111; }",
    "sitemap.xml": '<?xml version="1.0"?><urlset><url><loc>http://test/sites/x/</loc></url></urlset>',
    "meta.json": json.dumps({"slug": SLUG, "title": "State probe"}),
}


def _site(tmp_path: Path) -> Path:
    root = tmp_path / "sites"
    directory = root / SLUG
    directory.mkdir(parents=True)
    for name, content in GENERATED.items():
        (directory / name).write_text(content, encoding="utf-8")
    (directory / ".fixture.json").write_text(json.dumps({"note": MARKER_NOTE}), encoding="utf-8")
    (directory / "debug-notes.json").write_text('{"debug": true}', encoding="utf-8")
    (directory / ".internal").mkdir()
    (directory / ".internal" / "secret.txt").write_text("SECRET-INTERNAL-BYTES", encoding="utf-8")
    return directory


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    from app.main import create_app

    monkeypatch.setattr(site_service, "SITES_ROOT", tmp_path / "sites")
    return TestClient(create_app())


def _tree_state(directory: Path) -> dict[str, tuple[str, int]]:
    """Every entry under `directory`: files by (sha256, size), directories as ("<dir>", 0)."""
    state: dict[str, tuple[str, int]] = {}
    for path in sorted(directory.rglob("*")):
        rel = str(path.relative_to(directory)).replace("\\", "/")
        if path.is_dir():
            state[rel] = ("<dir>", 0)
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            state[rel] = (digest, path.stat().st_size)
    return state


# ── D1: refusals change no state on disk ────────────────────────────────────
def test_d1_a_batch_of_refusals_leaves_the_site_directory_byte_identical(tmp_path, monkeypatch):
    directory = _site(tmp_path)
    client = _client(tmp_path, monkeypatch)
    before = _tree_state(directory)

    paths = [
        f"/sites/{SLUG}/.fixture.json",
        f"/sites/{SLUG}/.internal/secret.txt",
        f"/sites/{SLUG}/.internal/",
        f"/sites/{SLUG}/debug-notes.json",
        f"/sites/{SLUG}/notes.txt",
        f"/sites/{SLUG}/%2Efixture.json",
    ]
    for path in paths:
        first = client.get(path)
        assert first.status_code == 404, (path, first.status_code)
        again = client.get(path)
        assert again.status_code == 404, (path, again.status_code)
        head = client.head(path)
        assert head.status_code == 404 and head.content == b"", path

    after = _tree_state(directory)
    assert after == before, "a refusal is a serving decision: nothing on disk may change"
    assert (directory / ".fixture.json").is_file()
    assert json.loads((directory / ".fixture.json").read_text(encoding="utf-8"))["note"] == MARKER_NOTE


# ── D2: deterministic refusal, never a served body on retry ─────────────────
def test_d2_a_repeated_refusal_is_deterministic_and_never_serves_on_a_retry(tmp_path, monkeypatch):
    _site(tmp_path)
    client = _client(tmp_path, monkeypatch)

    bodies = []
    for _ in range(3):
        resp = client.get(f"/sites/{SLUG}/.fixture.json")
        assert resp.status_code == 404
        assert resp.headers.get("X-Meridian-Error-Code") == "E_NOT_FOUND"
        assert MARKER_NOTE not in resp.text, "no refusal may leak the marker body"
        bodies.append(resp.json())

    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]["error"]["context"]["reason"] == "dotfile"
    assert bodies[0]["error"]["code"] == "E_NOT_FOUND"


# ── D3: an encoded dotfile cannot walk past the allowlist ───────────────────
def test_d3_an_encoded_dotfile_is_refused(tmp_path, monkeypatch):
    directory = _site(tmp_path)
    client = _client(tmp_path, monkeypatch)

    for path in (
        f"/sites/{SLUG}/%2Efixture.json",
        f"/sites/{SLUG}/%2e%2E/{SLUG}/.fixture.json",
        f"/sites/{SLUG}/.%66ixture.json",
    ):
        resp = client.get(path)
        assert resp.status_code == 404, (path, resp.status_code, resp.text[:200])
        assert MARKER_NOTE not in resp.text, path
    # the file is still there, still unreadable over HTTP
    assert (directory / ".fixture.json").is_file()


# ── D4: a refusal cannot poison the page path ──────────────────────────────
def test_d4_real_pages_serve_the_same_bytes_around_a_refusal(tmp_path, monkeypatch):
    _site(tmp_path)
    client = _client(tmp_path, monkeypatch)

    def served() -> dict[str, tuple[int, bytes]]:
        return {
            name: (r.status_code, r.content)
            for name, r in {
                "root": client.get(f"/sites/{SLUG}/"),
                "index": client.get(f"/sites/{SLUG}/index.html"),
                "about": client.get(f"/sites/{SLUG}/about.html"),
                "styles": client.get(f"/sites/{SLUG}/styles.css"),
                "meta": client.get(f"/sites/{SLUG}/meta.json"),
            }.items()
        }

    before = served()
    for _ in range(2):
        assert client.get(f"/sites/{SLUG}/.fixture.json").status_code == 404
    after = served()

    assert before == after
    assert after["root"][1] == GENERATED["index.html"].encode()
    assert after["about"][1] == GENERATED["about.html"].encode()
    assert after["styles"][1] == GENERATED["styles.css"].encode()
    assert json.loads(after["meta"][1])["slug"] == SLUG


# ── D5: no existence oracle ────────────────────────────────────────────────
def test_d5_the_refusal_does_not_reveal_whether_a_non_page_file_exists(tmp_path, monkeypatch):
    _site(tmp_path)
    client = _client(tmp_path, monkeypatch)

    present = client.get(f"/sites/{SLUG}/debug-notes.json")
    absent = client.get(f"/sites/{SLUG}/no-such-file.json")

    assert present.status_code == absent.status_code == 404
    present_body, absent_body = present.json(), absent.json()
    assert present_body["error"]["code"] == absent_body["error"]["code"] == "E_NOT_FOUND"
    assert present_body["error"]["context"]["reason"] == absent_body["error"]["context"]["reason"]
    assert present_body["error"]["message"].replace(
        f"/sites/{SLUG}/debug-notes.json", "<path>"
    ) == absent_body["error"]["message"].replace(f"/sites/{SLUG}/no-such-file.json", "<path>"), (
        "the message must depend on the RULE, not on the file's existence"
    )
    assert present_body["error"]["context"]["served_files"] == absent_body["error"]["context"][
        "served_files"
    ]
