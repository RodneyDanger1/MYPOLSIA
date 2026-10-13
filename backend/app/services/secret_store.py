"""The owner's DeepSeek API key: durable storage, masking, and a real verify probe.

WHY A FILE, NOT A TABLE
-----------------------
The frozen interface (`get_deepseek_api_key` / `set_deepseek_api_key` /
`clear_deepseek_api_key` / `deepseek_key_status`) needs one value.  This tree has no
key/value settings table (17 tables, none of them KV — see
`tests/unit/test_models_schema_frozen.py`), so a DB-backed store would mean a new
model + an alembic revision + a deliberate re-pin of the frozen DDL contract, all for
a single string.  The atomic file store below is the smaller, honest move, and it
keeps working when Postgres is down — which is exactly when a fresh install needs
onboarding.

WHERE IT LIVES
--------------
`settings.deepseek_key_file` (env override `DEEPSEEK_KEY_FILE`), default
`data/deepseek_api_key.json` resolved against the backend process working directory:
`backend/data/deepseek_api_key.json` locally, `/app/data/deepseek_api_key.json` in
the image (`backend/Dockerfile` sets `WORKDIR /app`).  The file is JSON, written
temp-file-then-`os.replace` (`O_CREAT|O_TRUNC`, mode 0600), and `chmod 0600` is
applied best-effort — on Windows `os.chmod` only toggles the read-only bit and the
file inherits the ACL of the user directory it lives under; that is stated, not
papered over.  A corrupt or unreadable file raises `E_STORAGE`, never a silent
fall-back to the environment (silently reporting "env" for a stored-but-broken key
would blame the wrong credential).

PRECEDENCE
----------
stored value > `DEEPSEEK_API_KEY`.  The env fallback reads the process environment
first (that is what docker-compose `env_file:` and an exported shell give), then the
`Settings.deepseek_api_key` field, which is how an entry in `backend/.env` arrives
(pydantic-settings loads the file but does not put it in `os.environ`).  The stored
value always wins: the whole point of the UI is that a key typed in the app overrides
deployment state.

THE CACHE
---------
Reads are cached in-process, keyed by resolved path, and invalidated by
`set_deepseek_api_key` / `clear_deepseek_api_key` (the only writers this process knows
about).  An edit of the file made by some other process is therefore not observed
until the next set/clear or process start — documented here rather than hidden behind
a stat() on every read.

In-process coherence (RD13): the write path (temp file -> `os.replace` -> cache update)
and the read-and-cache path share ONE lock, so two racing writers can no longer leave
the cache serving a value the file does not hold (the last completed write is the last
cache update), and a reader cannot cache a pre-write value after a completed write.
The file itself was always safe (the temp-file + atomic-replace dance), the cache was not.

CORRUPT vs EMPTY
----------------
Corrupt = invalid JSON, a JSON document that is not an object, or a value under
`deepseek_api_key` that is neither a string nor null.  Those raise `E_STORAGE` (never a
silent env fallback).  A missing key entry, an explicit JSON `null` or a blank string is
"no usable stored value": the environment fallback applies, exactly as if nothing had
been stored — a blank cannot be a credential, so it cannot be the "wrong credential"
the refusal above protects against.

THE KEY IS NEVER LOGGED
-----------------------
Nothing in this module logs a key (only names and outcomes), `deepseek_key_status`
returns a masked form, `verify_deepseek_key` scrubs the key out of every string it
returns, and the routes never echo the request body.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx

from app.config import settings
from app.core.errors import E_STORAGE, E_VALIDATION, ERROR_HINTS, PolsiaError

logger = logging.getLogger(__name__)

#: The environment variable the frozen interface names as the fallback source.
DEEPSEEK_ENV_VAR = "DEEPSEEK_API_KEY"

#: Where the stored value persists when no explicit path is configured. Relative to
#: the backend process's cwd on purpose: `backend/data/…` in the repo, `/app/data/…`
#: in the Docker image, with no per-environment branch in this code.
DEFAULT_KEY_FILE = "data/deepseek_api_key.json"

#: Override for the store path; also a `Settings` field (pydantic-settings reads the
#: same spelling), so a `.env` entry works in local dev and an exported var works in
#: docker-compose (whose `env_file:` exports the whole file into the container env).
KEY_FILE_ENV_VAR = "DEEPSEEK_KEY_FILE"

#: Zero-token-cost verification probe: listing models consumes no tokens. Documented
#: at https://api-docs.deepseek.com/api/list-models (curl: GET /models with a Bearer token).
DEEPSEEK_MODELS_URL = "https://api.deepseek.com/models"
VERIFY_TIMEOUT_SECONDS = 15.0

_SOURCE_STORED: Literal["stored"] = "stored"
_SOURCE_ENV: Literal["env"] = "env"
_SOURCE_NONE: Literal["none"] = "none"

#: RD13 sweep find: `ERROR_HINTS` (app/core/errors.py) has NO `E_STORAGE` entry, so the two
#: OSError handlers below used to raise `KeyError: 'E_STORAGE'` instead of the documented named
#: refusal — a raw crash on the unreadable-file path, and a generic 500 on a failed clear().
#: `.get` with a real hint keeps the refusal honest today AND picks up the canonical entry the
#: moment it lands in the core table. (REQUIRED-FOLLOW-UP: add E_STORAGE to ERROR_HINTS.)
_STORAGE_HINT = ERROR_HINTS.get(
    E_STORAGE,
    "check the file's permissions, or delete the store file and re-enter the key in Settings",
)

#: resolved-path -> stored value (None = "no usable stored value").  Cache-keying by
#: path (not a single slot) means a test or a re-configuration pointing at a different
#: file can never be served another file's cached value.
_FILE_CACHE: dict[str, str | None] = {}

#: Serializes (a) set's temp-write -> replace -> chmod -> cache update, (b) clear's
#: unlink -> cache update, and (c) the read-file-and-cache path below.  One lock, so
#: every cache transition happens in the same order as the file transition it mirrors;
#: without it two racing set()s could leave the cache serving the loser's value after
#: the winner's replace (RD13 — see module docstring, "In-process coherence").
_WRITE_LOCK = threading.Lock()


def _resolve_key_file() -> Path:
    """The active store path: explicit override > Settings field > documented default."""
    raw = (
        os.environ.get(KEY_FILE_ENV_VAR)
        or getattr(settings, "deepseek_key_file", "")
        or DEFAULT_KEY_FILE
    )
    return Path(raw).expanduser()


def _env_key() -> str:
    """`DEEPSEEK_API_KEY` from the process env, else from Settings (a `.env` file)."""
    raw = os.environ.get(DEEPSEEK_ENV_VAR) or getattr(settings, "deepseek_api_key", "")
    return raw.strip() if isinstance(raw, str) else ""


def mask_secret(value: str) -> str:
    """A display-only form of a secret: never enough to reconstruct it.

    `sk-…1234` for anything long enough that a 3-char prefix plus 4 trailing chars
    still leaves the middle hidden; shorter values get only the trailing hint or the
    bare ellipsis.  The threshold (12) guarantees prefix+tail < len(value), so the
    masked form can never equal the secret it masks.
    """
    if len(value) >= 12:
        return f"{value[:3]}…{value[-4:]}"
    if len(value) > 4:
        return f"…{value[-4:]}"
    return "…"


def _stored_key(path: Path) -> str | None:
    """The value persisted at `path` (cached per path), or None when nothing is stored.

    The read-and-cache step runs under `_WRITE_LOCK` with a double-check after acquiring
    it, so a reader that started during a write either sees the pre-write cache entry or
    waits and sees the completed write — it can never cache a value the file no longer
    holds (RD13).  Corruption is refused by NAME (`E_STORAGE`): invalid JSON, a JSON
    document that is not an object, or a non-string value under the key (see the module
    docstring, "CORRUPT vs EMPTY").
    """
    cache_key = str(path)
    if cache_key in _FILE_CACHE:
        return _FILE_CACHE[cache_key]
    with _WRITE_LOCK:
        if cache_key in _FILE_CACHE:  # another thread cached it while we waited
            return _FILE_CACHE[cache_key]
        value: str | None = None
        try:
            if path.is_file():
                raw_document = path.read_text(encoding="utf-8")
                try:
                    document = json.loads(raw_document)
                except ValueError as exc:
                    # The key file exists but is not the JSON this module wrote — refuse
                    # loudly instead of falling back to env (see module docstring).
                    raise PolsiaError(
                        E_STORAGE,
                        f"the stored DeepSeek key file is not valid JSON: {type(exc).__name__}",
                        how_to_fix=(
                            "delete the file and re-enter the key in Settings "
                            f"({path})"
                        ),
                        context={"path": str(path)},
                    ) from exc
                if not isinstance(document, dict):
                    # Valid JSON of the wrong shape (a list, a bare string/number) was not
                    # written by this module; treating it as "nothing stored" would silently
                    # fall back to env and blame the wrong credential (module docstring).
                    raise PolsiaError(
                        E_STORAGE,
                        "the stored DeepSeek key file is not a JSON object "
                        f"(found {type(document).__name__})",
                        how_to_fix=(
                            "delete the file and re-enter the key in Settings "
                            f"({path})"
                        ),
                        context={"path": str(path)},
                    )
                candidate = document.get("deepseek_api_key")
                if candidate is None:
                    # Key entry absent, or an explicit JSON null: nothing stored; the env
                    # fallback below applies (documented "CORRUPT vs EMPTY").
                    value = None
                elif not isinstance(candidate, str):
                    raise PolsiaError(
                        E_STORAGE,
                        "the stored DeepSeek key file carries a non-string value "
                        f"(found {type(candidate).__name__})",
                        how_to_fix=(
                            "delete the file and re-enter the key in Settings "
                            f"({path})"
                        ),
                        context={"path": str(path)},
                    )
                elif candidate.strip():
                    value = candidate.strip()
                # else: blank string -> no usable stored value; env fallback applies.
        except OSError as exc:
            raise PolsiaError(
                E_STORAGE,
                f"could not read the stored DeepSeek key file: {type(exc).__name__}: {exc}",
                how_to_fix=_STORAGE_HINT,
                context={"path": str(path)},
            ) from exc
        _FILE_CACHE[cache_key] = value
        return value


def _best_effort_chmod(path: Path) -> None:
    """Ask for 0600, honestly. On Windows this only clears the read-only bit; the file
    still inherits the (user-scoped) ACL of the directory it lives in."""
    try:
        os.chmod(path, 0o600)
    except OSError as exc:  # noqa: BLE001 -- permission tightening is best-effort
        logger.warning("could not tighten permissions on %s: %s", path, type(exc).__name__)


def get_deepseek_api_key() -> str | None:
    """The configured key: stored value first, then `DEEPSEEK_API_KEY`, else None."""
    stored = _stored_key(_resolve_key_file())
    if stored:
        return stored
    return _env_key() or None


def set_deepseek_api_key(value: str) -> None:
    """Persist `value` (stripped) durably and invalidate the cache. Blank is refused."""
    cleaned = (value or "").strip()
    if not cleaned:
        raise PolsiaError(
            E_VALIDATION,
            "the DeepSeek API key must not be blank",
            how_to_fix="paste the key from https://platform.deepseek.com/api_keys before saving",
            context={"field": "api_key"},
        )
    path = _resolve_key_file()
    payload = json.dumps({"deepseek_api_key": cleaned})
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        # RD13: the whole publish sequence is atomic w.r.t. other writers IN THIS PROCESS —
        # the thread that replaces the file last is also the one that updates the cache last,
        # so `get_deepseek_api_key()` can never settle on a racing loser's value.
        with _WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(handle, payload.encode("utf-8"))
            finally:
                os.close(handle)
            os.replace(temp, path)
            _best_effort_chmod(path)
            _FILE_CACHE[str(path)] = cleaned  # invalidate: next read sees the new value
    except OSError as exc:
        # Leave no orphan temp file behind, and keep the previous value live: the
        # cache is updated only after the replace succeeded.
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise PolsiaError(
            E_STORAGE,
            f"could not store the DeepSeek API key: {type(exc).__name__}: {exc}",
            how_to_fix=(
                "make sure the data directory is writable "
                f"({path.parent}) and try again"
            ),
            context={"path": str(path)},
        ) from exc
    logger.info("stored a new DeepSeek API key (len=%d)", len(cleaned))


def clear_deepseek_api_key() -> None:
    """Remove the stored value (an env key may still configure the runtime)."""
    path = _resolve_key_file()
    try:
        with _WRITE_LOCK:  # RD13: same critical section as set() — see _WRITE_LOCK
            path.unlink(missing_ok=True)
            _FILE_CACHE[str(path)] = None  # invalidate: next read falls through to env/None
    except OSError as exc:
        raise PolsiaError(
            E_STORAGE,
            f"could not remove the stored DeepSeek key file: {type(exc).__name__}: {exc}",
            how_to_fix=_STORAGE_HINT,
            context={"path": str(path)},
        ) from exc
    logger.info("cleared the stored DeepSeek API key")


def deepseek_key_status() -> dict[str, Any]:
    """`{"configured": bool, "source": "stored"|"env"|"none", "masked": str}`.

    `masked` is a display form of whichever key is live (or `…`), never the key.
    """
    stored = _stored_key(_resolve_key_file())
    if stored:
        return {"configured": True, "source": _SOURCE_STORED, "masked": mask_secret(stored)}
    env = _env_key()
    if env:
        return {"configured": True, "source": _SOURCE_ENV, "masked": mask_secret(env)}
    return {"configured": False, "source": _SOURCE_NONE, "masked": "…"}


def _scrub(text: str, secret: str) -> str:
    """Defense in depth: no returned string may contain the candidate key verbatim."""
    if secret and secret in text:
        return text.replace(secret, "(key hidden)")
    return text


def _api_error_message(response: httpx.Response) -> str:
    """The provider's own error line, bounded — used for honest `detail` strings."""
    try:
        document = response.json()
    except ValueError:
        document = None
    if isinstance(document, dict):
        error = document.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:200]
        if isinstance(error, str):
            return error[:200]
        if isinstance(document.get("message"), str):
            return document["message"][:200]
    return (response.text or "").strip().replace("\n", " ")[:200]


