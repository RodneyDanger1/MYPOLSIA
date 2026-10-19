"""V25/L2 — `github_repo` / `github_branch` / `github_commit` / `github_pr` (RED-first).

Written BEFORE `github_service`, before the four specs and before the allowlist entry exist.
On the untouched tree every case fails by name ("unknown tool 'github_repo': this registry
ships exactly …") — the RED run is `raw/RED-github.txt`.

WHAT IT PINS (the frozen V25 interface):

  * four OpenAI function specs, each `additionalProperties: false`, with exactly the fields
    the lane documents (mutation tools carry `dry_run`, which DEFAULTS TO TRUE);
  * the token is resolved through `app.services.secret_store` ONLY — no second reader here;
    absent token is `E_CONFIG`;
  * `E_AUTH` for 401/403, `E_VALIDATION` for bad repo/branch/path and 400/404/409/422,
    `E_UPSTREAM` for 5xx/timeout/transport/oversized;
  * `dry_run` mutations perform NO request at all (a plan, never a fake success);
  * a real commit goes through the Git data API: ref -> commit -> base64 blobs -> tree ->
    commit -> ref PATCH with `force: false`; a `force` argument is refused by name;
  * the frozen result shapes are exact key sets;
  * the allowlist grants all four to `code_generation` ONLY, and the firewall refuses every
    other agent by name; sandbox mode still refuses a mutation before it is attempted.

The ONLY fake is the HTTP transport (`httpx.MockTransport`), installed through
`github_service._TRANSPORT` — the labelled test seam (the same pattern
`tool_registry._SEARCH_TRANSPORT` uses). Validation, key resolution, refusal mapping and
payload shaping all run for real.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx
import pytest

from app.agents import policy_firewall as pf
from app.agents import tool_registry
from app.config import settings
from app.services import github_service, secret_store

TOKEN = "ghp_v25_test_token_do_not_use"
REPO = "acme/widgets"

REPO_KEYS = {"full_name", "private", "default_branch", "html_url", "description",
             "archived", "can_push"}
BRANCH_KEYS = {"repo", "branch", "sha", "created", "dry_run"}
COMMIT_KEYS = {"repo", "branch", "commit_sha", "url", "files", "dry_run"}
COMMIT_FILE_KEYS = {"path", "bytes", "sha"}
PR_KEYS = {"repo", "number", "url", "state", "head", "base", "dry_run"}
GITHUB_TOOLS = ("github_repo", "github_branch", "github_commit", "github_pr")


# ── the labelled wire double ────────────────────────────────────────────────────────────────

class _Wire:
    """Routes `(METHOD, path)` to an `httpx.Response` (or a callable) and records every call."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = urlsplit(str(request.url)).path
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        self.calls.append({
            "method": request.method,
            "url": str(request.url),
            "path": path,
            "body": body,
            "headers": {key.lower(): value for key, value in request.headers.items()},
        })
        route = self.routes.get((request.method, path))
        if route is None:
            return httpx.Response(404, json={"message": f"test wire has no route for {request.method} {path}"})
        if isinstance(route, Exception):
            raise route
        if callable(route):
            return route(request, body)
        return route

    def methods(self) -> list[str]:
        return [call["method"] for call in self.calls]

    def mutations(self) -> list[dict]:
        return [call for call in self.calls if call["method"] != "GET"]


@pytest.fixture(autouse=True)
def _runtime(monkeypatch, tmp_path):
    """A test-only secret store file, a token, no kill switch, sandbox off (except its own test)."""
    monkeypatch.setenv(secret_store.SECRETS_FILE_ENV_VAR, str(tmp_path / "secrets.json"))
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.delenv("POLSIA_KILL_SWITCH", raising=False)
    monkeypatch.setattr(settings, "sandbox_mode", False)
    yield
    github_service._TRANSPORT = None


def _install(monkeypatch, routes: dict) -> _Wire:
    wire = _Wire(routes)
    monkeypatch.setattr(github_service, "_TRANSPORT", httpx.MockTransport(wire))
    return wire


def _call(name: str, arguments: dict | None = None, *, agent_type: str = "code_generation") -> dict:
    return json.loads(tool_registry.execute_tool(name, arguments or {}, agent_type=agent_type))


