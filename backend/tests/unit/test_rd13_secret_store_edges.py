"""RD13 — adversarial edge sweep for the frozen `secret_store` interface.

Decisions pinned here (see the module docstring, sections "CORRUPT vs EMPTY" and
"In-process coherence"):

* corrupt = invalid/truncated JSON, a JSON document that is not an object, or a non-string
  value under `deepseek_api_key` -> `E_STORAGE`, NEVER a silent env fallback (silently
  reporting `source=env` would blame the credential the operator did not configure);
* key absent / explicit JSON `null` / blank string = "no usable stored value" -> env fallback
  (`_FILE_CACHE`'s own `None` semantics);
* an unreadable file is an honest `E_STORAGE` refusal too;
* staleness caused by ANOTHER process editing the file is documented behaviour, refreshed by
  the next set/clear (and by process start) — pinned as such, not "fixed";
* racing in-process writers keep the FILE atomic (temp + os.replace) AND the cache coherent
  with it (the RD13 lock): after the dust settles, `get_deepseek_api_key()` returns exactly
  what the file holds.

No network here (verify's socket is stubbed in `test_secret_store.py`).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from app.core.errors import E_STORAGE, E_VALIDATION, PolsiaError
from app.services import secret_store

FAKE_KEY = "sk-rd13-stored-key-0001"
FAKE_ENV_KEY = "sk-rd13-env-key-0002"


@pytest.fixture
def key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at a scratch file and clear the env fallback for the test."""
    path = tmp_path / "deepseek_api_key.json"
    monkeypatch.setenv(secret_store.KEY_FILE_ENV_VAR, str(path))
    monkeypatch.delenv(secret_store.DEEPSEEK_ENV_VAR, raising=False)
    return path


# ── corrupt shapes: E_STORAGE, never a silent env fallback ───────────────────────────────────


@pytest.mark.parametrize("document", [
    "this is not json at all",
    '{"deepseek_api_key": "sk-truncated',        # truncated mid-write
    "[1, 2, 3]",                                 # JSON, wrong top-level type
    '"a bare JSON string"',
    "42",
    "true",
    "null",                                      # a document that is not an object
    '{"deepseek_api_key": 123}',                 # right shape, wrong value type
    '{"deepseek_api_key": ["a", "b"]}',
    '{"deepseek_api_key": {"nested": 1}}',
    '{"deepseek_api_key": false}',
])
def test_corrupt_store_documents_raise_E_STORAGE_not_a_silent_env_fallback(
    key_file: Path, monkeypatch: pytest.MonkeyPatch, document: str
) -> None:
    key_file.write_text(document, encoding="utf-8")
    monkeypatch.setenv(secret_store.DEEPSEEK_ENV_VAR, FAKE_ENV_KEY)

    with pytest.raises(PolsiaError) as excinfo:
        secret_store.get_deepseek_api_key()
    assert excinfo.value.code == E_STORAGE
    assert str(key_file) in excinfo.value.how_to_fix  # the fix hint names the file to delete
    assert excinfo.value.how_to_fix  # ... and it is a real hint, not a KeyError('E_STORAGE')

    # The masked status refuses the same way instead of claiming "env" (wrong credential).
    with pytest.raises(PolsiaError) as excinfo:
        secret_store.deepseek_key_status()
    assert excinfo.value.code == E_STORAGE


@pytest.mark.parametrize("document", [
    "{}",                                        # key entry absent
    '{"deepseek_api_key": null}',                # explicit JSON null
    '{"deepseek_api_key": ""}',                  # blank string
    '{"deepseek_api_key": "   "}',
])
def test_no_usable_stored_value_shapes_fall_back_to_env(
    key_file: Path, monkeypatch: pytest.MonkeyPatch, document: str
) -> None:
    """A blank/null/absent value cannot be a credential: the env fallback applies, exactly as
    if nothing had been stored (set() itself refuses blanks, so this module never writes one)."""
    key_file.write_text(document, encoding="utf-8")
    monkeypatch.setenv(secret_store.DEEPSEEK_ENV_VAR, FAKE_ENV_KEY)

    assert secret_store.get_deepseek_api_key() == FAKE_ENV_KEY
    status = secret_store.deepseek_key_status()
    assert status["source"] == "env" and status["configured"] is True


def test_no_usable_stored_value_without_env_is_honestly_none(key_file: Path) -> None:
    key_file.write_text('{"deepseek_api_key": null}', encoding="utf-8")
    assert secret_store.get_deepseek_api_key() is None
    assert secret_store.deepseek_key_status() == {
        "configured": False, "source": "none", "masked": "…",
    }


