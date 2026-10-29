"""SV-W4-05 (wave 5): no `getattr(settings, <dynamic name>)` may name a phantom knob.

At the wave-4 HEAD twelve names were read through `getattr(settings, "<name>", <default>)` but
were NOT `Settings` fields; `Settings` is `extra="ignore"` (`app/config.py`), so an environment
value for any of them was silently discarded and the hardcoded default always won.  Forge closed
the four ws knobs and `deepseek_max_tokens` in wave 4; wave 5 closed the remaining seven:

    company_context_ttl_seconds        -> `company_service._cache_ttl()`
    deepseek_tool_loop_max_iterations  -> `deepseek_runtime._tool_loop_bound()`
    model_backend                      -> `crew_factory._spawn_facts()`
    redis_connect_timeout_seconds      -> `app/core/redis_client.py`
    redis_socket_timeout_seconds       -> `app/core/redis_client.py`
    secrets_file                       -> `secret_store._resolve_secrets_file()`, `claude_cli`
    stripe_signature_tolerance         -> `finance._signature_tolerance()`

This census is the regression guard: every literal name a product module reads from Settings must
be a declared field, and the ONE legitimate dynamic read (`secret_store._settings_fallback`, whose
key is caller-supplied) must be guarded by a `model_fields` membership check.  It is an AST scan,
not a grep: docstrings and comments cannot satisfy it.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from app.agents import deepseek_runtime
from app.api.v1 import finance as finance_api
from app.config import Settings, settings
from app.services import company_service, secret_store

BACKEND = pathlib.Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent
PRODUCT_ROOTS = (BACKEND / "app", BACKEND / "celery_app")

#: The new fields, their documented default, and the env name a deployment uses.
NEW_FIELDS: dict[str, tuple[object, str, str]] = {
    "company_context_ttl_seconds": (30.0, "COMPANY_CONTEXT_TTL_SECONDS", "30.0"),
    "deepseek_tool_loop_max_iterations": (
        4,
        "DEEPSEEK_TOOL_LOOP_MAX_ITERATIONS",
        "7",
    ),
    "model_backend": ("", "POLSIA_MODEL_BACKEND", "api"),
    "redis_connect_timeout_seconds": (1.0, "REDIS_CONNECT_TIMEOUT_SECONDS", "2.5"),
    "redis_socket_timeout_seconds": (1.0, "REDIS_SOCKET_TIMEOUT_SECONDS", "3.5"),
    "secrets_file": ("", "POLSIA_SECRETS_FILE", "data/custom_secrets.json"),
    "stripe_signature_tolerance": (300, "STRIPE_SIGNATURE_TOLERANCE", "45"),
}

#: The one dynamic reader that may exist: its key is a caller-supplied secret name, and it MUST
#: check `model_fields` before reading (so an unknown name can never claim a setting).
GUARDED_DYNAMIC_READERS = frozenset({"_settings_fallback"})

#: `_setting(name, default)` is a dynamic read BY DESIGN; its guard is different — every call site
#: must pass a literal, declared field name (`test_settings_helpers_are_only_called_with_real_field_names`).
LITERAL_HELPER_READERS = frozenset({"_setting"})


def _product_files() -> list[pathlib.Path]:
    return [
        path
        for root in PRODUCT_ROOTS
        if root.is_dir()
        for path in sorted(root.rglob("*.py"))
    ]


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8", errors="replace"))


def _enclosing_function(tree: ast.Module) -> dict[int, str]:
    """id(call node) -> name of the innermost function containing it."""
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    out: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call | ast.Attribute):
            continue
        current: ast.AST | None = node
        while current is not None and id(current) in parents:
            current = parents[id(current)]
            if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
                out[id(node)] = current.name
                break
    return out


def _settings_getattr_reads() -> list[tuple[pathlib.Path, int, ast.expr, str | None, str]]:
    """Every `getattr(settings, <expr>, ...)` in product code with its enclosing function."""
    reads = []
    for path in _product_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
        enclosing = _enclosing_function(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "getattr"):
                continue
            if len(node.args) < 2:
                continue
            base = node.args[0]
            if not (isinstance(base, ast.Name) and base.id == "settings"):
                continue
            reads.append((path, node.lineno, node.args[1], enclosing.get(id(node)),
                          ast.get_source_segment(text, node) or ""))
    return reads


def _helper_call_names() -> dict[str, list[tuple[str | None, bool, str]]]:
    """Helper name -> [(literal value or raw source, is_literal, location)] for product calls."""
    calls: dict[str, list[tuple[str | None, bool, str]]] = {}
    for path in _product_files():
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None
            )
            if name != "_setting":
                continue
            first = node.args[0] if node.args else None
            literal = isinstance(first, ast.Constant) and isinstance(first.value, str)
            calls.setdefault(name, []).append(
                (first.value if literal else (ast.unparse(first) if first is not None else None),
                 literal, f"{path.relative_to(ROOT)}:{node.lineno}")
            )
    return calls


def test_every_literal_getattr_settings_name_is_a_real_field() -> None:
    phantom = []
    for path, lineno, attr, _fn, _src in _settings_getattr_reads():
        if (
            isinstance(attr, ast.Constant)
            and isinstance(attr.value, str)
            and attr.value not in Settings.model_fields
        ):
            phantom.append(f"{path.relative_to(ROOT)}:{lineno} -> {attr.value}")
    assert phantom == [], (
        "these names are read from Settings with a getattr fallback but are NOT fields "
        f"(extra='ignore' makes their env value inert): {phantom}"
    )


def _function_source_contains(path: pathlib.Path, name: str, needle: str) -> bool:
    """Does the function `name` in `path` mention `needle` anywhere in its body (incl. guard)?"""
    text = path.read_text(encoding="utf-8", errors="replace")
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return needle in (ast.get_source_segment(text, node) or "")
    return False


def test_dynamic_settings_reads_are_guarded_or_literal_helpers() -> None:
    for path, lineno, attr, fn, src in _settings_getattr_reads():
        if isinstance(attr, ast.Constant):
            continue
        location = f"{path.relative_to(ROOT)}:{lineno}"
        assert fn in (GUARDED_DYNAMIC_READERS | LITERAL_HELPER_READERS), (
            f"{location}: {src} reads Settings through a computed name in {fn!r}; either make it "
            "a literal field read or add a model_fields guard and declare it here"
        )
        if fn in GUARDED_DYNAMIC_READERS:
            assert _function_source_contains(path, fn, "model_fields"), (
                f"{location}: the dynamic reader must be guarded by model_fields membership"
            )


def test_settings_helpers_are_only_called_with_real_field_names() -> None:
    calls = _helper_call_names()
    assert "_setting" in calls, "the _setting helper disappeared — update this census"
    problems = []
    for value, literal, location in calls["_setting"]:
        if not literal:
            problems.append(f"{location}: _setting called with a non-literal name {value!r}")
        elif value not in Settings.model_fields:
            problems.append(f"{location}: _setting({value!r}) is not a Settings field")
    assert problems == [], problems


def test_the_remaining_dynamic_reader_treats_an_unknown_name_as_unset(monkeypatch) -> None:
    """`_settings_fallback` is the only dynamic read: it must never invent a value."""
    monkeypatch.setattr(settings, "deepseek_api_key", "sk_field_value", raising=False)
    assert secret_store._settings_fallback("DEEPSEEK_API_KEY") == "sk_field_value"
    assert secret_store._settings_fallback("definitely_not_a_setting") == ""
    assert secret_store._settings_fallback("another-unknown") == ""


@pytest.mark.parametrize("field", sorted(NEW_FIELDS))
def test_each_closed_knob_is_a_declared_field_with_the_documented_default(field: str) -> None:
    default, _env_name, _value = NEW_FIELDS[field]
    assert field in Settings.model_fields, f"{field} is not a Settings field"
    assert Settings.model_fields[field].default == default


@pytest.mark.parametrize("field", sorted(NEW_FIELDS))
def test_each_closed_knob_is_declarable_from_the_environment(monkeypatch, field: str) -> None:
    _default, env_name, raw = NEW_FIELDS[field]
    monkeypatch.setenv(env_name, raw)
    built = Settings(_env_file=None)
    configured = getattr(built, field)
    expected = raw if isinstance(Settings.model_fields[field].default, str) else type(
        Settings.model_fields[field].default
    )(raw)
    assert configured == expected, f"{env_name} did not reach Settings.{field}"


@pytest.mark.parametrize("field", sorted(NEW_FIELDS))
def test_each_closed_knob_is_documented_in_the_env_template(field: str) -> None:
    _default, env_name, _raw = NEW_FIELDS[field]
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(rf"^{env_name}=", text, re.MULTILINE), (
        f".env.example must document {env_name} (the template claims a 1:1 field mapping)"
    )


def test_the_readers_see_the_configured_values(monkeypatch) -> None:
    """A real field is only real if its READER changes when the value changes."""
    monkeypatch.setattr(settings, "company_context_ttl_seconds", 7.5)
    assert company_service._cache_ttl() == 7.5

    monkeypatch.setattr(settings, "deepseek_tool_loop_max_iterations", 7)
    assert deepseek_runtime._tool_loop_bound() == 7
    monkeypatch.setattr(settings, "deepseek_tool_loop_max_iterations", 0)
    assert deepseek_runtime._tool_loop_bound() == 1, "the floor clamps to 1 provider call"
    monkeypatch.setattr(settings, "deepseek_tool_loop_max_iterations", 99)
    assert deepseek_runtime._tool_loop_bound() == 12, "the cap clamps a runaway loop"

    monkeypatch.setattr(settings, "stripe_signature_tolerance", 45)
    assert finance_api._signature_tolerance() == 45
    monkeypatch.setattr(settings, "stripe_signature_tolerance", 0)
    assert finance_api._signature_tolerance() == 300, "0 is the DEFAULT, never 'off'"


def test_the_secrets_file_reader_uses_the_field(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(secret_store.SECRETS_FILE_ENV_VAR, raising=False)
    custom = tmp_path / "custom-secrets.json"
    monkeypatch.setattr(settings, "secrets_file", str(custom))
    assert secret_store._resolve_secrets_file() == custom
    monkeypatch.setenv(secret_store.SECRETS_FILE_ENV_VAR, str(tmp_path / "env-wins.json"))
    assert secret_store._resolve_secrets_file() == tmp_path / "env-wins.json", (
        "the explicit environment variable keeps precedence over the field"
    )
