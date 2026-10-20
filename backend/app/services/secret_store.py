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
`data/deepseek_api_key.json` ANCHORED at this module — `<backend>/data/deepseek_api_key.json`
locally, and the same directory in the image, whose `WORKDIR /app` makes the anchor resolve
to `/app/data/deepseek_api_key.json` (`backend/Dockerfile`).  An explicitly configured path
(env or Settings field) is honoured verbatim — cwd-relative when it is relative, exactly as
before; only the UNCONFIGURED default never depends on the process cwd (L1-07: a launch from
another cwd used to write the owner's key to a path no `.gitignore` rule covers, and the
compose volume keyed on `backend/data` silently stopped being the store — the same hazard D7
already anchored `chroma_client._fallback_persist_dir` for).  The file is JSON, written
temp-file-then-`os.replace` (`O_CREAT|O_TRUNC`, mode 0600), then hardened best-effort:
`chmod 0600` on POSIX; on Windows — where `os.chmod` only toggles the read-only bit and
the file inherits the ACL of the user directory it lives under — the real tightening is
`icacls <file> /inheritance:r /grant:r <user>:(R,W)` (2R-05/U2-11), run after every write.
Every failing mechanism logs a warning NAMING the file (the miss is never silent); the
`.env` beside it is the operator's to tighten, and README ("Essential settings") carries
that step.  A corrupt or unreadable file raises `E_STORAGE`, never a silent
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
Reads are cached in-process, keyed by resolved path AND by the identity of the file the
value was read from: `(st_mtime_ns, st_size)`, or an explicit absent observation for a
missing file.  Every read costs ONE stat; when the identity a read observes still equals
the one cached with the value, the cache is served — otherwise the read-and-cache step
re-runs under the write lock (re-stat, re-read, re-cache).  A rewrite or a deletion made
by ANOTHER process (the API rotating the key while a worker keeps running) is therefore
observed by the very next read (V7-T06 — superseding the earlier behaviour, which was
invalidated only by this process's set/clear).  A writer that rewrites the file with a
byte-identical length AND a rolled-back `st_mtime_ns` is not distinguishable from no
change — stated, not papered over.

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
import getpass
import json
import logging
import os
import re
import subprocess
import threading
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

import httpx

from app.config import settings
from app.core.errors import E_STORAGE, E_VALIDATION, ERROR_HINTS, PolsiaError

logger = logging.getLogger(__name__)

#: The environment variable the frozen interface names as the fallback source.
DEEPSEEK_ENV_VAR = "DEEPSEEK_API_KEY"

#: Where the stored value persists when no explicit path is configured.  ANCHORED at this
#: file (`_anchored_default_key_file()` -> `<backend>/data/…`; `backend/data/…` in the repo,
#: `/app/data/…` in the Docker image), never at the process cwd — see `_resolve_key_file`.
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

#: RD13 sweep find: `ERROR_HINTS` (app/core/errors.py) once had NO `E_STORAGE` entry, so the two
#: OSError handlers below used to raise `KeyError: 'E_STORAGE'` instead of the documented named
#: refusal — a raw crash on the unreadable-file path, and a generic 500 on a failed clear().  The
#: canonical entry exists now (`errors.py: E_STORAGE = "repair or remove the local secret store
#: file (backend/data/deepseek_api_key.json) and retry"`, the same path this module's anchored
#: default resolves to), so the fallback below is inert belt-and-braces; the `.get` stays because
#: a lookup that cannot `KeyError` here is exactly what it is for.
_STORAGE_HINT = ERROR_HINTS.get(
    E_STORAGE,
    "check the file's permissions, or delete the store file and re-enter the key in Settings",
)

#: The identity of the store file as observed by a stat(): `(st_mtime_ns, st_size)`, or the
#: explicit `_ABSENT_IDENTITY` sentinel when the path did not exist.  Recorded alongside
#: each cached value, so a read can tell "the file changed under me" (V7-T06) from "current".
_FileIdentity = tuple[int, int] | Literal["absent"]
_ABSENT_IDENTITY: Literal["absent"] = "absent"

#: resolved-path -> (file identity, stored value (None = "no usable stored value")).  The
#: identity is what lets each read notice an out-of-process rewrite/deletion with ONE stat;
#: cache-keying by path (not a single slot) still means a test or a re-configuration
#: pointing at a different file can never be served another file's cached value.
_FILE_CACHE: dict[str, tuple[_FileIdentity, str | None]] = {}


def _observed_identity(path: Path) -> _FileIdentity:
    """`(st_mtime_ns, st_size)` of `path` as observed right now, or `_ABSENT_IDENTITY`.

    ONE stat per read is the accepted, documented cost of noticing out-of-process
    rotations (V7-T06): `st_mtime_ns` defeats coarse mtime resolution and `st_size` catches
    a same-tick rewrite that changed the byte length.  A path that does not exist (or whose
    parent is not a directory) is an OBSERVATION — `_ABSENT_IDENTITY` — not an error; any
    other stat failure propagates to the caller, which refuses by name.
    """
    try:
        stat = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return _ABSENT_IDENTITY
    return (stat.st_mtime_ns, stat.st_size)

#: Serializes (a) set's temp-write -> replace -> chmod -> cache update, (b) clear's
#: unlink -> cache update, and (c) the read-file-and-cache path below.  One lock, so
#: every cache transition happens in the same order as the file transition it mirrors;
#: without it two racing set()s could leave the cache serving the loser's value after
#: the winner's replace (RD13 — see module docstring, "In-process coherence").
_WRITE_LOCK = threading.Lock()


def _anchored_default_key_file() -> Path:
    """`<backend>/data/deepseek_api_key.json`, anchored at THIS file — never at the process cwd.

    The anchor is `Path(__file__).resolve().parents[2]` (app/services/secret_store.py ->
    `backend/`), the same treatment `chroma_client._fallback_persist_dir` applies to its store
    (D7).  In the Docker image the same expression resolves to `/app` (`WORKDIR /app`), so the
    container spelling of the documented default is unchanged.
    """
    return Path(__file__).resolve().parents[2] / DEFAULT_KEY_FILE


def _resolve_key_file() -> Path:
    """The active store path: explicit override > Settings field > repo-anchored default.

    An explicit `DEEPSEEK_KEY_FILE` (env or Settings field) that is not the documented default
    is honoured verbatim — `expanduser()`d, relative to the cwd it is resolved from, exactly as
    before (L1-07 keeps override semantics byte-identical).  Only the documented default value
    is repo-anchored, so `python -m uvicorn app.main:app` from the repo ROOT — the launch that
    used to write the key to `<repo>/data/…`, a path no `.gitignore` rule covers — now lands at
    `backend/data/deepseek_api_key.json`.  Compatibility: a store an earlier wrong-cwd launch
    wrote at the cwd-relative default is still read while the anchored store does not exist,
    with ONE warning naming both paths; once the anchored file exists it always wins.
    """
    raw = os.environ.get(KEY_FILE_ENV_VAR) or getattr(settings, "deepseek_key_file", "")
    if raw and str(raw) != DEFAULT_KEY_FILE:
        return Path(raw).expanduser()
    anchored = _anchored_default_key_file()
    legacy = Path(DEFAULT_KEY_FILE).expanduser()
    if (
        not anchored.is_file()
        and legacy.is_file()
        and os.path.normcase(str(legacy.resolve())) != os.path.normcase(str(anchored.resolve()))
    ):
        logger.warning(
            "the DeepSeek key store was found at the cwd-relative legacy path %s while the "
            "anchored default %s does not exist; reading the legacy file — move it to %s (or "
            "set %s) so the store keeps working from any cwd (L1-07)",
            legacy, anchored, anchored, KEY_FILE_ENV_VAR,
        )
        return legacy
    return anchored


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
    """The value persisted at `path`, cached per path + observed identity, or None.

    Freshness (V7-T06): every call observes the file identity with ONE stat; a cache entry
    whose recorded identity still matches is served as-is, otherwise the read-and-cache
    step re-runs under `_WRITE_LOCK` with a double-check after acquiring it (RD13: a racing
    in-process write can never leave the cache serving a value the file does not hold).
    The identity is stat()ed BEFORE the bytes are read and cached with them, so a rewrite
    landing mid-read makes the NEXT read's stat disagree and refresh — never the reverse.
    Corruption is refused by NAME (`E_STORAGE`): invalid JSON, a JSON document that is not
    an object, or a non-string value under the key (see the module docstring, "CORRUPT vs
    EMPTY").
    """
    cache_key = str(path)
    try:
        observed: _FileIdentity | None = _observed_identity(path)
    except OSError:
        observed = None  # cannot even stat: force the locked path, which refuses honestly
    entry = _FILE_CACHE.get(cache_key)
    if entry is not None and observed is not None and entry[0] == observed:
        return entry[1]
    with _WRITE_LOCK:
        try:
            observed = _observed_identity(path)  # re-stat under the lock (double-check basis)
        except OSError as exc:
            raise PolsiaError(
                E_STORAGE,
                f"could not stat the stored DeepSeek key file: {type(exc).__name__}: {exc}",
                how_to_fix=_STORAGE_HINT,
                context={"path": str(path)},
            ) from exc
        entry = _FILE_CACHE.get(cache_key)
        if entry is not None and entry[0] == observed:  # refreshed while we waited
            return entry[1]
        value: str | None = None
        try:
            if observed != _ABSENT_IDENTITY and path.is_file():
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
        _FILE_CACHE[cache_key] = (observed, value)
        return value


#: True on Windows.  A module constant (not an inline `os.name` read) so both permission
#: branches are deterministically exercisable in tests on either host (2R-05).
_WINDOWS = os.name == "nt"

#: Bound on the icacls call.  A hung tightening must never wedge the write path — best-effort
#: by design, and the value is already persisted durably when this runs.
_ICACLS_TIMEOUT_SECONDS = 15


def _current_username() -> str:
    """The name icacls will grant: USERNAME/USER from the environment, else the platform lookup.

    Never raises — an anonymous host is a refusal (no grant to name), not a crash.
    """
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if user:
        return user
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 — env-less hosts (empty USER/USERNAME/LOGNAME) raise here
        return ""


def _tighten_windows_acl(path: Path) -> bool:
    """Strip inherited ACEs and grant ONLY the current user R,W via `icacls`; True on success.

    2R-05 / U2-11: on Windows `os.chmod` only toggles the read-only bit — the file keeps the ACL
    it inherits from its directory, which is how `deepseek_api_key.json` was observed 0644.
    `/inheritance:r` removes the inherited ACEs, `/grant:r "<user>:(R,W)"` leaves exactly one
    grant: this user, read+write.  Never raises: a missing/hung/denied icacls is a False and the
    caller logs the warning that names the file.
    """
    user = _current_username()
    if not user:
        return False
    try:
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
            capture_output=True,
            text=True,
            timeout=_ICACLS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _best_effort_chmod(path: Path) -> bool:
    """Ask for owner-only access and REPORT whether the platform-strong mechanism won.

    POSIX: `os.chmod(path, 0o600)` IS the tightening.  Windows: the chmod call is kept (it still
    toggles the read-only bit, byte-identical to this module's previous behaviour) but the real
    tightening is `_tighten_windows_acl` (2R-05).  Each failing mechanism logs a warning NAMING
    the file — tightening stays best-effort (the key is already persisted durably when this runs;
    refusing the save over an ACL would lose the credential it protects), but the miss is never
    silent, so nobody believes the file is owner-only when it is not.

    Returns True when the platform-strong mechanism demonstrably succeeded; callers may ignore the
    value (none need to act on a False today) — the tests pin it.
    """
    chmod_ok = True
    try:
        os.chmod(path, 0o600)
    except OSError as exc:  # noqa: BLE001 -- permission tightening is best-effort
        chmod_ok = False
        logger.warning(
            "could not tighten permissions on %s: %s — the file may be readable by other users",
            path,
            type(exc).__name__,
        )
    if not _WINDOWS:
        return chmod_ok
    acl_ok = _tighten_windows_acl(path)
    if not acl_ok:
        logger.warning(
            "could not tighten the Windows ACL on %s: icacls /inheritance:r /grant:r did not "
            "succeed — the file keeps its inherited directory ACL and may be readable by other "
            'local users; tighten it by hand: icacls "%s" /inheritance:r /grant:r '
            '"%%USERNAME%%:(R,W)"',
            path,
            path,
        )
    return acl_ok


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
                # M1 (research-B): "durably" means the bytes must survive a CRASH.  os.replace
                # gives atomicity (never a torn file) but NOT durability — the page cache can
                # still lose the contents.  fsync the temp file BEFORE the replace (on Windows
                # os.fsync calls _commit).  Scope, stated honestly: the POSIX recipe also
                # fsyncs the directory (the rename itself); Windows has no portable directory
                # fsync, so that half is not represented here.
                os.fsync(handle)
            finally:
                os.close(handle)
            os.replace(temp, path)
            _best_effort_chmod(path)
            # V7-T06: cache the value TOGETHER with the identity a reader will observe.
            # os.replace moves the file without touching its bytes' mtime/size (probed on
            # this Windows host), so this stat is exactly what the next reader's stat sees;
            # any LATER third-party rewrite is noticed by the identity mismatch.
            _FILE_CACHE[str(path)] = (_observed_identity(path), cleaned)
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
            # V7-T06: an explicit absent observation — the next read falls through to
            # env/None; if the file is (re)created meanwhile, the mismatch refreshes.
            _FILE_CACHE[str(path)] = (_ABSENT_IDENTITY, None)
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


# ---------------------------------------------------------------------------
# The generic secret store (V25/L2) — the ONE read path other integrations get
# ---------------------------------------------------------------------------
# WHY THIS EXISTS: V25 grew a second credential (GitHub's `GITHUB_TOKEN`, and the deploy
# adapter that follows it), and the frozen rule for this tree is that a credential has exactly
# ONE resolver — the DeepSeek key already proved what a second path costs (two surfaces
# disagreeing about which key is live).  `get_secret(name)` is that resolver for every
# environment-style name: the SAME atomic file, the SAME temp-file + `os.replace` + fsync +
# best-effort chmod/icacls hardening, the SAME precedence shape as the DeepSeek key
# (stored value > process env > the matching Settings field, which is how a `backend/.env`
# entry arrives — pydantic-settings loads the file without exporting it).
#
# IT IS A SEPARATE FILE on purpose: `data/deepseek_api_key.json` is documented as holding one
# value and its CORRUPT-vs-EMPTY contract is pinned by its own tests; adding keys to it would
# quietly widen that contract.  `data/secrets.json` is `{"NAME": "value"}` with the identical
# hardening, and every reader goes through this module.
#
# NO CACHE, stated: a secret read costs one file read and happens once per tool call, so a
# read-through implementation cannot serve a rotated value from a stale cache — the class of
# bug `_FILE_CACHE` needed its identity dance to avoid.  Names are environment-style
# (`^[A-Z][A-Z0-9_]{0,63}$`) so a caller can never address a path, a dotted key or a blank.
SECRETS_FILE_ENV_VAR = "POLSIA_SECRETS_FILE"
DEFAULT_SECRETS_FILE = "data/secrets.json"
_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _anchored_default_secrets_file() -> Path:
    """`<backend>/data/secrets.json`, anchored at this file (the `deepseek_key_file` rule)."""
    return Path(__file__).resolve().parents[2] / DEFAULT_SECRETS_FILE


def _resolve_secrets_file() -> Path:
    """The active generic secret store: explicit `POLSIA_SECRETS_FILE` > repo-anchored default."""
    raw = os.environ.get(SECRETS_FILE_ENV_VAR) or getattr(settings, "secrets_file", "")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    return _anchored_default_secrets_file()


def _checked_secret_name(name: Any) -> str:
    """The environment-style name, or a NAMED `E_VALIDATION` — never a coerced string."""
    text = name.strip() if isinstance(name, str) else ""
    if not _SECRET_NAME_RE.match(text):
        raise PolsiaError(
            E_VALIDATION,
            f"secret name {name!r} is not a valid environment-style name "
            "(expected ^[A-Z][A-Z0-9_]{0,63}$)",
            how_to_fix="pass the credential's environment variable name, e.g. 'GITHUB_TOKEN'",
            context={"name": str(name)[:80]},
        )
    return text


def _read_secrets_document(path: Path) -> dict[str, Any]:
    """The store's JSON object, or `{}` when the file is absent. Corrupt refuses `E_STORAGE`."""
    try:
        if not path.is_file():
            return {}
        raw_document = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolsiaError(
            E_STORAGE,
            f"could not read the secret store file: {type(exc).__name__}: {exc}",
            how_to_fix=_STORAGE_HINT,
            context={"path": str(path)},
        ) from exc
    try:
        document = json.loads(raw_document)
    except ValueError as exc:
        raise PolsiaError(
            E_STORAGE,
            f"the secret store file is not valid JSON: {type(exc).__name__}",
            how_to_fix=f"delete the file and re-enter the secret ({path})",
            context={"path": str(path)},
        ) from exc
    if not isinstance(document, dict):
        raise PolsiaError(
            E_STORAGE,
            "the secret store file is not a JSON object "
            f"(found {type(document).__name__})",
            how_to_fix=f"delete the file and re-enter the secret ({path})",
            context={"path": str(path)},
        )
    return document


def _store_secrets_document(path: Path, document: dict[str, Any]) -> None:
    """Publish `document` with the store's ONE durable write recipe (caller holds the lock)."""
    payload = json.dumps(document)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(handle, payload.encode("utf-8"))
            os.fsync(handle)          # durability before the atomic replace (M1)
        finally:
            os.close(handle)
        os.replace(temp, path)
        _best_effort_chmod(path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise PolsiaError(
            E_STORAGE,
            f"could not store the secret: {type(exc).__name__}: {exc}",
            how_to_fix=(
                "make sure the data directory is writable "
                f"({path.parent}) and try again"
            ),
            context={"path": str(path)},
        ) from exc


def get_secret(name: str) -> str | None:
    """The value of `name`: stored > process environment > the matching Settings field.

    `None` means "no usable value anywhere" — never a fabricated placeholder.  A stored value
    that is not a string (the file was hand-edited into a bad shape) refuses `E_STORAGE`
    rather than silently falling back to the environment and blaming the wrong credential.
    """
    key = _checked_secret_name(name)
    document = _read_secrets_document(_resolve_secrets_file())
    candidate = document.get(key)
    if candidate is not None and not isinstance(candidate, str):
        raise PolsiaError(
            E_STORAGE,
            f"the secret store carries a non-string value for {key} "
            f"(found {type(candidate).__name__})",
            how_to_fix="delete the entry or re-enter the secret",
            context={"name": key},
        )
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    env_value = os.environ.get(key) or getattr(settings, key.lower(), "")
    text = env_value.strip() if isinstance(env_value, str) else ""
    return text or None


def set_secret(name: str, value: str) -> None:
    """Persist `value` under `name` in the generic store (blank is refused)."""
    key = _checked_secret_name(name)
    cleaned = value.strip() if isinstance(value, str) else ""
    if not cleaned:
        raise PolsiaError(
            E_VALIDATION,
            f"the secret {key} must not be blank",
            how_to_fix=f"pass the real value for {key} before saving",
            context={"name": key},
        )
    path = _resolve_secrets_file()
    with _WRITE_LOCK:
        document = _read_secrets_document(path)
        document[key] = cleaned
        _store_secrets_document(path, document)
    logger.info("stored secret %s (len=%d)", key, len(cleaned))


def clear_secret(name: str) -> None:
    """Remove `name` from the generic store (an env value may still configure it)."""
    key = _checked_secret_name(name)
    path = _resolve_secrets_file()
    with _WRITE_LOCK:
        document = _read_secrets_document(path)
        if key not in document:
            return
        document.pop(key)
        if document:
            _store_secrets_document(path, document)
        else:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise PolsiaError(
                    E_STORAGE,
                    f"could not remove the secret store file: {type(exc).__name__}: {exc}",
                    how_to_fix=_STORAGE_HINT,
                    context={"path": str(path)},
                ) from exc
    logger.info("cleared secret %s", key)


def secret_status(name: str) -> dict[str, Any]:
    """`{"configured", "source", "masked"}` for `name` — the same shape as the DeepSeek status."""
    key = _checked_secret_name(name)
    document = _read_secrets_document(_resolve_secrets_file())
    stored = document.get(key)
    if isinstance(stored, str) and stored.strip():
        return {"configured": True, "source": _SOURCE_STORED, "masked": mask_secret(stored.strip())}
    env_value = os.environ.get(key) or getattr(settings, key.lower(), "")
    text = env_value.strip() if isinstance(env_value, str) else ""
    if text:
        return {"configured": True, "source": _SOURCE_ENV, "masked": mask_secret(text)}
    return {"configured": False, "source": _SOURCE_NONE, "masked": "…"}


def secret_statuses(names: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Masked status for several names, reading the store file ONCE (V26/L2, additive).

    `secret_status` above parses the store per call; the Integrations panel reports six
    providers' variables at once, and a dozen reads of the same file is a pointless cost.  The
    precedence, the masking and the three-key shape are IDENTICAL to `secret_status` — this is
    that function's loop over one document, not a second implementation with its own rules.
    Names are validated by the same `_checked_secret_name` regex, so a caller still cannot
    address a path or a blank.
    """
    checked = [_checked_secret_name(name) for name in names]
    document = _read_secrets_document(_resolve_secrets_file())
    statuses: dict[str, dict[str, Any]] = {}
    for key in checked:
        stored = document.get(key)
        if isinstance(stored, str) and stored.strip():
            statuses[key] = {
                "configured": True,
                "source": _SOURCE_STORED,
                "masked": mask_secret(stored.strip()),
            }
            continue
        env_value = os.environ.get(key) or getattr(settings, key.lower(), "")
        text = env_value.strip() if isinstance(env_value, str) else ""
        if text:
            statuses[key] = {
                "configured": True,
                "source": _SOURCE_ENV,
                "masked": mask_secret(text),
            }
        else:
            statuses[key] = {"configured": False, "source": _SOURCE_NONE, "masked": "…"}
    return statuses


__all__ = [
    "DEEPSEEK_ENV_VAR",
    "DEEPSEEK_MODELS_URL",
    "DEFAULT_KEY_FILE",
    "DEFAULT_SECRETS_FILE",
    "KEY_FILE_ENV_VAR",
    "SECRETS_FILE_ENV_VAR",
    "clear_deepseek_api_key",
    "clear_secret",
    "deepseek_key_status",
    "get_deepseek_api_key",
    "get_secret",
    "mask_secret",
    "secret_status",
    "secret_statuses",
    "set_deepseek_api_key",
    "set_secret",
    "verify_deepseek_key",
]
