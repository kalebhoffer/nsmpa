from __future__ import annotations

from .db import Database
from .utils import normalize_url, registrableish_domain

# National journalism standards, ethics, legal-support, training, research, membership,
# student-media, local-news and specialty organizations. This seed is intentionally broad
# but is not represented as an exhaustive denominator of every U.S. journalism nonprofit.
SUPPORT_ORGS: list[tuple[str, str, str]] = [
    ("Society of Professional Journalists", "https://www.spj.org/", "ethics_professional"),
    ("Online News Association", "https://journalists.org/", "ethics_professional"),
    ("Poynter Institute", "https://www.poynter.org/", "training_ethics"),
    ("American Press Institute", "https://americanpressinstitute.org/", "research_training"),
    ("Reynolds Journalism Institute", "https://rjionline.org/", "research_training"),
    ("Reporters Committee for Freedom of the Press", "https://www.rcfp.org/", "legal_support"),
    ("Freedom of the Press Foundation", "https://freedom.press/", "press_freedom"),
    ("Committee to Protect Journalists", "https://cpj.org/", "press_freedom"),
    ("Investigative Reporters and Editors", "https://www.ire.org/", "training_professional"),
    ("Radio Television Digital News Association", "https://www.rtdna.org/", "ethics_professional"),
    ("National Press Photographers Association", "https://nppa.org/", "ethics_professional"),
    ("News/Media Alliance", "https://www.newsmediaalliance.org/", "industry_association"),
    ("National Newspaper Association", "https://www.nna.org/", "industry_association"),
    ("Local Media Association", "https://localmedia.org/", "industry_association"),
    ("LION Publishers", "https://lionpublishers.com/", "publisher_network"),
    ("Institute for Nonprofit News", "https://inn.org/", "publisher_network"),
    ("Associated Collegiate Press", "https://studentpress.org/acp/", "student_media_support"),
    ("College Media Association", "https://www.collegemedia.org/", "student_media_support"),
    ("Student Press Law Center", "https://splc.org/", "student_media_legal"),
    ("Journalism Education Association", "https://jea.org/", "journalism_education"),
    ("Association for Education in Journalism and Mass Communication", "https://www.aejmc.org/", "journalism_education"),
    ("Center for Media Engagement", "https://mediaengagement.org/", "research"),
    ("Tow Center for Digital Journalism", "https://towcenter.columbia.edu/", "research"),
    ("Nieman Lab", "https://www.niemanlab.org/", "research_analysis"),
    ("Shorenstein Center on Media, Politics and Public Policy", "https://shorensteincenter.org/", "research"),
    ("Lenfest Institute for Journalism", "https://www.lenfestinstitute.org/", "research_support"),
    ("Trusting News", "https://trustingnews.org/", "newsroom_guidance"),
    ("Solutions Journalism Network", "https://www.solutionsjournalism.org/", "newsroom_guidance"),
    ("National Association of Black Journalists", "https://nabjonline.org/", "journalist_association"),
    ("National Association of Hispanic Journalists", "https://nahj.org/", "journalist_association"),
    ("Asian American Journalists Association", "https://www.aaja.org/", "journalist_association"),
    ("Indigenous Journalists Association", "https://indigenousjournalists.org/", "journalist_association"),
    ("NLGJA: The Association of LGBTQ+ Journalists", "https://www.nlgja.org/", "journalist_association"),
    ("Society of Environmental Journalists", "https://www.sej.org/", "specialty_journalism"),
    ("Association of Health Care Journalists", "https://healthjournalism.org/", "specialty_journalism"),
    ("Education Writers Association", "https://ewa.org/", "specialty_journalism"),
    ("Religion News Association", "https://religionnews.com/rna/", "specialty_journalism"),
    ("SABEW", "https://sabew.org/", "specialty_journalism"),
    ("National Press Club Journalism Institute", "https://www.pressclubinstitute.org/", "training_support"),
    ("Dart Center for Journalism and Trauma", "https://dartcenter.org/", "newsroom_guidance"),
    ("Journalism and Women Symposium", "https://jaws.org/", "journalist_association"),
    ("OpenNews", "https://opennews.org/", "newsroom_technology"),
    ("News Product Alliance", "https://newsproduct.org/", "newsroom_professional"),
    ("The Marshall Project: A Guide to Criminal Justice Journalism", "https://www.themarshallproject.org/", "reference_newsroom"),
    ("First Amendment Coalition", "https://firstamendmentcoalition.org/", "legal_support"),
    ("Foundation for Individual Rights and Expression", "https://www.thefire.org/", "adjacent_legal_support"),
]


def seed_support_orgs(db: Database) -> dict[str, int]:
    inserted = updated = 0
    with db.transaction():
        for name, url, category in SUPPORT_ORGS:
            clean = normalize_url(url)
            if not clean:
                continue
            key = f"support:{registrableish_domain(clean)}:{name.lower()}"
            existing = db.conn.execute(
                "SELECT id FROM research_entities WHERE cohort='support_org' AND source_key=?", (key,)
            ).fetchone()
            db.conn.execute(
                """
                INSERT INTO research_entities(cohort,source_key,name,homepage_url,domain,source,verification_status,metadata_json)
                VALUES('support_org',?,?,?,?, 'builtin_seed','seeded',?)
                ON CONFLICT(cohort,source_key) DO UPDATE SET
                  name=excluded.name,homepage_url=excluded.homepage_url,domain=excluded.domain,
                  metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP
                """,
                (key, name, clean, registrableish_domain(clean), db.json({"category": category})),
            )
            if existing:
                updated += 1
            else:
                inserted += 1
    return {"inserted": inserted, "updated": updated, "total_seed": len(SUPPORT_ORGS)}