def test_extra_fields_beside_the_key_are_tolerated(key_file: Path) -> None:
    """Forward-compatible read: only the named key is consulted, extra fields do not corrupt."""
    key_file.write_text(
        json.dumps({"deepseek_api_key": FAKE_KEY, "note": "hand-written"}), encoding="utf-8"
    )
    assert secret_store.get_deepseek_api_key() == FAKE_KEY


def test_unreadable_file_is_an_honest_E_STORAGE_refusal(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file.write_text(json.dumps({"deepseek_api_key": FAKE_KEY}), encoding="utf-8")
    monkeypatch.setenv(secret_store.DEEPSEEK_ENV_VAR, FAKE_ENV_KEY)

    def _denied(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError(13, "Access is denied", str(self))

    monkeypatch.setattr(Path, "read_text", _denied)

    with pytest.raises(PolsiaError) as excinfo:
        secret_store.get_deepseek_api_key()
    assert excinfo.value.code == E_STORAGE
    assert "PermissionError" in excinfo.value.message
    # RD13: this used to be `KeyError: 'E_STORAGE'` — ERROR_HINTS had no E_STORAGE entry.
    assert excinfo.value.how_to_fix

    with pytest.raises(PolsiaError) as excinfo:
        secret_store.deepseek_key_status()
    assert excinfo.value.code == E_STORAGE


def test_failed_clear_is_a_named_E_STORAGE_refusal_not_a_KeyError(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_store.set_deepseek_api_key(FAKE_KEY)

    def _denied(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError(5, "simulated unlink failure", str(self))

    monkeypatch.setattr(Path, "unlink", _denied)

    with pytest.raises(PolsiaError) as excinfo:
        secret_store.clear_deepseek_api_key()
    assert excinfo.value.code == E_STORAGE
    assert excinfo.value.how_to_fix
    # The store file is still there — the refusal did not half-clear anything.
    assert key_file.exists()
    assert json.loads(key_file.read_text(encoding="utf-8"))["deepseek_api_key"] == FAKE_KEY


# ── documented staleness (another process edits the file) ────────────────────────────────────


def test_file_missing_under_a_warm_cache_serves_the_cache_until_set_or_clear(
    key_file: Path,
) -> None:
    secret_store.set_deepseek_api_key(FAKE_KEY)
    key_file.unlink()  # out-of-band removal: documented as unobserved until set/clear

    assert secret_store.get_deepseek_api_key() == FAKE_KEY
    assert secret_store.deepseek_key_status()["source"] == "stored"

    secret_store.clear_deepseek_api_key()  # the refresh point
    assert secret_store.get_deepseek_api_key() is None


def test_second_process_edit_is_not_observed_until_set_or_clear(key_file: Path) -> None:
    secret_store.set_deepseek_api_key(FAKE_KEY)
    key_file.write_text(json.dumps({"deepseek_api_key": "sk-rd13-other-process"}), encoding="utf-8")

    assert secret_store.get_deepseek_api_key() == FAKE_KEY      # documented: stale is expected

    secret_store.set_deepseek_api_key("sk-rd13-winner")          # any set refreshes
    assert secret_store.get_deepseek_api_key() == "sk-rd13-winner"
    assert json.loads(key_file.read_text(encoding="utf-8"))["deepseek_api_key"] == "sk-rd13-winner"


# ── set → remove → env fallback sequence ─────────────────────────────────────────────────────


def test_set_remove_env_fallback_sequence(key_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(secret_store.DEEPSEEK_ENV_VAR, FAKE_ENV_KEY)

    secret_store.set_deepseek_api_key(FAKE_KEY)
    assert secret_store.get_deepseek_api_key() == FAKE_KEY          # stored wins
    secret_store.clear_deepseek_api_key()
    assert secret_store.get_deepseek_api_key() == FAKE_ENV_KEY      # falls back, still configured
    assert secret_store.deepseek_key_status()["source"] == "env"

    secret_store.clear_deepseek_api_key()                           # idempotent, no error
    assert secret_store.get_deepseek_api_key() == FAKE_ENV_KEY

    monkeypatch.delenv(secret_store.DEEPSEEK_ENV_VAR)
    assert secret_store.get_deepseek_api_key() is None
    assert secret_store.deepseek_key_status()["source"] == "none"


# ── masking edge cases ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("value", "expected"), [
    ("abc", "…"),                     # 3-char: nothing is revealed at all
    ("abcd", "…"),
    ("abcde", "…bcde"),
    ("abcdefgh", "…efgh"),            # 8-char boundary (the >4 branch)
    ("abcdefghijkl", "abc…ijkl"),     # the >=12 branch
    ("🔑🔑🔑🔑🔑", "…🔑🔑🔑🔑"),          # unicode: slicing is by character, not bytes
    ("", "…"),
])
def test_masking_boundaries(value: str, expected: str) -> None:
    assert secret_store.mask_secret(value) == expected


def test_masking_never_equals_and_never_reveals_the_middle() -> None:
    for length in range(0, 40):
        value = "".join(chr(0x61 + (i % 26)) for i in range(length))
        masked = secret_store.mask_secret(value)
        assert masked != value
        if length >= 12:
            assert value[3:-4] not in masked  # prefix+ellipsis+tail leaves the middle hidden


# ── racing writers: the file stays atomic AND the cache stays coherent ───────────────────────


def test_racing_sets_leave_no_temp_file_and_one_valid_value(key_file: Path) -> None:
    """Stress a real race: 8 threads x 10 distinct writes against the real file."""
    written = [f"sk-rd13-race-{thread}-{step}" for thread in range(8) for step in range(10)]
    errors: list[BaseException] = []

    def writer(value: str) -> None:
        try:
            secret_store.set_deepseek_api_key(value)
        except BaseException as exc:  # record, never swallow
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(value,)) for value in written]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)

    on_disk = json.loads(key_file.read_text(encoding="utf-8"))
    assert on_disk["deepseek_api_key"] in written          # exactly one writer's value, no interleave
    leftovers = [p.name for p in key_file.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    # Coherence: every completed set() is visible to the very next read.
    assert secret_store.get_deepseek_api_key() == on_disk["deepseek_api_key"]


def test_two_writers_race_cache_and_file_stay_coherent(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic reproduction of the loser-writes-cache-last interleaving.

    Thread A replaces the file first but is held (via a gated os.replace) until thread B has
    written its cache entry; A then writes ITS cache entry last. Without the RD13 lock the cache
    keeps A's value while the file holds B's, and `get_deepseek_api_key()` serves a key that is
    no longer the persisted one (and would not be the one a restarted worker picks up). With the
    lock the two writers serialize: the thread that replaces the file last also caches last.
    """
    real_replace = os.replace
    a_replaced = threading.Event()
    cache_written = threading.Event()

    class _RecordingCache(dict):
        def __setitem__(self, cache_key: str, value: str | None) -> None:
            super().__setitem__(cache_key, value)
            cache_written.set()

    monkeypatch.setattr(secret_store, "_FILE_CACHE", _RecordingCache())

    def gated_replace(src: str, dst: str) -> None:
        if threading.current_thread().name == "writer-A":
            real_replace(src, dst)
            a_replaced.set()
            # Unlocked world: block until B has cached (B replaces + caches after this).
            # Locked world: B is waiting on _WRITE_LOCK, so this just times out.
            cache_written.wait(2.0)
        else:
            a_replaced.wait(2.0)  # B replaces only after A did
            real_replace(src, dst)

    monkeypatch.setattr(secret_store.os, "replace", gated_replace)

    errors: list[BaseException] = []

    def writer(value: str) -> None:
        try:
            secret_store.set_deepseek_api_key(value)
        except BaseException as exc:  # record, never swallow
            errors.append(exc)

    thread_a = threading.Thread(target=writer, args=("sk-rd13-A",), name="writer-A")
    thread_b = threading.Thread(target=writer, args=("sk-rd13-B",), name="writer-B")
    thread_a.start()
    thread_b.start()
    thread_a.join(10)
    thread_b.join(10)

    assert errors == []
    assert not thread_a.is_alive() and not thread_b.is_alive()

    on_disk = json.loads(key_file.read_text(encoding="utf-8"))["deepseek_api_key"]
    assert on_disk == "sk-rd13-B"  # the gate forces B's replace to be the last one
    assert secret_store.get_deepseek_api_key() == on_disk


def test_set_refuses_blank_without_touching_an_existing_file(key_file: Path) -> None:
    """Re-verified boundary: a refused write must not disturb the live value."""
    secret_store.set_deepseek_api_key(FAKE_KEY)
    with pytest.raises(PolsiaError) as excinfo:
        secret_store.set_deepseek_api_key("   ")
    assert excinfo.value.code == E_VALIDATION
    assert json.loads(key_file.read_text(encoding="utf-8"))["deepseek_api_key"] == FAKE_KEY
    assert secret_store.get_deepseek_api_key() == FAKE_KEY