def _repo_ok() -> httpx.Response:
    return httpx.Response(200, json={
        "full_name": REPO, "private": True, "default_branch": "main",
        "html_url": f"https://github.com/{REPO}", "description": "Widgets",
        "archived": False, "permissions": {"push": True},
    })


def _branch_ok(sha: str = "base123") -> httpx.Response:
    return httpx.Response(200, json={"name": "main", "protected": False,
                                     "commit": {"sha": sha}})


# ── the frozen interface ───────────────────────────────────────────────────────────────────

def test_the_four_specs_are_frozen_function_specs():
    specs = {spec["function"]["name"]: spec for spec in tool_registry.TOOL_SPECS}
    expected_required = {
        "github_repo": ["repo"],
        "github_branch": ["repo", "branch"],
        "github_commit": ["repo", "branch", "message", "files"],
        "github_pr": ["repo", "title", "head", "base"],
    }
    for name in GITHUB_TOOLS:
        assert name in specs, name
        function = specs[name]["function"]
        parameters = function["parameters"]
        assert specs[name]["type"] == "function"
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        assert parameters["required"] == expected_required[name], name
        assert name in tool_registry.TOOL_GUIDANCE, name
    # The mutation tools document the dry-run default in their own schema.
    for name in ("github_commit", "github_pr"):
        prop = specs[name]["function"]["parameters"]["properties"]["dry_run"]
        assert prop["type"] == "boolean"
        assert "default" in prop["description"].lower(), name
    branch_prop = specs["github_branch"]["function"]["parameters"]["properties"]
    assert "from_branch" in branch_prop and "dry_run" in branch_prop
    files = specs["github_commit"]["function"]["parameters"]["properties"]["files"]
    assert files["type"] == "array" and files["minItems"] == 1
    assert files["items"]["required"] == ["path", "content"]
    # `force` is not a field anywhere: the registry cannot express a force-push.
    for name in GITHUB_TOOLS:
        assert "force" not in specs[name]["function"]["parameters"]["properties"], name


def test_guidance_separates_github_from_the_local_workspace():
    assert "github" in tool_registry.TOOL_GUIDANCE["github_commit"].lower()
    assert "workspace" in tool_registry.TOOL_GUIDANCE["code_write"].lower()
    assert "github_commit" in tool_registry.TOOL_GUIDANCE["code_write"], (
        "the local-vs-GitHub choice must be stated where the agent chooses: code_write"
    )
    assert "dry_run" in tool_registry.TOOL_GUIDANCE["github_commit"]


def test_the_allowlist_grants_the_four_tools_to_code_generation_only():
    holders = {
        agent_type for agent_type, capability in pf.AGENT_CAPABILITIES.items()
        if set(capability.agent_tools) & set(GITHUB_TOOLS)
    }
    assert holders == {"code_generation"}
    assert tool_registry.tools_for("code_generation") == (
        "code_write", "code_read", "code_list", *GITHUB_TOOLS)
    for agent_type in ("orchestrator", "social_media", "business_planning", "finance"):
        assert tool_registry.tools_for(agent_type) == tuple(
            name for name in tool_registry.tools_for(agent_type) if name not in GITHUB_TOOLS
        ), agent_type


@pytest.mark.parametrize("agent_type", sorted(set(pf.AGENT_CAPABILITIES) - {"code_generation"}))
def test_every_other_agent_is_refused_by_name(agent_type):
    payload = _call("github_repo", {"repo": REPO}, agent_type=agent_type)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"
    assert "github_repo" in payload["error"]["message"]
    assert agent_type in payload["error"]["message"]


# ── the token path: secret store ONLY ──────────────────────────────────────────────────────

def test_the_token_is_resolved_through_the_secret_store(monkeypatch):
    assert github_service.resolve_token() == TOKEN
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setattr(settings, "github_token", "")
    with pytest.raises(Exception) as excinfo:
        github_service.resolve_token()
    assert getattr(excinfo.value, "code", "") == "E_CONFIG"


def test_a_stored_secret_wins_over_the_environment(monkeypatch):
    secret_store.set_secret("GITHUB_TOKEN", "ghp_stored_wins")
    assert github_service.resolve_token() == "ghp_stored_wins"


def test_a_corrupt_secret_store_is_E_STORAGE_not_a_missing_token(monkeypatch, tmp_path):
    """A broken store must not be reported as 'no token' — that would blame the wrong thing."""
    from app.core.errors import PolsiaError

    path = tmp_path / "corrupt-secrets.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(secret_store.SECRETS_FILE_ENV_VAR, str(path))

    with pytest.raises(PolsiaError) as excinfo:
        github_service.resolve_token()

    assert excinfo.value.code == "E_STORAGE"