async def verify_deepseek_key(candidate: str | None = None) -> dict[str, Any]:
    """A REAL probe of `candidate` (or the configured key): `GET /models` with Bearer auth.

    Listing models costs no tokens (documented pricing: tokens are billed per request
    content; this call sends none), so it is the cheapest honest way to prove a key
    authenticates.  The result NEVER fabricates: no key → ok:false, transport failure
    → ok:false with the real exception, HTTP failure → ok:false with the provider's own
    status and message.  Every returned string is scrubbed of the key.
    """
    key = (candidate or "").strip() or get_deepseek_api_key() or ""
    if not key:
        return {
            "ok": False,
            "detail": (
                "nothing to verify: no key is stored and "
                f"{DEEPSEEK_ENV_VAR} is not set"
            ),
        }
    try:
        async with httpx.AsyncClient(timeout=VERIFY_TIMEOUT_SECONDS) as client:
            response = await client.get(
                DEEPSEEK_MODELS_URL,
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            )
    except httpx.TimeoutException:
        return {
            "ok": False,
            "detail": f"timed out after {VERIFY_TIMEOUT_SECONDS:g}s calling GET {DEEPSEEK_MODELS_URL}",
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "detail": _scrub(
                f"could not reach {DEEPSEEK_MODELS_URL}: {type(exc).__name__}: {exc}", key
            ),
        }

    if response.status_code == 200:
        model_ids: list[str] = []
        try:
            document = response.json()
        except ValueError:
            document = None
        if isinstance(document, dict) and isinstance(document.get("data"), list):
            model_ids = [
                str(entry["id"])
                for entry in document["data"]
                if isinstance(entry, dict) and entry.get("id")
            ]
        if model_ids:
            return {
                "ok": True,
                "detail": (
                    f"HTTP 200 from GET {DEEPSEEK_MODELS_URL} — key accepted; "
                    f"{len(model_ids)} model(s) available: {', '.join(model_ids[:5])}"
                ),
            }
        return {
            "ok": True,
            "detail": f"HTTP 200 from GET {DEEPSEEK_MODELS_URL} — key accepted",
        }

    detail = f"HTTP {response.status_code} from GET {DEEPSEEK_MODELS_URL}"
    message = _api_error_message(response)
    if message:
        detail = f"{detail}: {message}"
    return {"ok": False, "detail": _scrub(detail, key)}


__all__ = [
    "DEEPSEEK_ENV_VAR",
    "DEEPSEEK_MODELS_URL",
    "DEFAULT_KEY_FILE",
    "KEY_FILE_ENV_VAR",
    "clear_deepseek_api_key",
    "deepseek_key_status",
    "get_deepseek_api_key",
    "mask_secret",
    "set_deepseek_api_key",
    "verify_deepseek_key",
]
