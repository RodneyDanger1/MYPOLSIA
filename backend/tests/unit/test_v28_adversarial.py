"""V28 / L6 — adversarial truth: the docs' current-state claims must match the tree.

These are NOT style tests. Each one pins a sentence that tells an operator something
FALSE about the shipped product (a route count, a surface that is missing from the
feature tour, a capability the docs deny while the code implements it).  A doc claim
that the code contradicts is the same defect class as a response that lies: it makes
the owner believe a capability is absent (or present) when it is not.

Every number is MEASURED here from the tree (routes from `app.openapi()`, pages from
`frontend/src/app/**/page.tsx`, nav rows from `nav-items.ts`, modules from
`backend/app/api/v1/*.py`, tables from the ORM `__tablename__` declarations), never
copied from another doc.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"
COMPLETENESS = REPO_ROOT / "COMPLETENESS.md"
NAV_ITEMS = REPO_ROOT / "frontend" / "src" / "components" / "layout" / "nav-items.ts"


def _read(path: Path) -> str:
    assert path.is_file(), f"doc/source missing: {path}"
    return path.read_text(encoding="utf-8")


def _api_v1_operations() -> int:
    """Every method+path operation under `/api/v1`, from the app's own OpenAPI schema."""
    from app.main import create_app

    schema = create_app().openapi()
    methods = {"get", "post", "put", "patch", "delete"}
    return sum(
        1
        for path, operations in schema["paths"].items()
        if path.startswith("/api/v1")
        for method in operations
        if method in methods
    )


def _route_modules() -> int:
    """The route modules the docs count: `backend/app/api/v1/*.py`, minus `__init__`."""
    root = REPO_ROOT / "backend" / "app" / "api" / "v1"
    return len([p for p in root.glob("*.py") if p.name != "__init__.py"])


def _page_count() -> int:
    return len(list((REPO_ROOT / "frontend" / "src" / "app").rglob("page.tsx")))


def _nav_hrefs() -> list[str]:
    return re.findall(r'\{\s*href:\s*"([^"]+)"', _read(NAV_ITEMS))


def _table_count() -> int:
    models = REPO_ROOT / "backend" / "app" / "models"
    return sum(
        len(re.findall(r"__tablename__\s*=", path.read_text(encoding="utf-8")))
        for path in models.glob("*.py")
    )


# --------------------------------------------------------------------------- #
# 1. The README's route count is a present-tense claim about THIS tree.
# --------------------------------------------------------------------------- #
def test_readme_route_count_matches_the_live_api_surface():
    readme = _read(README)
    match = re.search(r"The `/api/v1` surface is (\d+)\s+(?:routes|method\+path operations)", readme)
    assert match, "README no longer states the /api/v1 route count — update this pin"
    claimed = int(match.group(1))
    measured = _api_v1_operations()
    assert claimed == measured, (
        f"README.md:{readme[: match.start()].count(chr(10)) + 1} claims the /api/v1 surface is "
        f"{claimed} routes; the app's OpenAPI schema serves {measured} method+path operations. "
        "A stale count is a false statement about the product surface."
    )


# --------------------------------------------------------------------------- #
# 2. COMPLETENESS §2's counts are present tense too.
# --------------------------------------------------------------------------- #
def test_completeness_surface_counts_match_the_tree():
    text = _read(COMPLETENESS)
    line = text[: text.find("`page.tsx`")].count("\n") + 1

    pages = re.search(r"\*\*(\d+) `page\.tsx`\*\* files", text)
    nav = re.search(r"\*\*(\d+) top-level routes\*\* in the navigation", text)
    ops = re.search(r"exposes \*\*(\d+) method\+path operations\*\* across \*\*(\d+) modules\*\*", text)
    assert pages and nav and ops, "COMPLETENESS §2 count sentences changed shape — update this pin"

    assert int(pages.group(1)) == _page_count(), (
        f"COMPLETENESS.md:~{line} claims {pages.group(1)} page.tsx files; "
        f"the tree has {_page_count()}"
    )
    assert int(nav.group(1)) == len(_nav_hrefs()), (
        f"COMPLETENESS.md:~{line} claims {nav.group(1)} top-level nav routes; "
        f"nav-items.ts has {len(_nav_hrefs())}"
    )
    assert int(ops.group(1)) == _api_v1_operations(), (
        f"COMPLETENESS.md:~{line} claims {ops.group(1)} method+path operations; "
        f"the app serves {_api_v1_operations()}"
    )
    assert int(ops.group(2)) == _route_modules(), (
        f"COMPLETENESS.md:~{line} claims {ops.group(2)} route modules; "
        f"backend/app/api/v1 has {_route_modules()}"
    )