def test_secret_names_are_environment_style_only():
    from app.core.errors import PolsiaError

    for name in ("github_token", "GITHUB TOKEN", "../GITHUB_TOKEN", "", "1TOKEN"):
        with pytest.raises(PolsiaError) as excinfo:
            secret_store.get_secret(name)
        assert excinfo.value.code == "E_VALIDATION", name


def test_no_token_is_E_CONFIG_naming_the_variable(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(settings, "github_token", "")
    wire = _install(monkeypatch, {})

    payload = _call("github_repo", {"repo": REPO})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_CONFIG"
    assert "GITHUB_TOKEN" in payload["error"]["message"]
    assert payload["error"]["how_to_fix"]
    assert wire.calls == [], "no token means no request at all"


# ── github_repo: a real read, frozen shape ─────────────────────────────────────────────────

def test_github_repo_returns_the_frozen_shape(monkeypatch):
    wire = _install(monkeypatch, {("GET", f"/repos/{REPO}"): _repo_ok()})

    payload = _call("github_repo", {"repo": REPO})

    assert payload["ok"] is True and payload["tool"] == "github_repo"
    assert set(payload["result"]) == {"repo"}
    assert set(payload["result"]["repo"]) == REPO_KEYS
    assert payload["result"]["repo"]["full_name"] == REPO
    assert payload["result"]["repo"]["default_branch"] == "main"
    assert payload["result"]["repo"]["can_push"] is True
    call = wire.calls[0]
    assert call["url"] == f"https://api.github.com/repos/{REPO}"
    assert call["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert "github" in call["headers"]["accept"]
    assert TOKEN not in json.dumps(payload), "the token is never echoed"


@pytest.mark.parametrize("repo", [
    "", "no-slash", "a/b/c", "/leading", "trailing/", "a//b", "a/../b", "a b/c",
    "owner/na me", "C:/x", "owner/name?x=1", "owner/name\\x00",
])
def test_a_bad_repo_is_E_VALIDATION_before_any_request(monkeypatch, repo):
    wire = _install(monkeypatch, {})

    payload = _call("github_repo", {"repo": repo})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_VALIDATION", repo
    assert wire.calls == [], "invalid input never reaches the network"


def test_a_non_string_repo_is_E_VALIDATION(monkeypatch):
    _install(monkeypatch, {})
    payload = _call("github_repo", {"repo": 42})
    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION"


# ── named upstream refusals ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("status", "code", "hint"), [
    (401, "E_AUTH", "token"),
    (403, "E_AUTH", "token"),
    (404, "E_VALIDATION", "repo"),
    (422, "E_VALIDATION", "repo"),
    (500, "E_UPSTREAM", "retry"),
    (502, "E_UPSTREAM", "retry"),
])
def test_upstream_statuses_map_to_named_refusals(monkeypatch, status, code, hint):
    _install(monkeypatch, {("GET", f"/repos/{REPO}"): httpx.Response(
        status, json={"message": f"github said {status}"})})

    payload = _call("github_repo", {"repo": REPO})

    assert payload["ok"] is False
    assert payload["error"]["code"] == code, (status, payload)
    assert str(status) in payload["error"]["message"]
    assert hint in payload["error"]["how_to_fix"].lower()


def test_a_timeout_is_E_UPSTREAM(monkeypatch):
    _install(monkeypatch, {("GET", f"/repos/{REPO}"): httpx.ConnectTimeout("too slow")})

    payload = _call("github_repo", {"repo": REPO})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_UPSTREAM"
    assert "timed out" in payload["error"]["message"].lower()


def test_a_transport_error_is_E_UPSTREAM(monkeypatch):
    _install(monkeypatch, {("GET", f"/repos/{REPO}"): httpx.ConnectError("no route")})

    payload = _call("github_repo", {"repo": REPO})

    assert payload["ok"] is False and payload["error"]["code"] == "E_UPSTREAM"


def test_a_non_json_body_is_E_UPSTREAM_not_a_fabricated_repo(monkeypatch):
    _install(monkeypatch, {("GET", f"/repos/{REPO}"): httpx.Response(200, content=b"<html>")})

    payload = _call("github_repo", {"repo": REPO})

    assert payload["ok"] is False and payload["error"]["code"] == "E_UPSTREAM"


