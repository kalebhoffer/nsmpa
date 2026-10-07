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
    """Auditable fact-pattern similarity weights (see docs/classification_rubric.md#similarity)."""
    name: str = "post-publication relief case"
    student_context_weight: float = 8.0
    criminal_allegation_weight: float = 10.0
    dismissed_charges_weight: float = 15.0
    acquittal_exoneration_weight: float = 12.0
    vacated_conviction_weight: float = 15.0
    sealed_or_expunged_weight: float = 12.0
    long_time_passed_weight: float = 8.0
    private_individual_weight: float = 6.0
    search_prominence_weight: float = 10.0
    reputational_harm_weight: float = 8.0
    name_search_suppression_weight: float = 10.0
    deindex_relief_weight: float = 20.0
    anonymization_weight: float = 12.0
    update_context_weight: float = 6.0
    high_similarity_threshold: float = 45.0


class PeerGroup(BaseModel):
    """A named comparison group. Filters are ANDed; ``unitids`` are always included."""
    label: str
    description: str = ""
    states: list[str] = Field(default_factory=list)
    counties: list[str] = Field(default_factory=list)
    control: list[int] = Field(default_factory=list)
    carnegie_basic: list[int] = Field(default_factory=list)
    size_categories: list[int] = Field(default_factory=list)
    obe_regions: list[int] = Field(default_factory=list)
    unitids: list[str] = Field(default_factory=list)


EASTERN_WA_COUNTIES = [
    "Adams County", "Asotin County", "Benton County", "Chelan County", "Columbia County", "Douglas County",
    "Ferry County", "Franklin County", "Garfield County", "Grant County", "Kittitas County", "Klickitat County",
    "Lincoln County", "Okanogan County", "Pend Oreille County", "Spokane County", "Stevens County",
    "Walla Walla County", "Whitman County", "Yakima County",
]


def default_peer_groups() -> dict[str, PeerGroup]:
    return {
        "washington": PeerGroup(label="Washington institutions", states=["WA"]),
        "eastern_washington": PeerGroup(
            label="Eastern Washington institutions",
            description="WA institutions in the 20 counties east of the Cascade crest",
            states=["WA"], counties=EASTERN_WA_COUNTIES),
        "ewu_comparables_proxy": PeerGroup(
            label="Eastern Washington University comparables (proxy)",
            description=("Proxy definition, edit to match an official peer list: public Master's Colleges & "
                         "Universities: Larger Programs (Carnegie 2021 basic=18), 5,000-19,999 students "
                         "(IPEDS INSTSIZE 3-4), Rocky Mountain/Far West regions (OBEREG 7-8), plus EWU itself."),
            control=[1], carnegie_basic=[18], size_categories=[3, 4], obe_regions=[7, 8], unitids=["235097"]),
    }


class ValidationThresholds(BaseModel):
    min_discovery_coverage: float = Field(0.80, ge=0, le=1)
    min_crawl_success: float = Field(0.75, ge=0, le=1)
    max_undetermined_rate: float = Field(0.25, ge=0, le=1)
    min_source_verification: float = Field(0.10, ge=0, le=1)
    min_human_review_of_findings: float = Field(0.20, ge=0, le=1)
    min_mean_stance_confidence: float = Field(0.65, ge=0, le=1)
    max_duplicate_evidence_rate: float = Field(0.35, ge=0, le=1)
    min_entities_for_percentages: int = Field(30, ge=1)


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

    # --- v0.3 search ---------------------------------------------------------------------
    search_max_retries: int = Field(2, ge=0, le=6)
    search_retry_backoff_seconds: float = Field(2.0, ge=0.0, le=60)
    search_gl: str = "us"
    search_hl: str = "en"
    # --- v0.3 discovery ------------------------------------------------------------------
    discovery_concurrency: int = Field(4, ge=1, le=32)
    discovery_verify_top_candidates: int = Field(3, ge=0, le=10)
    discovery_site_pages: int = Field(14, ge=0, le=60)
    publication_ambiguity_margin: float = Field(0.08, ge=0, le=1)
    # --- v0.3 research -------------------------------------------------------------------
    research_depth: Literal["quick", "standard", "deep"] = "standard"
    research_concurrency: int = Field(3, ge=1, le=32)
    research_first_party_link_pages: int = Field(6, ge=0, le=40)
    research_max_evidence_per_page: int = Field(60, ge=5, le=500)
    research_third_party_fetch_limit: int = Field(4, ge=0, le=50)
    no_guidance_min_first_party_pages: int = Field(2, ge=1, le=50)
    no_guidance_max_inaccessible_ratio: float = Field(0.5, ge=0, le=1)
    # --- v0.3 validation gates (reports refuse national percentages below these) ----------
    validation: "ValidationThresholds" = Field(default_factory=lambda: ValidationThresholds())
    peer_groups: dict[str, PeerGroup] = Field(default_factory=default_peer_groups)

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
