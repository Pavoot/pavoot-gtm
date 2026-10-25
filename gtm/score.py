"""Company score: higher = contact sooner. Signals come from the prospecting run."""

from __future__ import annotations

from datetime import date, datetime

# A new marketing leader is the strongest trigger for Pavoot (they want quick, visible wins).
WEIGHTS = {
    "engaged_with_content": 35,  # liked / commented on a founder post, viewed a profile, pledged on pavoot.events
    "new_marketing_leader": 40,
    "funding": 30,              # recent Series A/B
    "hiring_events": 25,        # open field marketing / events roles
    "hosts_events": 20,         # already hosts events (Luma, meetups, dinners)
    "sponsors_events": 15,      # sponsors conferences / other people's events
    "product_launch": 10,
    "hiring": 5,                # hiring at all (growing)
}
FRESH_DAYS = 180                # older signals count half
HUB_CITIES = ("san francisco", "new york", "nyc", "bay area", "palo alto")  # where Pavoot runs events


def _age_days(seen_at: str | None, today: date) -> int | None:
    if not seen_at:
        return None
    try:
        return (today - datetime.fromisoformat(seen_at[:10]).date()).days
    except ValueError:
        return None


def current_weights(conn) -> dict:
    """Defaults overridden by the latest weight Ana approved for each kind (gtm.score_weights)."""
    w = dict(WEIGHTS)
    with conn.cursor() as cur:
        cur.execute("""select distinct on (kind) kind, weight from gtm.score_weights
                       where status = 'approved' order by kind, valid_from desc nulls last, id desc""")
        for r in cur.fetchall():
            w[r["kind"]] = r["weight"]
    return w


def score(company: dict, today: date | None = None, weights: dict | None = None) -> int:
    today = today or date.today()
    weights = weights or WEIGHTS
    total = 0
    counted = set()
    for s in company.get("signals") or []:
        kind = s.get("type")
        if kind not in weights or kind in counted:   # each kind counts once
            continue
        counted.add(kind)
        age = _age_days(s.get("seen_at"), today)
        total += weights[kind] if age is None or age <= FRESH_DAYS else weights[kind] // 2
    size = company.get("employees")
    if size is not None and 20 <= size <= 250:
        total += 10
    if any(c in (company.get("hq_city") or "").lower() for c in HUB_CITIES):
        total += 5
    return total


def rescore_all(conn, today: date | None = None) -> int:
    """Recompute every company's score with the current (approved) weights."""
    w = current_weights(conn)
    with conn.cursor() as cur:
        cur.execute("select id, signals, employees, hq_city, score from gtm.companies")
        rows = cur.fetchall()
    changed = 0
    for r in rows:
        new = score(r, today, w)
        if new != r["score"]:
            with conn.cursor() as cur:
                cur.execute("update gtm.companies set score = %s where id = %s", (new, r["id"]))
            changed += 1
    return changed
