"""Dependency audit: what the build needs vs what this venv can import.

Run with the repo venv:
  C:\\Users\\Riley\\Desktop\\Polsia\\.venv\\Scripts\\python.exe check_deps.py
"""
import importlib
import sys

# (module, why the build needs it, required-for-unit-tests?)
MODULES = [
    ("fastapi", "HTTP framework", True),
    ("starlette", "ASGI toolkit under FastAPI", True),
    ("uvicorn", "ASGI server (compose command)", False),
    ("sqlalchemy", "ORM + async", True),
    ("sqlalchemy.dialects.postgresql", "JSONB/ARRAY variants", True),
    ("aiosqlite", "test engine", True),
    ("alembic", "migrations", True),
    ("pydantic", "schemas", True),
    ("pydantic_settings", "config", True),
    ("httpx", "test client + integrations", True),
    ("pytest", "suite", True),
    ("pytest_asyncio", "suite", True),
    ("pytest_mock", "suite (mocker fixture)", True),
    ("pytest_cov", "coverage gate", False),
    ("dotenv", "env loading", True),
    ("structlog", "logging", False),
    ("tenacity", "retry helper", False),
    ("redis", "pub/sub + celery broker", True),
    ("celery", "task queue", True),
    ("asyncpg", "PostgreSQL async driver (prod)", False),
    ("chromadb", "vector memory (lazy-imported)", False),
    ("stripe", "webhook signature + billing (lazy)", False),
    ("tweepy", "X/Twitter (lazy)", False),
    ("sendgrid", "email (lazy)", False),
    ("github", "PyGithub: code agent (lazy)", False),
    ("tavily", "Tavily search (lazy)", False),
    ("google.ads.googleads", "Google Ads (lazy)", False),
    ("facebook_business", "Meta Ads (lazy)", False),
    ("websockets", "uvicorn[standard] WS support", False),
]

ok, missing, missing_required = [], [], []
for name, why, required in MODULES:
    try:
        mod = importlib.import_module(name)
        ver = getattr(mod, "__version__", "?")
        ok.append((name, ver, why))
    except Exception as exc:  # noqa: BLE001
        missing.append((name, type(exc).__name__, why, required))
        if required:
            missing_required.append(name)

print(f"python {sys.version.split()[0]}")
print(f"\nIMPORTABLE ({len(ok)}):")
for name, ver, why in ok:
    print(f"  ok   {name:<32} {ver:<12} {why}")
print(f"\nMISSING ({len(missing)}):")
for name, err, why, required in missing:
    tag = "REQUIRED-FOR-TESTS" if required else "lazy/optional"
    print(f"  MISS {name:<32} {err:<18} [{tag}] {why}")
print(f"\nmissing-but-required-for-the-unit-suite: {missing_required or 'none'}")
