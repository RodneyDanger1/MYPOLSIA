"""V7-T06 — the cache observes out-of-process rotations (meta-code GAP-FILL O2).

Contract under test (frozen by the wave plan): `_stored_key` records, alongside each cached
value, the file identity it observed — `(st_mtime_ns, st_size)`, or an explicit absent
observation for a missing file — and re-checks it with ONE stat per read.  A rewrite or a
deletion made by ANOTHER process (the API rotating the key while a worker keeps its cache)
is therefore observed by the very next read; the RD13 lock discipline and the E_STORAGE
corruption refusals are unchanged.

Raw RED evidence (legs (b), (c) and (d) fail on the pre-fix code):
`raw/v7-red-tests.txt` in the lane dir.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.core.errors import E_STORAGE, PolsiaError
from app.services import secret_store

#: Clearly fake values; shaped like real keys so masking behaves as in production.
FAKE_OLD = "sk-v7-rotation-old-000000000000000000"
FAKE_NEW = "sk-v7-rotation-new-1111111111111111111"
FAKE_ENV = "sk-v7-rotation-env-2222222222222222222"


@pytest.fixture
def key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at a scratch file and clear the env fallback for the test."""
    path = tmp_path / "deepseek_api_key.json"
    monkeypatch.setenv(secret_store.KEY_FILE_ENV_VAR, str(path))
    monkeypatch.delenv(secret_store.DEEPSEEK_ENV_VAR, raising=False)
    return path


def _rotate(path: Path, value: str | None) -> None:
    """Simulate the OTHER process: rewrite the store (or delete it), mtime explicitly newer.

    `os.utime` with an explicit +5s `st_mtime_ns` defeats coarse filesystem/clock resolution,
    so a same-tick write cannot masquerade as "no change" (the brief's rule).
    """
    if value is None:
        path.unlink()
        return
    path.write_text(json.dumps({"deepseek_api_key": value}), encoding="utf-8")
    st = path.stat()
    bumped = st.st_mtime_ns + 5_000_000_000
    os.utime(path, ns=(bumped, bumped))


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Wrap `Path.read_text` with a call counter (the test double stays inside the test)."""
    calls: list[Path] = []
    real = Path.read_text

    def counting(self: Path, *args: object, **kwargs: object) -> str:
        calls.append(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)
    return calls


# ── (a) an unchanged file between two reads is served from cache ─────────────────────────────


def test_an_unchanged_file_is_served_from_cache(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file.write_text(json.dumps({"deepseek_api_key": FAKE_OLD}), encoding="utf-8")
    calls = _count_reads(monkeypatch)

    assert secret_store.get_deepseek_api_key() == FAKE_OLD  # cold cache: one real read
    assert secret_store.get_deepseek_api_key() == FAKE_OLD  # warm cache: served from cache
    assert calls == [key_file], "the second read must be served from cache, not re-read"


# ── (b) THE RED LEG: a rotation by another process is observed ───────────────────────────────


def test_another_process_rotation_is_observed_on_the_next_read(key_file: Path) -> None:
    key_file.write_text(json.dumps({"deepseek_api_key": FAKE_OLD}), encoding="utf-8")
    assert secret_store.get_deepseek_api_key() == FAKE_OLD

    _rotate(key_file, FAKE_NEW)

    assert secret_store.get_deepseek_api_key() == FAKE_NEW, (
        "the next read must observe the rewritten file, not serve the stale cache"
    )
    status = secret_store.deepseek_key_status()
    assert status["source"] == "stored"
    assert status["masked"] == secret_store.mask_secret(FAKE_NEW)


# ── (c) a deletion by another process is observed; env/None fallback applies ─────────────────


def test_another_process_deletion_is_observed_then_env_fallback_applies(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_store.set_deepseek_api_key(FAKE_OLD)  # warm cache with a STORED value
    _rotate(key_file, None)

    assert secret_store.get_deepseek_api_key() is None
    assert secret_store.deepseek_key_status()["source"] == "none"

    monkeypatch.setenv(secret_store.DEEPSEEK_ENV_VAR, FAKE_ENV)
    assert secret_store.get_deepseek_api_key() == FAKE_ENV
    assert secret_store.deepseek_key_status()["source"] == "env"


# ── (d) the refresh path keeps the named E_STORAGE refusals unchanged ────────────────────────


def test_rotation_into_corruption_is_observed_and_refused_by_name(key_file: Path) -> None:
    secret_store.set_deepseek_api_key(FAKE_OLD)
    key_file.write_text("this is not json", encoding="utf-8")
    st = key_file.stat()
    os.utime(key_file, ns=(st.st_mtime_ns + 5_000_000_000,) * 2)

    with pytest.raises(PolsiaError) as excinfo:
        secret_store.get_deepseek_api_key()
    assert excinfo.value.code == E_STORAGE
    assert str(key_file) in excinfo.value.how_to_fix


def test_set_primes_the_cache_with_its_own_identity(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """set() still publishes + caches atomically: the next read costs no file read (RD13)."""
    secret_store.set_deepseek_api_key(FAKE_OLD)
    calls = _count_reads(monkeypatch)

    assert secret_store.get_deepseek_api_key() == FAKE_OLD
    assert calls == [], "set() must leave the cache serving exactly the value it published"
