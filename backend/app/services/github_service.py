"""GitHub source control for the `code_generation` agent (V25/L2).

WHY THIS MODULE (V25-PLAN, "Remaining work" row 2): "**GitHub tools** — repo/branch/commit/PR
behind the firewalled registry + secret store ... The code agent can actually ship code; source
history like Polsia".  The registry tools (`github_repo`, `github_branch`, `github_commit`,
`github_pr`) are thin argument-shaping wrappers over this module; everything that touches the
network, the credential or a refusal lives here so there is exactly ONE implementation.

THE TOKEN PATH — ONE RESOLVER
-----------------------------
`resolve_token()` reads `secret_store.get_secret("GITHUB_TOKEN")` and nothing else.  That
resolver is the app's existing secret-store path: a value stored in the dashboard's generic
secret store (`data/secrets.json`, atomic + fsync + owner-only hardening), else the process
environment variable `GITHUB_TOKEN`, else the matching `Settings` field (how a `backend/.env`
entry arrives).  This module reads no environment variable and no Settings field of its own —
a second key path is how two surfaces start disagreeing about which credential is live (the
same rule `tool_registry._deepseek_api_key` follows).  With no value the answer is `E_CONFIG`,
never a request without a token.

NAMED REFUSALS (the frozen V25 taxonomy)
----------------------------------------
  * `E_CONFIG`   — no `GITHUB_TOKEN` anywhere; nothing was attempted.
  * `E_AUTH`     — GitHub answered 401/403 (bad/expired/under-scoped token, or a rate limit).
  * `E_VALIDATION` — caller input the API would refuse: repo `owner/name`, branch name, file
    path, commit message, PR fields; also GitHub's 400/404/409/422 answers (a repo, branch or
    ref that does not exist is the caller's to fix).  Validated BEFORE any request where the
    input alone is enough.
  * `E_UPSTREAM` — 5xx, 429, timeouts, transport faults, a 200 whose body is not the documented
    JSON object, or a response larger than the read cap.  `E_UPSTREAM` is module-local: it is
    the V25 plan's name for "no usable answer from GitHub" and `app/core/errors.py` (outside
    this lane) does not carry it — the same treatment V24 gave `E_BALANCE`/`E_RATE_LIMIT`.
Nothing here fabricates a result: a refused call returns a `PolsiaError` with the provider's own
message (clipped and scrubbed of the token), and every success shape is built from real fields.

DRY RUN, AND NEVER A FORCE-PUSH
-------------------------------
The three mutating entry points (`create_branch`, `commit_files`, `create_pr`) take
`dry_run: bool = True` — TRUE by default, and a dry run performs NO HTTP request at all: it
validates the arguments and returns the plan with `dry_run: true` and `null` ids.  A plan is not
a success and can never be mistaken for one.  A real call passes `dry_run=False` explicitly.

`github_commit` walks GitHub's Git data API: resolve the branch ref -> its commit -> base tree,
create one base64 blob per file, build a tree on `base_tree`, create the commit with the branch
head as its single parent, then PATCH the ref with `{"sha": ..., "force": false}`.  Force is
ALWAYS false, the registry schema has no `force` field, and a `force` argument is refused by
name in `tool_registry` — this module can express no history rewrite at all.

FROZEN SHAPES (pinned by `tests/unit/test_v25_github_tools.py`)
--------------------------------------------------------------
  repo   -> {repo: {full_name, private, default_branch, html_url, description, archived,
                    can_push}}
  branch -> {repo, branch, sha, created, dry_run}
  commit -> {repo, branch, commit_sha, url, files: [{path, bytes, sha}], dry_run}
  pr     -> {repo, number, url, state, head, base, dry_run}

The `_TRANSPORT` seam is the LABELLED test double (the `tool_registry._SEARCH_TRANSPORT`
pattern): `None` in production (a plain `httpx.Client`), an `httpx.MockTransport` in tests, so
validation, token resolution, status mapping and payload shaping all run for real.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.core.errors import E_AUTH, E_CONFIG, E_STORAGE, E_VALIDATION, PolsiaError

#: GitHub's REST base. Module-level so a test can point it elsewhere without touching call sites.
GITHUB_API = "https://api.github.com"

#: The ONE credential name this module resolves (`secret_store` decides where the value lives).
GITHUB_TOKEN_NAME = "GITHUB_TOKEN"

#: Wall clock for ONE GitHub request.  Reads and writes are single calls; a commit is a handful
#: of sequential calls, each bounded by this.
GITHUB_TIMEOUT_S = 20.0

#: Read cap on a response body before it is refused (these endpoints answer in kilobytes; a
#: body past this is not the documented shape).
GITHUB_MAX_RESPONSE_BYTES = 1024 * 1024

#: Commit bounds: one file <= 256 KiB (the code workspace's own cap), a commit <= 30 files and
#: <= 512 KiB total — a bounded tool result and a bounded API payload.
MAX_FILE_BYTES = 256 * 1024
MAX_FILES_PER_COMMIT = 30
MAX_TOTAL_BYTES = 512 * 1024

#: The V25 plan's name for "GitHub gave no usable answer" (see the module docstring: local
#: constant because `app/core/errors.py` is outside this lane, the V24 E_BALANCE precedent).
E_UPSTREAM = "E_UPSTREAM"

#: Commit identity fixed to a plain file mode: this surface ships text files, never symlinks,
#: executables or submodules (a path the caller cannot use to change what a file means).
COMMIT_FILE_MODE = "100644"

#: The HTTP transport seam.  `None` in production; tests install an `httpx.MockTransport` here.
_TRANSPORT: Any = None

_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})/[A-Za-z0-9._-]{1,100}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_BRANCH_FORBIDDEN = ("..", "@{", "\\", " ", "~", "^", ":", "?", "*", "[")
_BRANCH_MAX_CHARS = 200
_PATH_MAX_CHARS = 400
_MESSAGE_MAX_CHARS = 4000
_TITLE_MAX_CHARS = 256
_PR_BODY_MAX_CHARS = 20000


class GithubApiError(PolsiaError):
    """A `PolsiaError` that also carries the HTTP status, so a caller can branch on 404."""

    def __init__(self, code: str, message: str, *, status: int, how_to_fix: str = "",
                 context: dict[str, Any] | None = None) -> None:
        super().__init__(code, message, how_to_fix=how_to_fix,
                         context={**(context or {}), "status": status})
        self.status = status


# ---------------------------------------------------------------------------
# the credential
# ---------------------------------------------------------------------------

def resolve_token() -> str:
    """`GITHUB_TOKEN` through the app's secret-store path, or a named `E_CONFIG`.

    A corrupt store is the store's own `E_STORAGE` and is passed through unchanged — reporting
    "no token" over a broken store would blame the wrong thing (the deepseek store's rule).
    """
    from app.services import secret_store

    try:
        value = secret_store.get_secret(GITHUB_TOKEN_NAME)
    except PolsiaError as exc:
        if exc.code == E_STORAGE:
            raise
        raise PolsiaError(
            exc.code, exc.message, how_to_fix=exc.how_to_fix, context=dict(exc.context)
        ) from exc
    if not value:
        raise PolsiaError(
            E_CONFIG,
            "github: no GITHUB_TOKEN is configured, so no GitHub call can be made",
            how_to_fix=(
                "store GITHUB_TOKEN through the app's secret store (or export GITHUB_TOKEN / "
                "set it in backend/.env): a fine-grained PAT with repository contents and "
                "pull-request permissions for the target repository"
            ),
            context={"variable": GITHUB_TOKEN_NAME},
        )
    return value


# ---------------------------------------------------------------------------
# caller input (validated before any request)
# ---------------------------------------------------------------------------

def validate_repo(value: Any) -> str:
    """`owner/name`, or a named `E_VALIDATION` — never a guessed or repaired spelling."""
    if not isinstance(value, str):
        raise PolsiaError(
            E_VALIDATION,
            f"github: 'repo' must be a string 'owner/name' (got {type(value).__name__})",
            how_to_fix="pass the repository as 'owner/name', e.g. 'acme/widgets'",
            context={"repo": str(value)[:120]},
        )
    text = value.strip()
    problem = ""
    if not text:
        problem = "is blank"
    elif any(ord(character) < 33 for character in text):
        problem = "contains whitespace or control characters"
    elif text.count("/") != 1:
        problem = "must be exactly 'owner/name' (one slash)"
    elif ".." in text:
        problem = "contains '..'"
    elif not _REPO_RE.match(text):
        problem = "is not a valid GitHub 'owner/name'"
    if problem:
        raise PolsiaError(
            E_VALIDATION,
            f"github: repo {value!r} {problem}",
            how_to_fix="pass the repository as 'owner/name', e.g. 'acme/widgets'",
            context={"repo": str(value)[:120]},
        )
    return text


def validate_branch(value: Any, *, field: str = "branch") -> str:
    """A git branch name GitHub will accept, or a named `E_VALIDATION`."""
    if not isinstance(value, str):
        raise PolsiaError(
            E_VALIDATION,
            f"github: {field!r} must be a string (got {type(value).__name__})",
            how_to_fix="pass a branch name such as 'main' or 'feature/checkout'",
            context={field: str(value)[:120]},
        )
    text = value.strip()
    problem = ""
    if not text:
        problem = "is blank"
    elif len(text) > _BRANCH_MAX_CHARS:
        problem = f"is longer than {_BRANCH_MAX_CHARS} characters"
    elif any(ord(character) < 33 for character in text):
        problem = "contains whitespace or control characters"
    elif text.startswith(("-", "/")) or text.endswith(("/", ".")):
        problem = "starts or ends with a character git refuses"
    elif text.endswith(".lock"):
        problem = "ends with '.lock'"
    elif "//" in text:
        problem = "contains an empty path segment"
    elif any(token in text for token in _BRANCH_FORBIDDEN):
        problem = "contains a sequence git refuses"
    if problem:
        raise PolsiaError(
            E_VALIDATION,
            f"github: {field} {value!r} {problem}",
            how_to_fix="pass a valid git branch name (no spaces, '..', '~^:?*[', backslash)",
            context={field: str(value)[:120]},
        )
    return text


def validate_path(value: Any) -> str:
    """A repository-relative file path, or a named `E_VALIDATION` (traversal-proof)."""
    if not isinstance(value, str):
        raise PolsiaError(
            E_VALIDATION,
            f"github_commit: a file 'path' must be a string (got {type(value).__name__})",
            how_to_fix="pass a repository-relative path, e.g. 'src/app.py'",
            context={"path": str(value)[:120]},
        )
    text = value.strip()
    normalized = text.replace("\\", "/")
    problem = ""
    if not text:
        problem = "is blank"
    elif len(text) > _PATH_MAX_CHARS:
        problem = f"is longer than {_PATH_MAX_CHARS} characters"
    elif any(ord(character) < 32 for character in text):
        problem = "contains control characters"
    elif "\\" in text:
        problem = "contains a backslash (GitHub paths are forward-slash)"
    elif normalized.startswith("/") or _DRIVE_RE.match(normalized):
        problem = "is absolute"
    elif any(part in ("", ".", "..") for part in normalized.split("/")):
        problem = "contains an empty, '.' or '..' segment"
    if problem:
        raise PolsiaError(
            E_VALIDATION,
            f"github_commit: file path {value!r} {problem}",
            how_to_fix="pass a repository-relative path inside the repo, e.g. 'src/app.py'",
            context={"path": str(value)[:120]},
        )
    return normalized


def _validate_files(value: Any) -> list[dict[str, Any]]:
    """`[{path, data, text}]` for a commit's `files`, or a named `E_VALIDATION`."""
    if not isinstance(value, list) or not value:
        raise PolsiaError(
            E_VALIDATION,
            "github_commit: 'files' must be a non-empty list of {path, content}",
            how_to_fix="pass at least one file: [{\"path\": \"src/app.py\", \"content\": \"...\"}]",
            context={"files": type(value).__name__},
        )
    if len(value) > MAX_FILES_PER_COMMIT:
        raise PolsiaError(
            E_VALIDATION,
            f"github_commit: {len(value)} files exceeds the {MAX_FILES_PER_COMMIT}-file cap "
            "for one commit",
            how_to_fix=f"split the change into commits of at most {MAX_FILES_PER_COMMIT} files",
            context={"files": len(value)},
        )
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise PolsiaError(
                E_VALIDATION,
                f"github_commit: files[{index}] must be an object with path+content",
                how_to_fix="pass [[{path, content}]] entries",
                context={"index": index},
            )
        unknown = sorted(set(item) - {"path", "content"})
        if unknown:
            raise PolsiaError(
                E_VALIDATION,
                f"github_commit: files[{index}] carries unknown field(s) {', '.join(unknown)}; "
                "one entry is exactly {path, content}",
                how_to_fix="remove the extra field(s); this surface ships text file contents",
                context={"index": index, "unknown": unknown},
            )
        path = validate_path(item.get("path"))
        if path in seen:
            raise PolsiaError(
                E_VALIDATION,
                f"github_commit: file path {path!r} appears twice in one commit",
                how_to_fix="list each path once",
                context={"path": path},
            )
        seen.add(path)
        content = item.get("content")
        if not isinstance(content, str):
            raise PolsiaError(
                E_VALIDATION,
                f"github_commit: {path!r} content must be a string "
                f"(got {type(content).__name__})",
                how_to_fix="pass the complete UTF-8 text of the file as 'content'",
                context={"path": path},
            )
        if "\x00" in content:
            raise PolsiaError(
                E_VALIDATION,
                f"github_commit: {path!r} content contains a NUL byte — text files only",
                how_to_fix="commit text; binary content is refused by name",
                context={"path": path},
            )
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise PolsiaError(
                E_VALIDATION,
                f"github_commit: {path!r} content is not encodable as UTF-8 "
                f"({exc.reason} at index {exc.start})",
                how_to_fix="pass valid UTF-8 text",
                context={"path": path},
            ) from exc
        if len(data) > MAX_FILE_BYTES:
            raise PolsiaError(
                E_VALIDATION,
                f"github_commit: {path!r} is {len(data)} bytes; the per-file cap is "
                f"{MAX_FILE_BYTES} (256 KiB)",
                how_to_fix="split the file into pieces of at most 256 KiB of text",
                context={"path": path, "bytes": len(data)},
            )
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise PolsiaError(
                E_VALIDATION,
                f"github_commit: the files total more than {MAX_TOTAL_BYTES} bytes",
                how_to_fix="split the change into smaller commits",
                context={"bytes": total},
            )
        normalized.append({"path": path, "data": data, "text": content})
    return normalized


def _validate_text(value: Any, *, field: str, maximum: int, required: bool = True) -> str | None:
    """A bounded, non-blank string field (or `None` when optional and absent)."""
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise PolsiaError(
            E_VALIDATION,
            f"github: {field!r} must be a non-blank string",
            how_to_fix=f"pass {field} as text",
            context={field: str(value)[:120]},
        )
    text = value.strip()
    if len(text) > maximum:
        raise PolsiaError(
            E_VALIDATION,
            f"github: {field!r} is longer than {maximum} characters",
            how_to_fix=f"shorten {field}",
            context={field: len(text)},
        )
    return text


def _validate_bool(value: Any, *, field: str, default: bool) -> bool:
    """A strict boolean argument; `None` (absent/JSON null) means the documented default."""
    if value is None:
        return default
    if not isinstance(value, bool):
        raise PolsiaError(
            E_VALIDATION,
            f"github: {field!r} must be true or false (got {value!r})",
            how_to_fix=f"pass {field} as a JSON boolean, not a string",
            context={field: str(value)[:40]},
        )
    return value


# ---------------------------------------------------------------------------
# the one HTTP path
# ---------------------------------------------------------------------------

def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "PolsiaAgent/1.0 (+github_service)",
    }


