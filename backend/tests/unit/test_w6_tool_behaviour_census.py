"""WAVE 6 / F-W6-01 — the general-tool behavioural census: schema vs implementation, all 17 tools.

WHY THIS FILE EXISTS.  Wave 4 proved ONE behavioural property per (tool, required argument) pair:
omit it and the implementation refuses by name (22 pairs; `test_w4_capability_census.py` group F
states "21" in its docstring — this lane re-derived the number from `TOOL_SPECS` and it is 22:
`forge/raw/w6/spec-table.txt`).  That census asked one question of each tool.  The wave-6 job is to
validate the actual implementations against their SPECS — required args, return shapes, refusals,
caps, sandbox behaviour — so this file adds the dimensions the wave-4 probes never touched, and
re-derives every table from `TOOL_SPECS` instead of typing a second copy:

  A. the DECLARED SURFACE — every spec declares `additionalProperties: false`, and an argument a
     tool's schema does not declare is refused by name rather than silently dropped (F-W6-01; the
     4 GitHub tools did this, the other 13 did not, until this wave);
  B. TYPE drift — a declared argument sent with the wrong JSON type is refused, not coerced;
  C. CAPS — the documented file/listing/result bounds are enforced with the real numbers in the
     refusal message (256 KiB write, 256 KiB read, the listing bound, the 100k-char seam);
  D. the REFUSAL ENVELOPE — `{ok: false, error: {code, message, how_to_fix, context}}` with a
     known `E_*` code, for every tool, produced with NO I/O and NO network;
  E. the SUCCESS ENVELOPE — `{ok: true, tool, result}` with the documented result keys, for the
     tools that answer without a provider (the code tools and the pricing catalog);
  F. the METERED per-run cap and the sandbox/allowlist legs that sit in front of a tool call.

Test-double boundary (§7): `tmp_path` is a real directory tree; the monkeypatched `_RUNNERS` entry
in the result-ceiling test replaces a runner to feed the SEAM under test, and the HTTP transport is
never installed (nothing here reaches the network — the metered-cap probe relies on the missing-key
refusal happening AFTER the slot is charged, which is the documented order).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents import policy_firewall as pf
from app.agents import tool_registry as registry
from app.config import settings
from app.core.errors import HTTP_STATUS_BY_CODE

SPEC_NAMES = sorted(spec["function"]["name"] for spec in registry.TOOL_SPECS)
DECLARED = {
    spec["function"]["name"]: frozenset(
        spec["function"]["parameters"].get("properties") or {}
    )
    for spec in registry.TOOL_SPECS
}
REQUIRED = {
    spec["function"]["name"]: tuple(spec["function"]["parameters"].get("required") or ())
    for spec in registry.TOOL_SPECS
}

#: The tool-local codes the registry may answer with that are not API status rows.
_TOOL_LOCAL_CODES = {"E_UNAVAILABLE", "E_RATE_LIMIT", "E_BALANCE"}


def _agent_for(tool: str) -> str:
    """The first agent whose allowlist grants `tool` (derived; never a hand-typed second copy)."""
    for agent_type in sorted(pf.AGENT_CAPABILITIES):
        if tool in pf.AGENT_CAPABILITIES[agent_type].agent_tools:
            return agent_type
    raise AssertionError(f"no agent holds {tool!r}")


@pytest.fixture(autouse=True)
def _no_provider_key(monkeypatch):
    """No DeepSeek key for ANY test in this file: every provider path must refuse, none may dial.

    `web_search` is a real provider call, and this file never wants one.  The resolver is stubbed
    to "no key" so the census is offline by construction on a developer machine that has a key in
    its environment — the refusal path (`E_CONFIG`, "no key configured") is then the one measured.
    """
    monkeypatch.setattr(registry, "_deepseek_api_key", lambda: None)


def _call(name: str, arguments: dict | None = None, *, agent_type: str | None = None) -> dict:
    raw = registry.execute_tool(
        name, arguments if arguments is not None else {},
        agent_type=agent_type or _agent_for(name))
    assert isinstance(raw, str), f"{name}: execute_tool must return text, got {type(raw).__name__}"
    return json.loads(raw)


def _assert_named_refusal(payload: dict, *, tool: str) -> dict:
    """The ONE refusal envelope, checked structurally; returns the error object."""
    assert payload.get("ok") is False, f"{tool}: expected a refusal, got {payload!r}"
    error = payload.get("error") or {}
    assert set(error) >= {"code", "message", "how_to_fix", "context"}, error
    code = error["code"]
    assert isinstance(code, str) and code.startswith("E_"), code
    assert code in HTTP_STATUS_BY_CODE or code in _TOOL_LOCAL_CODES, (
        f"{tool}: {code!r} is not a known refusal code (an unnamed code cannot be handled)")
    assert str(error["message"]).strip(), f"{tool}: the refusal names no reason"
    assert str(error["how_to_fix"]).strip(), f"{tool}: the refusal says nothing about the fix"
    assert isinstance(error["context"], dict), f"{tool}: context must be an object"
    assert error["context"].get("tool") == tool, (
        f"{tool}: the refusal context must name the tool it came from: {error['context']!r}")
    return error


# ── A. the declared surface: `additionalProperties: false` is honoured ───────────────────────


def test_every_spec_declares_additional_properties_false():
    """The contract F-W6-01 makes the implementation honour has to be in every spec."""
    missing = [
        spec["function"]["name"] for spec in registry.TOOL_SPECS
        if spec["function"]["parameters"].get("additionalProperties") is not False
    ]
    assert not missing, f"these specs do not forbid undeclared arguments: {missing}"


def test_the_declared_argument_table_is_derived_from_the_specs():
    """A hand-typed second copy is how a table drifts; this one cannot."""
    assert set(registry._DECLARED_ARGUMENTS) == set(SPEC_NAMES)
    assert dict(registry._DECLARED_ARGUMENTS) == DECLARED


@pytest.mark.parametrize("name", SPEC_NAMES)
def test_an_undeclared_argument_is_refused_by_name_before_any_io(name: str):
    """F-W6-01, all 17 tools: the key the schema never declared is REFUSED, never dropped.

    The probe names a key no spec declares, so every tool must answer the same way: a named
    `E_VALIDATION` that names the offending key and the accepted set.  This fires before any
    filesystem, provider or network work (the `code_write` case proves that separately below).
    """
    payload = _call(name, {"__wave6_undeclared__": 1})

    error = _assert_named_refusal(payload, tool=name)
    assert error["code"] == "E_VALIDATION", error
    assert "__wave6_undeclared__" in error["message"], error["message"]
    assert error["context"].get("unknown") == ["__wave6_undeclared__"], error["context"]
    accepted = sorted(DECLARED[name])
    if accepted:
        assert all(key in error["message"] for key in accepted), (
            f"{name}: the refusal must list the arguments the tool DOES accept: "
            f"{error['message']!r}")
    else:
        assert "no arguments" in error["message"], error["message"]


def test_a_refused_undeclared_argument_writes_nothing(tmp_path, monkeypatch):
    """The check sits before the runner: a refusal cannot leave half a file behind."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "code_workspace_dir", str(workspace))

    payload = _call("code_write", {"path": "a.txt", "content": "x", "__wave6_undeclared__": 1},
                    agent_type="code_generation")

    assert _assert_named_refusal(payload, tool="code_write")["code"] == "E_VALIDATION"
    assert not (workspace / "a.txt").exists(), "a refused call wrote the file anyway"


