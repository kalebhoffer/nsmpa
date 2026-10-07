from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class InstitutionFilters(BaseModel):
    include_four_year: bool = True
    include_two_year: bool = False
    include_less_than_two_year: bool = False
    include_public: bool = True
    include_private_nonprofit: bool = True
    include_private_for_profit: bool = False


class CaseProfile(BaseModel):
    name: str = "post-publication relief case"
    student_context_weight: float = 8.0
    criminal_allegation_weight: float = 10.0
    dismissed_charges_weight: float = 15.0
    vacated_conviction_weight: float = 15.0
    sealed_or_expunged_weight: float = 12.0
    long_time_passed_weight: float = 8.0
    search_prominence_weight: float = 10.0
    deindex_relief_weight: float = 20.0
    anonymization_weight: float = 12.0
    update_context_weight: float = 6.0


class Settings(BaseModel):
    database_path: Path = Path("output/nsmpa.sqlite3")
    output_dir: Path = Path("output")
    user_agent: str = "NSMPA/0.2 (+https://example.org/nsmpa; contact=research@example.org)"
    respect_robots_txt: bool = True
    request_timeout_seconds: float = Field(20, ge=1, le=120)
    connect_timeout_seconds: float = Field(10, ge=1, le=60)
    max_retries: int = Field(3, ge=0, le=10)
    retry_backoff_seconds: float = Field(1.0, ge=0.0, le=30)
    max_redirects: int = Field(8, ge=1, le=20)
    allowed_ports: list[int] = Field(default_factory=lambda: [80, 443, 8080, 8443])
    host_failure_threshold: int = Field(6, ge=1, le=100)
    max_response_bytes: int = Field(10_000_000, ge=100_000, le=100_000_000)
    per_host_delay_seconds: float = Field(1.5, ge=0.0, le=30)
    max_concurrency: int = Field(16, ge=1, le=128)
    max_per_host_concurrency: int = Field(2, ge=1, le=16)
    crawl_max_pages_per_publication: int = Field(250, ge=10, le=10_000)
    crawl_max_depth: int = Field(4, ge=1, le=12)
    sitemap_max_urls: int = Field(3000, ge=50, le=100_000)
    policy_candidate_limit: int = Field(80, ge=10, le=1000)
    search_provider: Literal["auto", "brave", "serper", "none"] = "auto"
    search_results_per_query: int = Field(8, ge=1, le=20)
    publication_confidence_threshold: float = Field(0.60, ge=0, le=1)
    publication_early_stop_threshold: float = Field(0.84, ge=0, le=1)
    search_cache_days: int = Field(3650, ge=0, le=36500)
    search_credit_budget_per_run: int = Field(50_000, ge=1, le=5_000_000)
    discovery_max_searches_per_institution: int = Field(4, ge=1, le=12)
    research_max_searches_per_entity: int = Field(18, ge=1, le=100)
    research_max_targets_per_entity: int = Field(30, ge=1, le=250)
    research_fetch_top_targets: int = Field(18, ge=1, le=100)
    research_candidate_threshold: float = Field(0.28, ge=0, le=1)
    save_html_snapshots: bool = True
    snapshot_dir: Path = Path("output/snapshots")
    research_snapshot_dir: Path = Path("output/research_snapshots")
    allow_pdf: bool = True
    allowed_states: list[str] = Field(default_factory=list)
    institution_filters: InstitutionFilters = Field(default_factory=InstitutionFilters)
    case_profile: CaseProfile = Field(default_factory=CaseProfile)

    @model_validator(mode="after")
    def normalize_paths(self) -> "Settings":
        self.database_path = Path(self.database_path)
        self.output_dir = Path(self.output_dir)
        self.snapshot_dir = Path(self.snapshot_dir)
        self.research_snapshot_dir = Path(self.research_snapshot_dir)
        return self

    def ensure_dirs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if self.save_html_snapshots:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            self.research_snapshot_dir.mkdir(parents=True, exist_ok=True)



def load_settings(path: str | Path | None = None) -> Settings:
    data: dict = {}
    if path:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    if os.getenv("NSMPA_USER_AGENT"):
        data["user_agent"] = os.environ["NSMPA_USER_AGENT"]
    settings = Settings.model_validate(data)
    settings.ensure_dirs()
    return settings
