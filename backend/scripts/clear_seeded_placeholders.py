"""Clear the SEEDED placeholder values from the existing company row (V22 honesty sweep, W2/D1+D2).

The seed script is fixed (fresh databases get `kpis={}` and `website_url=""`), but a database seeded
BEFORE V22 still carries the fabricated values:

  * `kpis = {mrr_usd: 0, active_customers: 0, churn_rate: 0.0, cac_usd: 0, ltv_usd: 0, nps: 0}`
    — zeros nobody measured, rendered as measurements on the dashboard;
  * `website_url = "https://yourcompany.com"` — a placeholder that SATISFIES the email-compliance
    footer gate (a live send would print privacy/unsubscribe links on a domain the owner does not
    own).

This script clears ONLY those exact placeholder values. It never touches a real KPI or a real URL.

Usage:
    python scripts/clear_seeded_placeholders.py            # dry run: prints what it would change
    python scripts/clear_seeded_placeholders.py --apply    # performs the update

Exit codes: 0 = done/nothing to do, 1 = the database refused (named error printed), 2 = usage.
"""
from __future__ import annotations

import asyncio
import os
import sys

# The house bootstrap (same as scripts/seed_company.py): running `python scripts/<x>.py` puts
# `scripts/` on sys.path, not the backend root, so `import app` needs the parent prepended.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.company import CompanyConfig

#: The exact dict the old seed wrote. Equality (not "contains zero") is the guard: a real `0` the
#: owner typed is their measurement and is NEVER cleared.
SEEDED_KPIS = {
    "mrr_usd": 0,
    "active_customers": 0,
    "churn_rate": 0.0,
    "cac_usd": 0,
    "ltv_usd": 0,
    "nps": 0,
}

#: The exact placeholder the old seed wrote.
SEEDED_WEBSITE = "https://yourcompany.com"


async def _run(apply: bool) -> int:
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    changed = 0
    try:
        async with session_factory() as session:
            rows = (await session.execute(select(CompanyConfig))).scalars().all()
            if not rows:
                print("No company row — nothing to clear.")
                return 0
            for company in rows:
                kpis = dict(company.kpis or {})
                website = (company.website_url or "").strip()
                clears_kpis = kpis == SEEDED_KPIS
                clears_website = website == SEEDED_WEBSITE
                if not (clears_kpis or clears_website):
                    print(f"company #{company.id} ({company.name}): no seeded placeholders — untouched.")
                    continue
                print(f"company #{company.id} ({company.name}):")
                if clears_kpis:
                    print(f"  kpis: {kpis} -> {{}} (fabricated zeros; the dashboard will show the honest dash)")
                if clears_website:
                    print(f'  website_url: "{website}" -> "" (placeholder; compliance will refuse BY NAME '
                          f"until your real domain is set in Settings -> Company)")
                if apply:
                    if clears_kpis:
                        company.kpis = {}
                    if clears_website:
                        company.website_url = ""
                    changed += 1
            if apply and changed:
                await session.commit()
                print(f"Applied to {changed} row(s).")
            elif apply:
                print("Nothing to apply.")
            else:
                print("Dry run — re-run with --apply to perform the update.")
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    unknown = [a for a in argv if a not in ("--apply",)]
    if unknown:
        print(f"usage: python scripts/clear_seeded_placeholders.py [--apply]  (unknown: {unknown})")
        return 2
    try:
        return asyncio.run(_run(apply))
    except Exception as exc:  # a named failure must still exit non-zero and say why
        print(f"refused: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