# --------------------------------------------------------------------------- #
# 3. Every shipped nav page appears in the COMPLETENESS surface table.
# --------------------------------------------------------------------------- #
def test_completeness_surface_table_names_every_nav_page():
    text = _read(COMPLETENESS)
    missing = [
        href
        for href in _nav_hrefs()
        if f"frontend/src/app{href}/page.tsx" not in text
    ]
    assert not missing, (
        "COMPLETENESS §2 ('The surfaces — feature, page, API') omits shipped nav "
        f"page(s): {', '.join(sorted(missing))}. The table is the document's own map of "
        "every surface; a page the navigation serves but the map does not is a false map."
    )


# --------------------------------------------------------------------------- #
# 4. The README must not DENY a capability the code ships.
# --------------------------------------------------------------------------- #
def test_readme_does_not_deny_shipped_capabilities():
    readme = _read(README)
    # 4a. Render deploy: V25 shipped `services/deploy_service.py`, the
    #     POST /api/v1/sites/{slug}/deploy route, and the Render integration row.
    assert "there is no Deployment agent and never was" not in readme, (
        "README still says Render deployment is 'not implemented' while "
        "backend/app/services/deploy_service.py and POST /api/v1/sites/{slug}/deploy "
        "ship it (dry-run by default; E_CONFIG without the two Render variables)."
    )
    assert "RENDER_API_KEY" in readme, (
        "README's Integrations section must name RENDER_API_KEY once the deploy adapter exists"
    )
    # 4b. DeepSeek tool loop: `deepseek_runtime._tool_specs_for` sends the registry's
    #     TOOL_SPECS and executes every tool_calls turn; competitor_research is granted
    #     web_fetch + web_search.
    assert "cannot call tools yet" not in readme, (
        "README still says the DeepSeek-first runtime cannot call tools; "
        "backend/app/agents/deepseek_runtime.py runs the registry tool loop "
        "(`_execute_tool_calls`) and policy_firewall grants competitor_research "
        "web_fetch + web_search."
    )
    assert "web_search" in readme or "web_fetch" in readme, (
        "README's web-search bullet must name the registry tool the runtime actually runs"
    )


# --------------------------------------------------------------------------- #
# 5. The layout's ORM table count.
# --------------------------------------------------------------------------- #
def test_readme_orm_table_count_matches_the_models():
    readme = _read(README)
    match = re.search(r"SQLAlchemy ORM \((\d+) tables\)", readme)
    assert match, "README no longer states the ORM table count — update this pin"
    measured = _table_count()
    assert int(match.group(1)) == measured, (
        f"README claims {match.group(1)} ORM tables; backend/app/models declares "
        f"{measured} __tablename__ tables"
    )
    completeness = _read(COMPLETENESS)
    other = re.search(r"PostgreSQL 16\*\* \((\d+) tables\)", completeness)
    assert other, "COMPLETENESS no longer states the ORM table count — update this pin"
    assert int(other.group(1)) == measured, (
        f"COMPLETENESS.md claims {other.group(1)} tables; the ORM declares {measured}"
    )


# --------------------------------------------------------------------------- #
# 6. The feature tour lists every page the navigation serves.
# --------------------------------------------------------------------------- #
def test_readme_feature_tour_lists_every_nav_page():
    readme = _read(README)
    missing = [href for href in _nav_hrefs() if f"| `{href}` |" not in readme]
    assert not missing, (
        "README's feature tour omits shipped nav page(s): "
        f"{', '.join(sorted(missing))}. A tour that stops at the surfaces it covered "
        "three waves ago reads as if those later pages do not exist."
    )
