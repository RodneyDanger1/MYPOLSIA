"""All ORM models.  Importing this package registers every table on Base.metadata
(required by alembic/env.py and tests/integration/conftest.py)."""
# Convenience re-export: `from app.models import Base` also works.  The canonical
# import used by every shipped artifact is `from app.core.database import Base`.
from app.core.database import (
    Base as Base,  # noqa: E402  (load-bearing re-export: importing app.models is what registers every table on Base.metadata, which alembic/env.py and tests/integration/conftest.py depend on — the `as Base` spelling marks it as an intentional re-export rather than a dead import)
)
from app.models.ads import AdCampaign, AdMetric
from app.models.auto_mode import AutoModeSession
from app.models.chat import ChatMessage
from app.models.company import CompanyConfig
from app.models.competitor import Competitor
from app.models.finance import ExpenseRecord, RevenueSnapshot, StripeEvent
from app.models.invoice import Invoice
from app.models.memory import MemoryEntry
from app.models.outreach import EmailCampaign, EmailLog, Prospect
from app.models.report import ActivityLog, DailyReport
from app.models.schedule import TaskSchedule
from app.models.social import SocialEngagement, SocialPost
from app.models.support import SupportInboxItem
from app.models.task import AgentRun, Task
from app.models.types import json_type, string_list_type

__all__ = [
    "AdCampaign",
    "AdMetric",
    "AgentRun",
    "ActivityLog",
    "AutoModeSession",
    "ChatMessage",
    "CompanyConfig",
    "Competitor",
    "DailyReport",
    "EmailCampaign",
    "EmailLog",
    "ExpenseRecord",
    "Invoice",
    "MemoryEntry",
    "Prospect",
    "RevenueSnapshot",
    "SocialEngagement",
    "SocialPost",
    "StripeEvent",
    "SupportInboxItem",
    "Task",
    "TaskSchedule",
    "json_type",
    "string_list_type",
]