# ── github_branch: read, then create (dry-run by default) ─────────────────────────────────

def test_github_branch_without_from_branch_reads_the_branch(monkeypatch):
    wire = _install(monkeypatch, {
        ("GET", f"/repos/{REPO}/branches/main"): _branch_ok("base123"),
    })

    payload = _call("github_branch", {"repo": REPO, "branch": "main"})

    assert payload["ok"] is True and payload["tool"] == "github_branch"
    result = payload["result"]
    assert set(result) == BRANCH_KEYS
    assert result == {"repo": REPO, "branch": "main", "sha": "base123",
                      "created": False, "dry_run": False}
    assert wire.methods() == ["GET"]


def test_creating_a_branch_dry_runs_by_default_and_sends_nothing(monkeypatch):
    wire = _install(monkeypatch, {})

    payload = _call("github_branch",
                    {"repo": REPO, "branch": "v25/feature", "from_branch": "main"})

    assert payload["ok"] is True
    assert payload["result"] == {"repo": REPO, "branch": "v25/feature", "sha": None,
                                 "created": False, "dry_run": True}
    assert wire.calls == [], "a dry run is a plan, never a request"


def test_creating_a_branch_for_real_creates_the_ref_from_the_base_sha(monkeypatch):
    wire = _install(monkeypatch, {
        ("GET", f"/repos/{REPO}/branches/v25%2Ffeature"):
            httpx.Response(404, json={"message": "Branch not found"}),
        ("GET", f"/repos/{REPO}/branches/main"): _branch_ok("base123"),
        ("POST", f"/repos/{REPO}/git/refs"): httpx.Response(
            201, json={"ref": "refs/heads/v25/feature", "object": {"sha": "base123"}}),
    })

    payload = _call("github_branch",
                    {"repo": REPO, "branch": "v25/feature", "from_branch": "main",
                     "dry_run": False})

    result = payload["result"]
    assert set(result) == BRANCH_KEYS
    assert result["created"] is True and result["sha"] == "base123" and result["dry_run"] is False
    create = [call for call in wire.calls if call["method"] == "POST"][0]
    assert create["body"] == {"ref": "refs/heads/v25/feature", "sha": "base123"}
    assert "force" not in json.dumps(create["body"]).lower()


def test_an_existing_branch_is_reported_not_recreated(monkeypatch):
    wire = _install(monkeypatch, {
        ("GET", f"/repos/{REPO}/branches/main"): _branch_ok("base123"),
    })

    payload = _call("github_branch",
                    {"repo": REPO, "branch": "main", "from_branch": "dev", "dry_run": False})

    assert payload["result"] == {"repo": REPO, "branch": "main", "sha": "base123",
                                 "created": False, "dry_run": False}
    assert wire.mutations() == []


@pytest.mark.parametrize("branch", ["", " ", "..", "a/../b", "-x", "a b", "x~1", "x^", "x:y",
                                    "x?", "x*", "x[1]", "x\\y", "x@{y", "x.lock", "a" * 300])
def test_a_bad_branch_is_E_VALIDATION(monkeypatch, branch):
    wire = _install(monkeypatch, {})
    payload = _call("github_branch", {"repo": REPO, "branch": branch})
    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION", branch
    assert wire.calls == []


# ── github_commit: the Git data API, base64 blobs, force=false ─────────────────────────────

def _commit_routes() -> dict:
    def blob(request, body):
        return httpx.Response(201, json={"sha": "blob-" + str(abs(hash(body["content"])) % 100000)})

    return {
        ("GET", f"/repos/{REPO}/git/ref/heads/main"):
            httpx.Response(200, json={"ref": "refs/heads/main", "object": {"sha": "base123"}}),
        ("GET", f"/repos/{REPO}/git/commits/base123"):
            httpx.Response(200, json={"sha": "base123", "tree": {"sha": "tree456"}}),
        ("POST", f"/repos/{REPO}/git/blobs"): blob,
        ("POST", f"/repos/{REPO}/git/trees"):
            httpx.Response(201, json={"sha": "newtree"}),
        ("POST", f"/repos/{REPO}/git/commits"):
            httpx.Response(201, json={"sha": "commit789",
                                      "html_url": f"https://github.com/{REPO}/commit/commit789"}),
        ("PATCH", f"/repos/{REPO}/git/refs/heads/main"):
            httpx.Response(200, json={"ref": "refs/heads/main",
                                      "object": {"sha": "commit789"}}),
    }


