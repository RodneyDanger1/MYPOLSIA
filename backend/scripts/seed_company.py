"""Seed a default CompanyConfig row if none exists."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.company import CompanyConfig


async def seed():
    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as session:
        result = await session.execute(select(CompanyConfig).limit(1))
        existing = result.scalar_one_or_none()

        if existing:
            print("Company config already exists — skipping seed.")
            return

        company = default_company()
        session.add(company)
        await session.commit()
        print(f"Seeded company: {company.name}")

    await engine.dispose()


def default_company() -> CompanyConfig:
    """The seed's one source of truth — importable so tests can pin its honesty rules.

    Two V22 honesty rules live here (W2/D1+D2) and are pinned by
    `tests/unit/test_v22_seed_truth.py`: zero-valued KPIs are NOT measurements (empty renders "—"),
    and a placeholder domain must never satisfy the email-compliance gate (empty refuses by name).
    """
    return CompanyConfig(
        name="My Company",
        mission="Build products that make people's lives better.",
        vision="Become the go-to solution in our market within 3 years.",
        description="A SaaS platform that automates key business operations.",
        target_market="Small and medium-sized businesses in North America.",
        value_prop="Save 10+ hours per week by automating your marketing, sales, and support.",
        pricing_model={"plans": [{"name": "Starter", "price": 49}, {"name": "Pro", "price": 149}]},
        goals={"q1_2026": ["Hit $10K MRR", "Get 100 paying customers", "Launch v2 of product"]},
        # V22 honesty fix (W2/D1): the seed used to write zero-valued KPIs, and the dashboard
        # rendered them as MEASUREMENTS ($0.00 MRR, 0 customers, 0.0% churn). A value nobody
        # measured must not exist: an empty object renders as "—" until the owner enters real
        # numbers. (There is no KPI editor yet — that is a named product ask, not a fake zero.)
        kpis={},
        # V22 honesty fix (W2/D2): a placeholder domain must NEVER satisfy the email-compliance
        # gate — with SANDBOX_MODE off it would print privacy/unsubscribe links on a domain the
        # owner does not own. Empty means compliance refuses BY NAME until the real URL is set.
        website_url="",
        github_repo="yourorg/yourrepo",
        product_type="SaaS",
        industry="Technology",
        timezone="UTC",
        daily_cycle_hour=6,
    )


if __name__ == "__main__":
    asyncio.run(seed())
