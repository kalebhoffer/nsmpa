from __future__ import annotations

from .db import Database
from .utils import normalize_url, registrableish_domain

# Deliberately labeled a benchmark panel, not an exhaustive census.
# Mixes national, metro, nonprofit, digital-native, broadcast, wire, and regional outlets.
BENCHMARK_NEWSROOMS: list[tuple[str, str, str]] = [
    ("Associated Press", "https://apnews.com/", "wire"),
    ("Reuters", "https://www.reuters.com/", "wire"),
    ("The New York Times", "https://www.nytimes.com/", "national"),
    ("The Washington Post", "https://www.washingtonpost.com/", "national"),
    ("The Wall Street Journal", "https://www.wsj.com/", "national"),
    ("USA TODAY", "https://www.usatoday.com/", "national"),
    ("Los Angeles Times", "https://www.latimes.com/", "metro"),
    ("Chicago Tribune", "https://www.chicagotribune.com/", "metro"),
    ("The Boston Globe", "https://www.bostonglobe.com/", "metro"),
    ("The Philadelphia Inquirer", "https://www.inquirer.com/", "metro"),
    ("The Seattle Times", "https://www.seattletimes.com/", "metro"),
    ("The Denver Post", "https://www.denverpost.com/", "metro"),
    ("The Dallas Morning News", "https://www.dallasnews.com/", "metro"),
    ("Houston Chronicle", "https://www.houstonchronicle.com/", "metro"),
    ("Miami Herald", "https://www.miamiherald.com/", "metro"),
    ("Atlanta Journal-Constitution", "https://www.ajc.com/", "metro"),
    ("Star Tribune", "https://www.startribune.com/", "metro"),
    ("San Francisco Chronicle", "https://www.sfchronicle.com/", "metro"),
    ("The Arizona Republic", "https://www.azcentral.com/", "metro"),
    ("Tampa Bay Times", "https://www.tampabay.com/", "metro"),
    ("The Baltimore Sun", "https://www.baltimoresun.com/", "metro"),
    ("The Plain Dealer / cleveland.com", "https://www.cleveland.com/", "metro"),
    ("The Oregonian / OregonLive", "https://www.oregonlive.com/", "metro"),
    ("The Salt Lake Tribune", "https://www.sltrib.com/", "regional_nonprofit"),
    ("ProPublica", "https://www.propublica.org/", "nonprofit_investigative"),
    ("The Marshall Project", "https://www.themarshallproject.org/", "nonprofit_investigative"),
    ("The Texas Tribune", "https://www.texastribune.org/", "nonprofit_state"),
    ("CalMatters", "https://calmatters.org/", "nonprofit_state"),
    ("VTDigger", "https://vtdigger.org/", "nonprofit_state"),
    ("MinnPost", "https://www.minnpost.com/", "nonprofit_local"),
    ("NPR", "https://www.npr.org/", "public_media"),
    ("PBS NewsHour", "https://www.pbs.org/newshour/", "public_media"),
    ("CNN", "https://www.cnn.com/", "broadcast_digital"),
    ("NBC News", "https://www.nbcnews.com/", "broadcast_digital"),
    ("ABC News", "https://abcnews.go.com/", "broadcast_digital"),
    ("CBS News", "https://www.cbsnews.com/", "broadcast_digital"),
    ("FOX News", "https://www.foxnews.com/", "broadcast_digital"),
    ("Politico", "https://www.politico.com/", "digital_native"),
    ("Axios", "https://www.axios.com/", "digital_native"),
    ("Vox", "https://www.vox.com/", "digital_native"),
    ("Slate", "https://slate.com/", "digital_native"),
    ("The Daily Beast", "https://www.thedailybeast.com/", "digital_native"),
    ("Bloomberg", "https://www.bloomberg.com/", "business"),
    ("Forbes", "https://www.forbes.com/", "business"),
    ("Fortune", "https://fortune.com/", "business"),
]


# Other domains these organizations publish their own standards on (counted as their own site).
ALT_DOMAINS: dict[str, list[str]] = {
    "Associated Press": ["ap.org"],
    "Reuters": ["thomsonreuters.com", "reutersagency.com"],
}


def seed_benchmark_newsrooms(db: Database) -> dict[str, int]:
    inserted = updated = 0
    with db.transaction():
        for name, url, category in BENCHMARK_NEWSROOMS:
            clean = normalize_url(url)
            if not clean:
                continue
            domain = registrableish_domain(clean)
            key = f"benchmark:{domain}:{name.lower()}"
            existing = db.conn.execute(
                "SELECT id FROM research_entities WHERE cohort='professional_newsroom' AND source_key=?", (key,)
            ).fetchone()
            db.conn.execute(
                """
                INSERT INTO research_entities(cohort,source_key,name,homepage_url,domain,source,verification_status,metadata_json)
                VALUES('professional_newsroom',?,?,?,?, 'builtin_benchmark','seeded',?)
                ON CONFLICT(cohort,source_key) DO UPDATE SET name=excluded.name,homepage_url=excluded.homepage_url,
                  domain=excluded.domain,metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP
                """,
                (key, name, clean, domain, db.json({"category": category, "benchmark": True,
                                                    **({"alt_domains": ALT_DOMAINS[name]} if name in ALT_DOMAINS else {})})),
            )
            if existing:
                updated += 1
            else:
                inserted += 1
    return {"inserted": inserted, "updated": updated, "total_seed": len(BENCHMARK_NEWSROOMS)}
