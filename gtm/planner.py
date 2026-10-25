"""Daily plan: who starts a sequence today, which variant and sender they get, and when each
cadence step is due. Writes sequences + planned touches; drafting fills the text later.

Cadence (business days, so "day 7" is a week later):
  cold, email + LinkedIn: day 1 email, day 2 LinkedIn, day 3 email, day 7 email
                          (channel_order experiment "linkedin_first" swaps email and LinkedIn)
  cold, email only:       day 1, 3, 7 (no daily emails to strangers: spam complaints hurt the domain)
  cold, LinkedIn only:    day 1, 3, 7
  warm (is_warm):         day 1, 2, 3, 7 by email (Ana's manual cadence)
Max 3 people per company, one new start per company every 2 business days, and nobody at a
company that has replied (its stage is no longer new / contacting).
"""

from __future__ import annotations

import os
import random
from datetime import date, timedelta

from psycopg.types.json import Jsonb

from .roles import pick_contacts

MAILBOXES = {"ana": "ana@pavoot.com", "gohar": "gohar@pavoot.com"}
OFFSETS = {1: 0, 2: 1, 3: 2, 7: 5}          # cadence day -> business days after the start
STAGGER_DAYS = 2
PER_COMPANY = 3


def senders() -> list[str]:
    """Mailboxes outreach may come from. GTM_SENDERS=ana while Gohar's mailbox isn't connected."""
    keys = [k.strip() for k in os.environ.get("GTM_SENDERS", "ana,gohar").split(",") if k.strip() in MAILBOXES]
    return [MAILBOXES[k] for k in keys] or [MAILBOXES["ana"]]