def test_the_github_force_hint_survives_the_generalised_check():
    """The specific sentence the four GitHub tools carried is kept, not flattened away."""
    payload = _call("github_commit",
                    {"repo": "owner/name", "branch": "b", "message": "m",
                     "files": [{"path": "a.txt", "content": "x"}], "force": True},
                    agent_type="code_generation")

    error = _assert_named_refusal(payload, tool="github_commit")
    assert error["code"] == "E_VALIDATION"
    assert "force" in error["message"].lower()
    assert "force-push" in error["message"].lower(), (
        "the GitHub surface's own limit must still be explained, not just the spelling")
    assert "force=false" in error["message"].lower()


def test_the_allowlist_gate_still_answers_before_the_argument_check():
    """Order pin: a tool the agent does not hold is refused for THAT reason, not for a typo."""
    payload = _call("github_commit", {"repo": "owner/name", "branch": "b", "message": "m",
                                      "files": [], "__wave6_undeclared__": 1},
                    agent_type="social_media")

    error = _assert_named_refusal(payload, tool="github_commit")
    assert error["code"] == "E_POLICY_DENIED", error
    assert error["context"].get("policy_code"), "a policy denial names its firewall reason"


# ── B. type drift: a declared argument with the wrong JSON type is refused ──────────────────

