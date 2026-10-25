"""YC candidates from the scraped public directory (read-only; never written to).

Series A/B proxy: active, not a nonprofit, 15-300 people, batch from 2015 through 2024,
with a location in a city where Pavoot runs events (score.HUB_CITIES).
The weekly YC sync (pavoot-ops) refreshes the scrape; until then the files are a snapshot.
"""

from __future__ import annotations

import glob
import json
import os
import re
from urllib.parse import urlparse

from .score import HUB_CITIES

YC_DIR = os.environ.get("YC_DATA_DIR",
                        os.path.expanduser("~/Documents/CODEBASE/scrape yc public directory/data"))
MIN_TEAM, MAX_TEAM = 15, 300
MIN_YEAR, MAX_YEAR = 2015, 2024
SWEET_SPOT = 80


def domain_of(website: str | None) -> str | None:
    if not website:
        return None
    host = urlparse(website if "//" in website else "//" + website).hostname or ""
    return re.sub(r"^www\.", "", host.lower()) or None


def _year(batch: str | None) -> int | None:
    m = re.search(r"(\d{4})$", batch or "")
    return int(m.group(1)) if m else None


def load(yc_dir: str = YC_DIR) -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(yc_dir, "yc_*.json"))):
        with open(path) as f:
            out.extend(json.load(f))
    return out


def is_candidate(c: dict) -> bool:
    year = _year(c.get("batch"))
    size = c.get("team_size") or 0
    return (c.get("status") == "Active" and not c.get("nonprofit")
            and MIN_TEAM <= size <= MAX_TEAM and year is not None and MIN_YEAR <= year <= MAX_YEAR
            and domain_of(c.get("website")) is not None
            and any(h in (c.get("all_locations") or "").lower() for h in HUB_CITIES))


def to_prospect(c: dict) -> dict:
    """Shape a YC record like a prospecting-run company (see prompts/prospect.md)."""
    signals = []
    if c.get("isHiring"):
        signals.append({"type": "hiring", "detail": "hiring on YC Work at a Startup",
                        "url": f"https://www.ycombinator.com/companies/{c.get('slug')}"})
    return {
        "name": c["name"],
        "domain": domain_of(c.get("website")),
        "source": "yc",
        "hq_city": (c.get("all_locations") or "").split(";")[0].strip() or None,
        "employees": c.get("team_size"),
        "funding_stage": None,
        "signals": signals,
        "notes": f"YC {c.get('batch')}. {c.get('one_liner') or ''}".strip(),
        "people": [{"full_name": f["name"], "title": f.get("title") or "Founder",
                    "linkedin_url": f.get("linkedin_url") or None, "source": "yc_directory"}
                   for f in c.get("founders") or [] if f.get("name") and f.get("is_active", True)],
    }


def candidates(exclude_domains: set[str], limit: int, yc_dir: str = YC_DIR) -> list[dict]:
    """Hiring companies first, then team size closest to ~80 (typical Series A/B), skipping
    companies already in the CRM."""
    pool = [c for c in load(yc_dir) if is_candidate(c) and domain_of(c.get("website")) not in exclude_domains]
    pool.sort(key=lambda c: (not c.get("isHiring"), abs((c.get("team_size") or 0) - SWEET_SPOT), c["name"]))
    return [to_prospect(c) for c in pool[:limit]]