def business_day(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def add_business_days(d: date, n: int) -> date:
    d = business_day(d)
    step = 1 if n >= 0 else -1
    for _ in range(abs(n)):
        d += timedelta(days=step)
        while d.weekday() >= 5:
            d += timedelta(days=step)
    return d


BOUNCE_LIMIT, BOUNCE_WINDOW = 0.05, 40     # >5% of the last 40+ emails bounced -> pause new email sequences


def email_paused(cur) -> bool:
    """Circuit breaker for the sending domain: too many recent bounces means bad addresses, and more
    sends would hurt deliverability for everyone. New sequences go LinkedIn-only until it recovers."""
    cur.execute("""select count(*) n, count(*) filter (where status = 'bounced') b from (
                       select status from gtm.touches where channel = 'email' and status in ('sent', 'bounced')
                       order by sent_at desc nulls last limit %s) x""", (BOUNCE_WINDOW * 2,))
    r = cur.fetchone()
    return r["n"] >= BOUNCE_WINDOW and r["b"] / r["n"] > BOUNCE_LIMIT


def channel_plan(person: dict, kind: str, order: str = "email_first", email_ok: bool = True) -> list[tuple[int, str]]:
    email = (email_ok and bool(person.get("email")) and person.get("email_status") != "invalid"
             and not person.get("suppressed"))
    li = bool(person.get("linkedin_url"))
    if kind == "warm":
        return [(s, "email") for s in (1, 2, 3, 7)] if email else ([(s, "linkedin") for s in (1, 3, 7)] if li else [])
    if email and li:
        first, second = ("linkedin", "email") if order == "linkedin_first" else ("email", "linkedin")
        return [(1, first), (2, second), (3, first), (7, "email")]
    if email:
        return [(1, "email"), (3, "email"), (7, "email")]
    if li:
        return [(1, "linkedin"), (3, "linkedin"), (7, "linkedin")]
    return []


def sequence_kind(person: dict, company: dict) -> str:
    if person.get("is_warm"):
        return "warm"
    if person.get("role") == "Speaker":
        return "speaker"
    if person.get("role") == "Photographer":
        return "photographer"
    if company.get("kind") == "referral_partner":
        return "referral_partner"
    return "cold"


# ---------- experiments ----------

def running_experiment(cur) -> dict | None:
    cur.execute("select * from gtm.experiments where status = 'running' order by started_at nulls last, id limit 1")
    return cur.fetchone()


def baseline_key(exp: dict) -> str:
    vs = exp["variants"]
    return next((v["key"] for v in vs if v.get("baseline")), vs[0]["key"])


def variant_counts(cur, exp_id: int) -> dict:
    """{variant: (people, positive)} from the sequences already assigned."""
    cur.execute("""select s.variant, count(*) people,
                          count(*) filter (where exists (select 1 from gtm.replies r
                               where r.person_id = s.person_id and r.class = 'interested'
                                 and r.received_at >= s.created_at)) positive
                   from gtm.sequences s where s.experiment_id = %s group by s.variant""", (exp_id,))
    return {r["variant"]: (r["people"], r["positive"]) for r in cur.fetchall()}


def choose_variant(exp: dict, counts: dict, rng: random.Random) -> tuple[str, str]:
    """Holdout share gets the baseline; the rest goes to the variant with the highest draw from
    Beta(1 + positive, 1 + people - positive): early leaders get more volume, nobody is starved."""
    if rng.random() * 100 < exp.get("holdout_pct", 10):
        return baseline_key(exp), "holdout"
    best, best_draw = None, -1.0
    for v in exp["variants"]:
        people, pos = counts.get(v["key"], (0, 0))
        draw = rng.betavariate(1 + pos, 1 + max(0, people - pos))
        if draw > best_draw:
            best, best_draw = v["key"], draw
    return best, "sampled"


# ---------- planning ----------

def _companies(cur) -> list[dict]:
    # Companies with a warm lead first, then by score.
    cur.execute("""select c.*, exists (select 1 from gtm.people p where p.company_id = c.id and p.is_warm) warm
                   from gtm.companies c
                   where c.kind in ('prospect', 'referral_partner') and c.stage in ('new', 'contacting')
                   order by warm desc, c.score desc nulls last, c.id""")
    return cur.fetchall()


def _people(cur, company_id: int) -> list[dict]:
    cur.execute("""select p.*, exists (select 1 from gtm.suppression s where s.email = lower(p.email)) suppressed,
                          exists (select 1 from gtm.sequences q where q.person_id = p.id) sequenced
                   from gtm.people p where p.company_id = %s order by p.is_warm desc, p.id""", (company_id,))
    return cur.fetchall()


def _recent_start(cur, company_id: int, since: date) -> bool:
    cur.execute("""select 1 from gtm.sequences s join gtm.people p on p.id = s.person_id
                   where p.company_id = %s and s.started_on > %s limit 1""", (company_id, since))
    return cur.fetchone() is not None


def track_opens(step: int, channel: str, mode: str) -> bool:
    """config/outreach.json open_tracking: all | followups | off."""
    if channel != "email" or mode == "off":
        return False
    return mode == "all" or step > 1


def _open_mode() -> str:
    try:
        from .drafts import settings
        mode = settings().get("open_tracking", "all")
    except (OSError, ValueError):
        mode = "all"
    return mode if mode in ("all", "followups", "off") else "all"


def start_sequence(cur, person: dict, company: dict, today: date, exp: dict | None, rng: random.Random,
                   email_ok: bool = True, open_mode: str = "all") -> int | None:
    kind = sequence_kind(person, company)
    variant, assignment, order = None, "fixed", "email_first"
    sender = rng.choice(senders())
    if exp:
        variant, assignment = choose_variant(exp, variant_counts(cur, exp["id"]), rng)
        if exp["variable"] == "sender" and variant in MAILBOXES:
            sender = MAILBOXES[variant]
        if exp["variable"] == "channel_order":
            order = variant
    plan = channel_plan(person, kind, order, email_ok)
    if not plan:
        return None
    start = business_day(today)
    cur.execute("""insert into gtm.sequences (person_id, kind, sender, experiment_id, variant, assignment, started_on)
                   values (%s, %s, %s, %s, %s, %s, %s) returning id""",
                (person["id"], kind, sender, exp["id"] if exp else None, variant, assignment, start))
    seq_id = cur.fetchone()["id"]
    for step, channel in plan:
        cur.execute("""insert into gtm.touches (sequence_id, step, channel, scheduled_for, status, track_opens, utm)
                       values (%s, %s, %s, %s, 'planned', %s, %s)""",
                    (seq_id, step, channel, add_business_days(start, OFFSETS[step]), track_opens(step, channel, open_mode),
                     Jsonb({"source": "gtm", "campaign": exp["name"] if exp else "none",
                            "content": variant or "none"})))
    return seq_id


def plan_day(conn, today: date | None = None, new_limit: int = 20, rng: random.Random | None = None) -> list[dict]:
    """Start up to `new_limit` sequences. One short transaction per company."""
    today = today or date.today()
    rng = rng or random.Random()
    started = []
    with conn.cursor() as cur:
        exp = running_experiment(cur)
        companies = _companies(cur)
        email_ok = not email_paused(cur)
    open_mode = _open_mode()
    if not email_ok:
        print("planner: email paused (too many recent bounces); new sequences are LinkedIn-only")
    since = add_business_days(today, -STAGGER_DAYS)
    for c in companies:
        if len(started) >= new_limit:
            break
        with conn.transaction(), conn.cursor() as cur:
            if _recent_start(cur, c["id"], since):
                continue
            people = _people(cur, c["id"])
            done = [p for p in people if p["sequenced"]]
            if len(done) >= PER_COMPANY:
                continue
            warm = [p for p in people if p["is_warm"] and not p["sequenced"]]
            pool = warm or [p for p in pick_contacts(people, c.get("employees"), PER_COMPANY) if not p["sequenced"]]
            for p in pool:
                seq = start_sequence(cur, p, c, today, exp, rng, email_ok, open_mode)
                if seq:
                    cur.execute("update gtm.companies set stage = 'contacting', updated_at = now() "
                                "where id = %s and stage = 'new'", (c["id"],))
                    started.append({"sequence_id": seq, "person": p["full_name"], "company": c["name"]})
                    break
    return started


def due_touches(conn, today: date | None = None) -> list[dict]:
    """Planned touches to draft today: active sequence, scheduled on or before today, and the
    previous step already sent on an earlier day (follow-ups never go out before step 1 does)."""
    today = today or date.today()
    with conn.cursor() as cur:
        cur.execute("""select t.id from gtm.touches t join gtm.sequences s on s.id = t.sequence_id
                       where t.status = 'planned' and s.status = 'active' and t.scheduled_for <= %s
                         and not exists (select 1 from gtm.touches e where e.sequence_id = t.sequence_id
                                         and e.step < t.step
                                         and (e.status not in ('sent', 'skipped') or e.sent_at::date >= %s))
                       order by t.scheduled_for, t.id""", (today, today))
        return [r["id"] for r in cur.fetchall()]