def _provider_message(raw: bytes) -> str:
    """GitHub's own `message` line, clipped — never the token, never a traceback."""
    try:
        document = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return ""
    if isinstance(document, dict) and isinstance(document.get("message"), str):
        return document["message"].strip()[:300]
    return ""


def _status_mapping(status: int) -> tuple[str, str]:
    """The frozen V25 status table: (named code, how-to-fix)."""
    if status in (401, 403):
        return (E_AUTH,
                "check GITHUB_TOKEN — GitHub rejected it or it lacks the repository "
                "contents/pull-request permission; a 403 can also be a rate limit, so retry "
                "after the reset if the token is valid")
    if status in (400, 404, 409, 422):
        return (E_VALIDATION,
                "check the repo owner/name, branch name and file paths — GitHub refused them")
    return (E_UPSTREAM,
            f"retry after a short wait; GitHub answered HTTP {status} and gave no usable answer")


def _scrub(text: str, token: str) -> str:
    """Defense in depth: no returned string may carry the token verbatim."""
    if token and token in text:
        return text.replace(token, "«redacted»")
    return text


def _request(
    method: str,
    path: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
    expect: tuple[int, ...] = (200,),
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """ONE GitHub API call -> its JSON object, or a named refusal.  Never returns prose."""
    owned = client is None
    http = client or httpx.Client(
        timeout=httpx.Timeout(GITHUB_TIMEOUT_S),
        follow_redirects=False,
        transport=_TRANSPORT,
    )
    try:
        try:
            response = http.request(method, f"{GITHUB_API}{path}", headers=_headers(token),
                                    json=body)
        except httpx.TimeoutException as exc:
            raise PolsiaError(
                E_UPSTREAM,
                f"github: {method} {path} timed out after {GITHUB_TIMEOUT_S:g}s",
                how_to_fix="retry once; if it persists, check GitHub's status page",
                context={"method": method, "path": path, "timeout_s": GITHUB_TIMEOUT_S},
            ) from exc
        except httpx.HTTPError as exc:
            raise PolsiaError(
                E_UPSTREAM,
                _scrub(f"github: {method} {path} failed at the transport layer: "
                       f"{type(exc).__name__}: {exc}", token),
                how_to_fix="check network reachability to api.github.com and retry",
                context={"method": method, "path": path},
            ) from exc
    finally:
        if owned:
            http.close()

    raw = response.content or b""
    if len(raw) > GITHUB_MAX_RESPONSE_BYTES:
        raise GithubApiError(
            E_UPSTREAM,
            f"github: {method} {path} returned {len(raw)} bytes, past the "
            f"{GITHUB_MAX_RESPONSE_BYTES}-byte read cap",
            status=response.status_code,
            how_to_fix="retry; if it persists the endpoint is returning an unexpected body",
            context={"method": method, "path": path},
        )
    if response.status_code not in expect:
        code, how_to_fix = _status_mapping(response.status_code)
        detail = _provider_message(raw)
        message = f"github: {method} {path} answered HTTP {response.status_code}"
        if detail:
            message += f": {detail}"
        raise GithubApiError(code, _scrub(message, token), status=response.status_code,
                             how_to_fix=how_to_fix, context={"method": method, "path": path})
    if not raw:
        return {}
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise GithubApiError(
            E_UPSTREAM,
            f"github: {method} {path} answered {response.status_code} with a body that is "
            "not JSON — refusing rather than reporting a result nobody can read",
            status=response.status_code,
            how_to_fix="retry; if it persists the GitHub contract has changed",
            context={"method": method, "path": path},
        ) from None
    if not isinstance(document, dict):
        raise GithubApiError(
            E_UPSTREAM,
            f"github: {method} {path} answered {response.status_code} with a JSON "
            f"{type(document).__name__}, not the documented object",
            status=response.status_code,
            how_to_fix="retry; if it persists the GitHub contract has changed",
            context={"method": method, "path": path},
        )
    return document


def _required_sha(document: dict[str, Any], *keys: str) -> str:
    """A sha nested under `keys`, or `E_UPSTREAM` — a 200 without it is not a usable answer."""
    node: Any = document
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, str) or not node.strip():
        raise PolsiaError(
            E_UPSTREAM,
            f"github: the API answered 200 without the expected {'/'.join(keys)}",
            how_to_fix="retry; if it persists the GitHub contract has changed",
            context={"expected": "/".join(keys)},
        )
    return node


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

