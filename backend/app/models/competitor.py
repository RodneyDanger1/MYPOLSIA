"""competitors (module name INFERRED - unreferenced)."""
from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String, Text, func

from app.core.database import Base
from app.models.types import json_type, string_list_type


class Competitor(Base):
    __tablename__ = "competitors"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    website = Column(String(512))
    pricing_info = Column(json_type())
    positioning = Column(Text)
    strengths = Column(string_list_type())
    weaknesses = Column(string_list_type())
    last_researched = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