#: (tool, arguments, the argument that is wrong) — every row is a TYPE violation, not a shape one.
_TYPE_PROBES: list[tuple[str, dict]] = [
    ("web_fetch", {"url": "https://example.com", "max_chars": "many"}),
    ("web_fetch", {"url": 123}),
    ("web_search", {"query": ["soy", "wax"]}),
    ("web_search", {"query": "x", "count": [5]}),
    ("research_fetch", {"url": "https://example.com", "max_chars": "big"}),
    ("fx_convert", {"amount": "ten", "base": "USD", "quote": "EUR"}),
    ("fx_convert", {"amount": 10, "base": 1, "quote": "EUR"}),
    ("signals_read", {"limit": "ten"}),
    ("phone_read", {"thread_id": "dana"}),
    ("phone_read", {"limit": True}),
    ("github_commit", {"repo": "owner/name", "branch": "b", "message": "m", "files": "a.txt"}),
    ("github_commit", {"repo": "owner/name", "branch": "b", "message": "m",
                       "files": [{"path": "a.txt", "content": 42}]}),
    ("github_pr", {"repo": "owner/name", "title": 5, "head": "f", "base": "main"}),
    ("code_write", {"path": 7, "content": "x"}),
    ("code_write", {"path": "a.txt", "content": 7}),
    ("code_read", {"path": ["a.txt"]}),
]


@pytest.mark.parametrize(("name", "arguments"), _TYPE_PROBES,
                         ids=[f"{name}-{index}" for index, (name, _) in
                              enumerate(_TYPE_PROBES)])
def test_a_declared_argument_with_the_wrong_type_is_refused(name: str, arguments: dict):
    """A JSON `number` for a string argument (or vice versa) must not be coerced into a call."""
    payload = _call(name, arguments)

    error = _assert_named_refusal(payload, tool=name)
    assert error["code"] == "E_VALIDATION", error


# ── C. caps: the documented bounds are real numbers in the refusal ──────────────────────────


def test_the_write_cap_is_256_kib_and_the_refusal_names_both_numbers(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "code_workspace_dir", str(workspace))
    oversize = "x" * (registry.CODE_WRITE_MAX_BYTES + 1)

    payload = _call("code_write", {"path": "big.txt", "content": oversize},
                    agent_type="code_generation")

    error = _assert_named_refusal(payload, tool="code_write")
    assert error["code"] == "E_VALIDATION"
    assert str(registry.CODE_WRITE_MAX_BYTES) in error["message"], error["message"]
    assert str(len(oversize)) in error["message"], error["message"]
    assert not (workspace / "big.txt").exists(), "the over-cap write reached the filesystem"
    assert registry.CODE_WRITE_MAX_BYTES == 256 * 1024


def test_the_read_cap_is_256_kib(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "code_workspace_dir", str(workspace))
    (workspace / "big.txt").write_bytes(b"x" * (registry.CODE_READ_MAX_BYTES + 1))

    payload = _call("code_read", {"path": "big.txt"}, agent_type="code_generation")

    error = _assert_named_refusal(payload, tool="code_read")
    assert error["code"] == "E_VALIDATION"
    assert str(registry.CODE_READ_MAX_BYTES) in error["message"], error["message"]
    assert registry.CODE_READ_MAX_BYTES == 256 * 1024