COMMIT_ARGS = {
    "repo": REPO,
    "branch": "main",
    "message": "feat: ship the thing",
    "files": [{"path": "src/app.py", "content": "print('hi')\n"},
              {"path": "notes/plan.md", "content": "plan"}],
}


def test_a_commit_dry_runs_by_default_and_sends_nothing(monkeypatch):
    wire = _install(monkeypatch, {})

    payload = _call("github_commit", COMMIT_ARGS)

    assert payload["ok"] is True and payload["tool"] == "github_commit"
    result = payload["result"]
    assert set(result) == COMMIT_KEYS
    assert result["dry_run"] is True and result["commit_sha"] is None and result["url"] is None
    assert [file["path"] for file in result["files"]] == ["src/app.py", "notes/plan.md"]
    assert [file["bytes"] for file in result["files"]] == [12, 4]
    assert all(set(file) == COMMIT_FILE_KEYS for file in result["files"])
    assert all(file["sha"] is None for file in result["files"])
    assert wire.calls == []


def test_a_real_commit_walks_the_git_data_api_with_base64_blobs(monkeypatch):
    wire = _install(monkeypatch, _commit_routes())

    payload = _call("github_commit", {**COMMIT_ARGS, "dry_run": False})

    result = payload["result"]
    assert set(result) == COMMIT_KEYS
    assert result["repo"] == REPO and result["branch"] == "main"
    assert result["commit_sha"] == "commit789"
    assert result["url"] == f"https://github.com/{REPO}/commit/commit789"
    assert result["dry_run"] is False
    assert [file["path"] for file in result["files"]] == ["src/app.py", "notes/plan.md"]

    blobs = [call for call in wire.calls if call["path"].endswith("/git/blobs")]
    assert len(blobs) == 2
    import base64
    assert blobs[0]["body"]["encoding"] == "base64"
    assert base64.b64decode(blobs[0]["body"]["content"]).decode() == "print('hi')\n"
    assert base64.b64decode(blobs[1]["body"]["content"]).decode() == "plan"

    tree = [call for call in wire.calls if call["path"].endswith("/git/trees")][0]
    assert tree["body"]["base_tree"] == "tree456"
    assert [entry["path"] for entry in tree["body"]["tree"]] == ["src/app.py", "notes/plan.md"]
    assert all(entry["type"] == "blob" and entry["mode"] == "100644"
               for entry in tree["body"]["tree"])

    commit = [call for call in wire.calls
              if call["method"] == "POST" and call["path"].endswith("/git/commits")][0]
    assert commit["body"]["message"] == "feat: ship the thing"
    assert commit["body"]["tree"] == "newtree"
    assert commit["body"]["parents"] == ["base123"]

    patch = [call for call in wire.calls if call["method"] == "PATCH"][0]
    assert patch["path"] == f"/repos/{REPO}/git/refs/heads/main"
    assert patch["body"] == {"sha": "commit789", "force": False}, "force is ALWAYS false"


def test_no_request_body_anywhere_ever_asks_to_force(monkeypatch):
    wire = _install(monkeypatch, _commit_routes())
    _call("github_commit", {**COMMIT_ARGS, "dry_run": False})
    for call in wire.calls:
        body = call["body"] or {}
        assert body.get("force") is not True, call
        assert "refspec" not in json.dumps(body).lower(), call


def test_a_force_argument_is_refused_by_name_and_never_reaches_the_network(monkeypatch):
    wire = _install(monkeypatch, _commit_routes())

    payload = _call("github_commit", {**COMMIT_ARGS, "dry_run": False, "force": True})

    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION"
    assert "force" in payload["error"]["message"].lower()
    assert wire.calls == []


@pytest.mark.parametrize("files", [
    [],
    "src/app.py",
    [{"path": "src/app.py"}],
    [{"path": "src/app.py", "content": 42}],
    [{"path": "../escape.py", "content": "x"}],
    [{"path": "/etc/passwd", "content": "x"}],
    [{"path": "C:/x.txt", "content": "x"}],
    [{"path": "a\\b.txt", "content": "x"}],
    [{"path": "a//b.txt", "content": "x"}],
    [{"path": "ok.txt", "content": "x"}] * 31,
])
def test_malformed_commit_files_are_E_VALIDATION(monkeypatch, files):
    wire = _install(monkeypatch, {})
    payload = _call("github_commit", {**COMMIT_ARGS, "files": files, "dry_run": False})
    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION", files
    assert wire.calls == []


