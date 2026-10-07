from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class PolicyClass(StrEnum):
    A_EXPLICIT_RELIEF = "A_EXPLICIT_RELIEF"
    B_CHANGED_CIRCUMSTANCES = "B_CHANGED_CIRCUMSTANCES"
    C_CASE_BY_CASE = "C_CASE_BY_CASE"
    D_UPDATE_ONLY = "D_UPDATE_ONLY"
    E_STRICT_ARCHIVE = "E_STRICT_ARCHIVE"
    F_NO_PUBLIC_POLICY_FOUND = "F_NO_PUBLIC_POLICY_FOUND"
    U_UNDETERMINED = "U_UNDETERMINED"


class CrawlStatus(StrEnum):
    QUEUED = "queued"
    FETCHED = "fetched"
    SKIPPED_ROBOTS = "skipped_robots"
    BLOCKED = "blocked"
    TOO_LARGE = "too_large"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass(slots=True)
class Institution:
    unitid: str
    name: str
    city: str | None = None
    state: str | None = None
    website: str | None = None
    control: int | None = None
    level: int | None = None
    source_year: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchResult:
    url: str
    title: str = ""
    snippet: str = ""
    rank: int = 0
    provider: str = ""
    query: str = ""


@dataclass(slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    content: bytes
    elapsed_ms: int
    content_type: str
    robots_allowed: bool = True
    error: str | None = None
    redirect_chain: list[str] = field(default_factory=list)
    access_class: str = "ok"


@dataclass(slots=True)
class PageAnalysis:
    url: str
    title: str
    text: str
    canonical_url: str | None
    meta_robots: str | None
    x_robots_tag: str | None
    noindex: bool
    nofollow: bool
    links: list[str]
    policy_score: float
    evidence_tags: list[str]
    excerpts: list[tuple[str, str]]