def get_repo(repo: Any, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """`GET /repos/{owner}/{name}` -> the frozen repo shape."""
    name = validate_repo(repo)
    token = resolve_token()
    document = _request("GET", f"/repos/{name}", token=token, client=client)
    permissions = document.get("permissions")
    can_push = permissions.get("push") if isinstance(permissions, dict) else None
    return {
        "full_name": document.get("full_name") if isinstance(document.get("full_name"), str)
        else name,
        "private": document.get("private") is True,
        "default_branch": document.get("default_branch")
        if isinstance(document.get("default_branch"), str) else None,
        "html_url": document.get("html_url")
        if isinstance(document.get("html_url"), str) else None,
        "description": document.get("description")
        if isinstance(document.get("description"), str) else None,
        "archived": document.get("archived") is True,
        "can_push": can_push if isinstance(can_push, bool) else None,
    }


def get_branch(repo: Any, branch: Any, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """`GET /repos/{repo}/branches/{branch}` -> the frozen branch shape (created=False)."""
    name = validate_repo(repo)
    branch_name = validate_branch(branch)
    token = resolve_token()
    document = _request("GET", f"/repos/{name}/branches/{quote(branch_name, safe='')}",
                        token=token, client=client)
    sha = _required_sha(document, "commit", "sha")
    return {"repo": name, "branch": branch_name, "sha": sha, "created": False, "dry_run": False}


# ---------------------------------------------------------------------------
# mutations (dry_run defaults to TRUE)
# ---------------------------------------------------------------------------

def create_branch(
    repo: Any,
    branch: Any,
    from_branch: Any,
    *,
    dry_run: bool = True,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Create `branch` at `from_branch`'s head.  Idempotent: an existing branch is reported."""
    name = validate_repo(repo)
    branch_name = validate_branch(branch)
    base_name = validate_branch(from_branch, field="from_branch")
    plan = {"repo": name, "branch": branch_name, "sha": None, "created": False, "dry_run": True}
    if _validate_bool(dry_run, field="dry_run", default=True):
        return plan

    token = resolve_token()
    try:
        existing = get_branch(name, branch_name, client=client)
    except GithubApiError as exc:
        if exc.status != 404:
            raise
    else:
        return {**existing, "dry_run": False}

    base = get_branch(name, base_name, client=client)
    _request(
        "POST",
        f"/repos/{name}/git/refs",
        token=token,
        body={"ref": f"refs/heads/{branch_name}", "sha": base["sha"]},
        expect=(201,),
        client=client,
    )
    return {"repo": name, "branch": branch_name, "sha": base["sha"], "created": True,
            "dry_run": False}


def commit_files(
    repo: Any,
    branch: Any,
    message: Any,
    files: Any,
    *,
    dry_run: bool = True,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Commit `files` (text) to `branch` through the Git data API.  Never force-pushes."""
    name = validate_repo(repo)
    branch_name = validate_branch(branch)
    commit_message = _validate_text(message, field="message", maximum=_MESSAGE_MAX_CHARS)
    normalized = _validate_files(files)
    plan_files = [{"path": entry["path"], "bytes": len(entry["data"]), "sha": None}
                  for entry in normalized]
    if _validate_bool(dry_run, field="dry_run", default=True):
        return {"repo": name, "branch": branch_name, "commit_sha": None, "url": None,
                "files": plan_files, "dry_run": True}

    token = resolve_token()
    ref = _request(
        "GET",
        f"/repos/{name}/git/ref/heads/{quote(branch_name, safe='/')}",
        token=token,
        client=client,
    )
    parent_sha = _required_sha(ref, "object", "sha")
    parent = _request("GET", f"/repos/{name}/git/commits/{parent_sha}", token=token,
                      client=client)
    base_tree = _required_sha(parent, "tree", "sha")

    tree_entries: list[dict[str, Any]] = []
    result_files: list[dict[str, Any]] = []
    for entry in normalized:
        blob = _request(
            "POST",
            f"/repos/{name}/git/blobs",
            token=token,
            body={
                "content": base64.b64encode(entry["data"]).decode("ascii"),
                "encoding": "base64",
            },
            expect=(201,),
            client=client,
        )
        blob_sha = _required_sha(blob, "sha")
        tree_entries.append({"path": entry["path"], "mode": COMMIT_FILE_MODE,
                             "type": "blob", "sha": blob_sha})
        result_files.append({"path": entry["path"], "bytes": len(entry["data"]),
                             "sha": blob_sha})

    tree = _request(
        "POST",
        f"/repos/{name}/git/trees",
        token=token,
        body={"base_tree": base_tree, "tree": tree_entries},
        expect=(201,),
        client=client,
    )
    tree_sha = _required_sha(tree, "sha")
    commit = _request(
        "POST",
        f"/repos/{name}/git/commits",
        token=token,
        body={"message": commit_message, "tree": tree_sha, "parents": [parent_sha]},
        expect=(201,),
        client=client,
    )
    commit_sha = _required_sha(commit, "sha")
    _request(
        "PATCH",
        f"/repos/{name}/git/refs/heads/{quote(branch_name, safe='/')}",
        token=token,
        # FORCE IS ALWAYS FALSE: a commit this surface makes is a fast-forward onto the
        # branch head it just read; it can never rewrite history.
        body={"sha": commit_sha, "force": False},
        expect=(200,),
        client=client,
    )
    url = commit.get("html_url")
    return {
        "repo": name,
        "branch": branch_name,
        "commit_sha": commit_sha,
        "url": url if isinstance(url, str) and url.strip()
        else f"https://github.com/{name}/commit/{commit_sha}",
        "files": result_files,
        "dry_run": False,
    }


def create_pr(
    repo: Any,
    title: Any,
    head: Any,
    base: Any,
    body: Any = None,
    *,
    dry_run: bool = True,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Open a PR from `head` into `base` (never merges, never force-pushes)."""
    name = validate_repo(repo)
    head_name = validate_branch(head, field="head")
    base_name = validate_branch(base, field="base")
    pr_title = _validate_text(title, field="title", maximum=_TITLE_MAX_CHARS)
    pr_body = _validate_text(body, field="body", maximum=_PR_BODY_MAX_CHARS, required=False)
    plan = {"repo": name, "number": None, "url": None, "state": None, "head": head_name,
            "base": base_name, "dry_run": True}
    if _validate_bool(dry_run, field="dry_run", default=True):
        return plan

    token = resolve_token()
    payload: dict[str, Any] = {"title": pr_title, "head": head_name, "base": base_name,
                               "draft": False}
    if pr_body is not None:
        payload["body"] = pr_body
    document = _request("POST", f"/repos/{name}/pulls", token=token, body=payload,
                        expect=(201,), client=client)
    number = document.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        raise PolsiaError(
            E_UPSTREAM,
            "github: the pull request was answered 201 without a number — refusing to report "
            "a PR reference nobody can look up",
            how_to_fix="check the repository's pull requests on GitHub; retry if absent",
            context={"repo": name},
        )
    url = document.get("html_url")
    state = document.get("state")
    return {
        "repo": name,
        "number": number,
        "url": url if isinstance(url, str) and url.strip() else None,
        "state": state if isinstance(state, str) and state.strip() else None,
        "head": head_name,
        "base": base_name,
        "dry_run": False,
    }


__all__ = [
    "COMMIT_FILE_MODE",
    "E_UPSTREAM",
    "GITHUB_API",
    "GITHUB_MAX_RESPONSE_BYTES",
    "GITHUB_TIMEOUT_S",
    "GITHUB_TOKEN_NAME",
    "GithubApiError",
    "MAX_FILES_PER_COMMIT",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "commit_files",
    "create_branch",
    "create_pr",
    "get_branch",
    "get_repo",
    "resolve_token",
    "validate_branch",
    "validate_path",
    "validate_repo",
]