def test_a_commit_message_is_required(monkeypatch):
    _install(monkeypatch, {})
    payload = _call("github_commit", {**COMMIT_ARGS, "message": "   "})
    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION"


# ── github_pr ─────────────────────────────────────────────────────────────────────────────

def test_a_pr_dry_runs_by_default_and_sends_nothing(monkeypatch):
    wire = _install(monkeypatch, {})

    payload = _call("github_pr", {"repo": REPO, "title": "Ship it",
                                  "head": "v25/feature", "base": "main"})

    assert payload["ok"] is True and payload["tool"] == "github_pr"
    assert payload["result"] == {"repo": REPO, "number": None, "url": None, "state": None,
                                 "head": "v25/feature", "base": "main", "dry_run": True}
    assert wire.calls == []


def test_a_real_pr_posts_the_frozen_payload_and_shape(monkeypatch):
    wire = _install(monkeypatch, {("POST", f"/repos/{REPO}/pulls"): httpx.Response(201, json={
        "number": 17, "html_url": f"https://github.com/{REPO}/pull/17",
        "state": "open", "head": {"ref": "v25/feature"}, "base": {"ref": "main"},
    })})

    payload = _call("github_pr", {"repo": REPO, "title": "Ship it", "head": "v25/feature",
                                  "base": "main", "body": "why", "dry_run": False})

    result = payload["result"]
    assert set(result) == PR_KEYS
    assert result == {"repo": REPO, "number": 17,
                      "url": f"https://github.com/{REPO}/pull/17", "state": "open",
                      "head": "v25/feature", "base": "main", "dry_run": False}
    body = wire.calls[0]["body"]
    assert body["title"] == "Ship it" and body["head"] == "v25/feature" and body["base"] == "main"
    assert body["body"] == "why" and body["draft"] is False


@pytest.mark.parametrize("arguments", [
    {"repo": REPO, "title": "", "head": "v25/feature", "base": "main"},
    {"repo": REPO, "title": "t", "head": "", "base": "main"},
    {"repo": REPO, "title": "t", "head": "v25/feature", "base": "main", "body": 42},
    {"repo": REPO, "title": "t", "head": "v25/feature", "base": "main", "dry_run": "false"},
])
def test_malformed_pr_arguments_are_E_VALIDATION(monkeypatch, arguments):
    wire = _install(monkeypatch, {})
    payload = _call("github_pr", arguments)
    assert payload["ok"] is False and payload["error"]["code"] == "E_VALIDATION", arguments
    assert wire.calls == []


# ── the sandbox still stands in front of every mutation ────────────────────────────────────

def test_sandbox_mode_refuses_a_mutation_before_the_tool_runs(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_mode", True)
    wire = _install(monkeypatch, _commit_routes())

    payload = _call("github_commit", {**COMMIT_ARGS, "dry_run": False})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"
    assert "SANDBOX_MODE" in payload["error"]["message"]
    assert wire.calls == []


def test_reads_still_work_while_sandboxed(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_mode", True)
    _install(monkeypatch, {("GET", f"/repos/{REPO}"): _repo_ok()})

    payload = _call("github_repo", {"repo": REPO})

    assert payload["ok"] is True


def test_a_branch_READ_works_while_sandboxed_but_a_branch_CREATE_does_not(monkeypatch):
    """`github_branch` is gated by the SAME predicate its runner uses: no `from_branch` = GET."""
    monkeypatch.setattr(settings, "sandbox_mode", True)
    wire = _install(monkeypatch, {("GET", f"/repos/{REPO}/branches/main"): _branch_ok()})

    read = _call("github_branch", {"repo": REPO, "branch": "main"})
    create = _call("github_branch",
                   {"repo": REPO, "branch": "feature/x", "from_branch": "main", "dry_run": False})

    assert read["ok"] is True and read["result"]["sha"] == "base123"
    assert create["ok"] is False and create["error"]["code"] == "E_POLICY_DENIED"
    assert wire.mutations() == []
