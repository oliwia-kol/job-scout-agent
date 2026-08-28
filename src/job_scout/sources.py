"""Validated source configuration for official company career pages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, TypeAdapter


class SourceConfig(BaseModel):
    id: str
    company: str
    adapter: str
    career_url: HttpUrl
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


def load_sources(path: Path) -> list[SourceConfig]:
    data = json.loads(path.read_text(encoding="utf-8"))
    sources = TypeAdapter(list[SourceConfig]).validate_python(data)
    ids = [source.id for source in sources]
    companies = [source.company.casefold() for source in sources]
    if len(ids) != len(set(ids)):
        raise ValueError("source ids must be unique")
    if len(companies) != len(set(companies)):
        raise ValueError("company names must be unique")
    return sources
