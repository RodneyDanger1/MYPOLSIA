"""All ORM models.  Importing this package registers every table on Base.metadata
(required by alembic/env.py and tests/integration/conftest.py)."""
from app.models.ads import AdCampaign, AdMetric
from app.models.company import CompanyConfig
from app.models.competitor import Competitor
from app.models.finance import ExpenseRecord, RevenueSnapshot, StripeEvent
from app.models.memory import MemoryEntry
from app.models.outreach import EmailCampaign, EmailLog, Prospect
from app.models.report import ActivityLog, DailyReport
from app.models.social import SocialEngagement, SocialPost
from app.models.task import AgentRun, Task
from app.models.types import json_type, string_list_type

# Convenience re-export: `from app.models import Base` also works.  The canonical
# import used by every shipped artifact is `from app.core.database import Base`.
from app.core.database import Base  # noqa: E402  (re-export)

__all__ = [
    "AdCampaign",
    "AdMetric",
    "AgentRun",
    "ActivityLog",
    "CompanyConfig",
    "Competitor",
    "DailyReport",
    "EmailCampaign",
    "EmailLog",
    "ExpenseRecord",
    "MemoryEntry",
    "Prospect",
    "RevenueSnapshot",
    "SocialEngagement",
    "SocialPost",
    "StripeEvent",
    "Task",
    "json_type",
    "string_list_type",
]