def test_the_listing_bound_truncates_and_says_so(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "many").mkdir(parents=True)
    monkeypatch.setattr(settings, "code_workspace_dir", str(workspace))
    for index in range(registry.CODE_LIST_MAX_ENTRIES + 3):
        (workspace / "many" / f"f{index:04d}.txt").write_text("x", encoding="utf-8")

    payload = _call("code_list", {"path": "many"}, agent_type="code_generation")

    assert payload["ok"] is True, payload
    result = payload["result"]
    assert result["returned"] == registry.CODE_LIST_MAX_ENTRIES
    assert result["truncated"] is True
    assert str(registry.CODE_LIST_MAX_ENTRIES) in result["note"], result["note"]


def test_the_result_ceiling_refuses_instead_of_truncating(monkeypatch):
    """F-31's backstop at the ONE seam: a runner that forgot its own bound is a named refusal."""
    huge = {"ok": True, "tool": "pricing_catalog",
            "result": {"rows": ["y" * 1000] * (registry.TOOL_RESULT_CEILING // 500)}}
    monkeypatch.setitem(registry._RUNNERS, "pricing_catalog", lambda **_: huge)

    payload = json.loads(
        registry.execute_tool("pricing_catalog", {}, agent_type="orchestrator"))

    error = _assert_named_refusal(payload, tool="pricing_catalog")
    assert error["code"] == "E_BUDGET_EXCEEDED"
    assert error["context"]["ceiling"] == registry.TOOL_RESULT_CEILING
    assert error["context"]["chars"] > registry.TOOL_RESULT_CEILING
    assert "NOT truncated" in error["message"], error["message"]


def test_the_per_run_metered_cap_is_enforced_and_named():
    """Ten metered calls per run; the eleventh is refused and NOTHING is sent or billed.

    The refusal below is the missing-key `E_CONFIG` each `web_search` answers — reached AFTER the
    slot is charged, which is the documented order ("a refusal must never spend a slot" refers to
    the gate/argument refusals above it).  No key, no transport, no provider call.
    """
    budget = registry.begin_run_tool_budget()
    try:
        codes = [
            _call("web_search", {"query": f"probe {index}"}, agent_type="competitor_research")
            ["error"]["code"]
            for index in range(registry.METERED_CALLS_PER_RUN)
        ]
        over = _call("web_search", {"query": "one too many"},
                     agent_type="competitor_research")
    finally:
        registry.end_run_tool_budget(budget)

    assert set(codes) == {"E_CONFIG"}, codes
    error = _assert_named_refusal(over, tool="web_search")
    assert error["code"] == "E_BUDGET_EXCEEDED", error
    assert error["context"]["cap"] == registry.METERED_CALLS_PER_RUN
    assert error["context"]["used"] == registry.METERED_CALLS_PER_RUN
    assert frozenset({"research_fetch", "web_search"}) == registry.METERED_TOOLS


# ── D. the refusal envelope, produced with no I/O at all ────────────────────────────────────


@pytest.mark.parametrize("name", SPEC_NAMES)
def test_every_tool_answers_a_named_refusal_envelope(name: str):
    """One uniform shape across the registry: a loop can always finish honestly from it."""
    _assert_named_refusal(_call(name, {"__wave6_undeclared__": 1}), tool=name)


# ── E. the success envelope: `{ok, tool, result}` with the documented keys ──────────────────


def test_the_code_tools_success_envelope_is_the_documented_one(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "code_workspace_dir", str(workspace))

    written = _call("code_write", {"path": "src/app.py", "content": "print('hi')\n"},
                    agent_type="code_generation")
    read_back = _call("code_read", {"path": "src/app.py"}, agent_type="code_generation")
    listed = _call("code_list", {"path": "src"}, agent_type="code_generation")

    assert written == {"ok": True, "tool": "code_write",
                       "result": {"path": "src/app.py", "bytes": 12}}
    assert read_back["ok"] is True and read_back["tool"] == "code_read"
    assert set(read_back["result"]) == {"path", "text", "bytes"}
    assert read_back["result"]["text"] == "print('hi')\n", (
        "the text must be byte-exact (a newline rewrite would corrupt a deliverable)")
    assert read_back["result"]["bytes"] == 12
    assert listed["ok"] is True and listed["tool"] == "code_list"
    assert set(listed["result"]) == {"path", "entries", "returned", "truncated", "note"}
    assert [entry["path"] for entry in listed["result"]["entries"]] == ["src/app.py"]
    assert listed["result"]["entries"][0]["kind"] == "file"


def test_a_run_scoped_workspace_is_isolated_and_validated(tmp_path, monkeypatch):
    """The V25 run id keeps two runs apart and refutes a hostile one by name."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "code_workspace_dir", str(workspace))

    first = json.loads(registry.execute_tool(
        "code_write", {"path": "note.md", "content": "run seven"},
        agent_type="code_generation", run_id="7"))
    second = json.loads(registry.execute_tool(
        "code_read", {"path": "note.md"}, agent_type="code_generation", run_id="8"))

    assert first["ok"] is True and first["result"]["path"] == "note.md"
    assert _assert_named_refusal(second, tool="code_read")["code"] == "E_NOT_FOUND"
    assert json.loads(registry.execute_tool(
        "code_write", {"path": "note.md", "content": "x"},
        agent_type="code_generation", run_id=".."))["error"]["code"] == "E_VALIDATION"


def test_the_pricing_catalog_envelope_carries_its_provenance():
    """A research snapshot must never be presented as live pricing."""
    payload = _call("pricing_catalog", {}, agent_type="business_planning")

    assert payload["ok"] is True, payload
    assert payload["tool"] == "pricing_catalog"
    result = payload["result"]
    assert set(result) == {"provenance", "row_count", "rows_returned", "truncated", "rows"}
    assert result["row_count"] == len(result["rows"]) or result["truncated"] is True
    assert result["provenance"], "the catalog's as-of/staleness provenance is part of the answer"


def test_the_web_fetch_refusals_are_named_without_touching_the_network():
    """Scheme, port, max_chars — the three checks that run before `pinned_http_get`."""
    scheme = _call("web_fetch", {"url": "ftp://example.com/x"})
    blank = _call("web_fetch", {"url": "   "})
    zero = _call("web_fetch", {"url": "https://example.com", "max_chars": 0})

    assert _assert_named_refusal(scheme, tool="web_fetch")["code"] == "E_VALIDATION"
    assert _assert_named_refusal(blank, tool="web_fetch")["code"] == "E_VALIDATION"
    assert _assert_named_refusal(zero, tool="web_fetch")["code"] == "E_VALIDATION"
    assert "http" in scheme["error"]["message"], scheme["error"]["message"]


# ── F. the legs in FRONT of a tool call ─────────────────────────────────────────────────────


def test_an_unclassified_tool_is_refused_with_E_CONFIG(monkeypatch):
    """Fails CLOSED: a spec with no class entry never runs and never defaults to a read."""
    probe_name = "wave6_probe_unclassified"
    monkeypatch.setitem(registry._SPEC_BY_NAME, probe_name, {
        "type": "function", "function": {"name": probe_name, "parameters": {}}})

    payload = json.loads(registry.execute_tool(
        probe_name, {}, agent_type="code_generation"))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_CONFIG"
    assert "NOT executed" in payload["error"]["message"]


def test_a_denied_tool_is_refused_before_the_argument_check():
    """The allowlist is checked against the REAL grant table, not against a test double."""
    holder = _agent_for("web_search")
    other = next(agent for agent in sorted(pf.AGENT_CAPABILITIES)
                 if "web_search" not in pf.AGENT_CAPABILITIES[agent].agent_tools)

    allowed = _call("web_search", {"query": "wax"}, agent_type=holder)
    denied = _call("web_search", {"query": "wax"}, agent_type=other)

    assert allowed["error"]["code"] == "E_CONFIG", "no key is configured in tests"
    assert _assert_named_refusal(denied, tool="web_search")["code"] == "E_POLICY_DENIED"


def test_the_workspace_escape_refusals_are_named(tmp_path, monkeypatch):
    """``..``, an absolute path and a drive letter never reach the filesystem."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "code_workspace_dir", str(workspace))

    for path in ("../escape.txt", "/etc/passwd", "C:/x.txt", "..\\escape.txt"):
        payload = _call("code_write", {"path": path, "content": "x"},
                        agent_type="code_generation")
        error = _assert_named_refusal(payload, tool="code_write")
        assert error["code"] == "E_VALIDATION", (path, error)
    assert sorted(item.name for item in Path(workspace).iterdir()) == [], (
        "a refused path wrote something outside the workspace root")


def test_a_missing_file_is_E_NOT_FOUND_not_an_empty_read(tmp_path, monkeypatch):
    """An absent file must never read as "the file is empty"."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "code_workspace_dir", str(workspace))

    payload = _call("code_read", {"path": "nope.md"}, agent_type="code_generation")

    error = _assert_named_refusal(payload, tool="code_read")
    assert error["code"] == "E_NOT_FOUND"
    assert "no file" in error["message"].lower(), error["message"]


# ── G. the EGRESS floor at the tool seam (sandbox behaviour) ────────────────────────────────


@pytest.mark.parametrize(("url", "reason"), [
    ("http://169.254.169.254/latest/meta-data/", "link-local"),
    ("http://10.0.0.5/", "private"),
    ("http://192.168.1.10/", "private"),
])
def test_web_fetch_refuses_a_non_public_address_by_name(url: str, reason: str):
    """The egress floor is part of `web_fetch`'s contract: a private address never gets dialled.

    A literal address needs no DNS, so this check is offline and instantaneous — the refusal comes
    from the address classification, and it cites the reason.  These are the SSRF cases (the cloud
    metadata endpoint above all), so the code must be the policy refusal, not a transport error.
    """
    payload = _call("web_fetch", {"url": url}, agent_type="competitor_research")

    error = _assert_named_refusal(payload, tool="web_fetch")
    assert error["code"] == "E_POLICY_DENIED", error
    assert reason in error["message"].lower(), error["message"]
    assert error["context"].get("egress_code") == "E_EGRESS_BLOCKED", error["context"]


def test_web_fetch_refuses_a_non_web_port_before_dialling():
    """Port 8000 on a loopback address is not a web fetch, whatever resolves."""
    payload = _call("web_fetch", {"url": "http://127.0.0.1:8000/admin"},
                    agent_type="competitor_research")

    error = _assert_named_refusal(payload, tool="web_fetch")
    assert error["code"] == "E_POLICY_DENIED", error
    assert "127.0.0.1" in error["message"], error["message"]


def test_the_health_tools_refuse_a_private_target_by_name():
    """`link_status`/`domain_health` keep their own caller-input refusal for a private target."""
    link = _call("link_status", {"url": "http://127.0.0.1:8000/"})
    domain = _call("domain_health", {"host": "localhost"})

    link_error = _assert_named_refusal(link, tool="link_status")
    domain_error = _assert_named_refusal(domain, tool="domain_health")
    assert link_error["code"] == "E_VALIDATION"
    assert "not allowed" in link_error["message"].lower(), link_error["message"]
    assert domain_error["code"] == "E_VALIDATION"
    assert "bare hostname" in domain_error["message"].lower(), domain_error["message"]


def test_a_github_mutation_is_refused_before_the_network_while_sandboxed():
    """The sandbox leg stands in front of every external effect, and nothing is sent.

    The refusal is `E_POLICY_DENIED` on the TOOL transport by design (D3-R1 adopted only the
    cancellation code from `PolicyDecision.e_code`; the firewall's own `e_code` for this same
    decision is `E_SANDBOX_BLOCKED`, which the side-effect transport reports).  Both are 403 and
    the message names SANDBOX_MODE — recorded here so the divergence is pinned, not accidental.
    """
    payload = _call("github_commit",
                    {"repo": "owner/name", "branch": "b", "message": "m",
                     "files": [{"path": "a.txt", "content": "x"}], "dry_run": False},
                    agent_type="code_generation")

    error = _assert_named_refusal(payload, tool="github_commit")
    assert error["code"] == "E_POLICY_DENIED", error
    assert "SANDBOX_MODE" in error["message"], error["message"]
