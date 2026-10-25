import random
from datetime import date, datetime, timezone

from psycopg.types.json import Jsonb

from gtm import importer, planner

FRI, MON = date(2026, 10, 23), date(2026, 10, 26)


def _run(*companies):
    return {"companies": list(companies)}


def _co(name, score_signal=None, people=(), employees=120, **kw):
    return {"name": name, "domain": f"{name.lower()}.com", "source": "non_yc", "employees": employees,
            "signals": [{"type": score_signal, "detail": "x", "url": f"https://{name}.com/{score_signal}"}] if score_signal else [],
            "people": list(people), **kw}


def _p(name, title, email=True, li=True):
    slug = name.lower().replace(" ", "")
    return {"full_name": name, "title": title, **({"email": f"{slug}@x.com"} if email else {}),
            **({"linkedin_url": f"https://www.linkedin.com/in/{slug}"} if li else {})}


def _all(conn, q, *a):
    with conn.cursor() as cur:
        cur.execute(q, a)
        return cur.fetchall()


# ---------- pure helpers ----------

def test_business_days():
    assert planner.add_business_days(FRI, 1) == MON
    assert planner.add_business_days(MON, 5) == date(2026, 11, 2)
    assert planner.add_business_days(MON, -2) == date(2026, 10, 22)
    assert planner.business_day(date(2026, 10, 24)) == MON


def test_channel_plans():
    both = {"email": "a@x.com", "linkedin_url": "li"}
    assert planner.channel_plan(both, "cold") == [(1, "email"), (2, "linkedin"), (3, "email"), (7, "email")]
    assert planner.channel_plan(both, "cold", "linkedin_first")[:3] == [(1, "linkedin"), (2, "email"), (3, "linkedin")]
    assert planner.channel_plan({"email": "a@x.com"}, "cold") == [(1, "email"), (3, "email"), (7, "email")]
    assert planner.channel_plan({"linkedin_url": "li"}, "cold") == [(1, "linkedin"), (3, "linkedin"), (7, "linkedin")]
    assert planner.channel_plan({"email": "a@x.com"}, "warm") == [(s, "email") for s in (1, 2, 3, 7)]
    assert planner.channel_plan({"email": "a@x.com", "email_status": "invalid"}, "cold") == []
    assert planner.channel_plan({"email": "a@x.com", "suppressed": True, "linkedin_url": "li"}, "cold")[0] == (1, "linkedin")


def test_thompson_prefers_leader_and_respects_holdout():
    exp = {"variants": [{"key": "a", "baseline": True}, {"key": "b"}], "holdout_pct": 10}
    rng = random.Random(1)
    counts = {"a": (200, 4), "b": (200, 30)}
    picks = [planner.choose_variant(exp, counts, rng) for _ in range(2000)]
    holdout = sum(1 for _, how in picks if how == "holdout")
    b = sum(1 for k, how in picks if how == "sampled" and k == "b")
    sampled = sum(1 for _, how in picks if how == "sampled")
    assert 140 < holdout < 260                      # ~10%
    assert b / sampled > 0.95                       # clear leader gets almost all sampled traffic
    even = [planner.choose_variant({**exp, "holdout_pct": 0}, {}, rng)[0] for _ in range(2000)]
    assert 800 < even.count("a") < 1200             # no data yet: roughly even


# ---------- planning against the database ----------

def test_plan_picks_top_companies_one_person_each_with_touches(conn):
    importer.import_run(conn, _run(
        _co("Hot", "new_marketing_leader", [_p("Cmo Hot", "VP Marketing"), _p("Ev Hot", "Head of Events")]),
        _co("Mild", "hiring", [_p("Ceo Mild", "CEO")], employees=20),
        _co("Nobody", "funding", [_p("Eng", "Engineer")])))
    started = planner.plan_day(conn, MON, new_limit=20, rng=random.Random(0))
    assert [s["company"] for s in started] == ["Hot", "Mild"]          # by score; Nobody has no buyer
    assert started[0]["person"] == "Cmo Hot"                             # big company: CMO first
    t = _all(conn, "select step, channel, scheduled_for, status from gtm.touches where sequence_id = %s order by step",
             started[0]["sequence_id"])
    assert [(r["step"], r["channel"]) for r in t] == [(1, "email"), (2, "linkedin"), (3, "email"), (7, "email")]
    assert [r["scheduled_for"] for r in t] == [MON, date(2026, 10, 27), date(2026, 10, 28), date(2026, 11, 2)]
    assert all(r["status"] == "planned" for r in t)
    assert _all(conn, "select stage from gtm.companies where name = 'Hot'")[0]["stage"] == "contacting"


def test_stagger_cap_and_limit(conn):
    importer.import_run(conn, _run(_co("Hot", "funding", [_p("A", "CMO"), _p("B", "Field Marketing Manager"),
                                                         _p("C", "Head of Events"), _p("D", "VP Sales")])))
    assert len(planner.plan_day(conn, MON)) == 1
    assert planner.plan_day(conn, date(2026, 10, 27)) == []              # within 2 business days
    assert planner.plan_day(conn, date(2026, 10, 28))[0]["person"] == "B"
    planner.plan_day(conn, date(2026, 10, 30))
    assert planner.plan_day(conn, date(2026, 11, 3)) == []               # 3 per company max
    assert len(_all(conn, "select * from gtm.sequences")) == 3


def test_new_limit(conn):
    importer.import_run(conn, _run(*[_co(f"C{i}", "funding", [_p(f"P{i}", "CEO")]) for i in range(5)]))
    assert len(planner.plan_day(conn, MON, new_limit=3)) == 3


def test_replied_company_and_suppressed_email(conn):
    importer.import_run(conn, _run(_co("Done", "funding", [_p("X", "CMO")]),
                                   _co("Sup", "funding", [_p("Y", "CMO", li=False)])))
    with conn.cursor() as cur:
        cur.execute("update gtm.companies set stage = 'replied' where name = 'Done'")
        cur.execute("insert into gtm.suppression (email, reason) values ('y@x.com', 'unsubscribe')")
    assert planner.plan_day(conn, MON) == []


def test_warm_leads_go_first_with_daily_cadence(conn):
    importer.import_run(conn, _run(_co("Hot", "new_marketing_leader", [_p("Cold One", "CMO")]),
                                   _co("Calm", None, [_p("Warm One", "Engineer", li=False)])))
    with conn.cursor() as cur:
        cur.execute("update gtm.people set is_warm = true where full_name = 'Warm One'")
    started = planner.plan_day(conn, MON, new_limit=1)
    assert started[0]["person"] == "Warm One"
    steps = _all(conn, "select step from gtm.touches t join gtm.sequences s on s.id = t.sequence_id "
                       "where s.kind = 'warm' order by step")
    assert [r["step"] for r in steps] == [1, 2, 3, 7]


def test_experiment_sender_variant_sets_mailbox(conn, monkeypatch):
    importer.import_run(conn, _run(*[_co(f"C{i}", "funding", [_p(f"P{i}", "CEO")]) for i in range(30)]))
    with conn.cursor() as cur:
        cur.execute("""insert into gtm.experiments (name, variable, hypothesis, win_condition, variants, status, holdout_pct)
                       values ('sender-1', 'sender', 'h', 'w', %s, 'running', 0)""",
                    (Jsonb([{"key": "ana", "baseline": True}, {"key": "gohar"}]),))
    planner.plan_day(conn, MON, new_limit=30, rng=random.Random(3))
    rows = _all(conn, "select variant, sender, assignment from gtm.sequences")
    assert all(planner.MAILBOXES[r["variant"]] == r["sender"] and r["assignment"] == "sampled" for r in rows)
    assert {r["variant"] for r in rows} == {"ana", "gohar"}
    utm = _all(conn, "select utm from gtm.touches limit 1")[0]["utm"]
    assert utm["campaign"] == "sender-1" and utm["content"] in ("ana", "gohar")


def test_gtm_senders_env_restricts_mailboxes(conn, monkeypatch):
    monkeypatch.setenv("GTM_SENDERS", "ana")
    importer.import_run(conn, _run(*[_co(f"C{i}", "funding", [_p(f"P{i}", "CEO")]) for i in range(6)]))
    planner.plan_day(conn, MON, rng=random.Random(5))
    assert {r["sender"] for r in _all(conn, "select sender from gtm.sequences")} == {"ana@pavoot.com"}


def test_due_waits_for_previous_step_to_be_sent(conn):
    importer.import_run(conn, _run(_co("Hot", "funding", [_p("A", "CMO")])))
    seq = planner.plan_day(conn, MON)[0]["sequence_id"]
    ids = {r["step"]: r["id"] for r in _all(conn, "select id, step from gtm.touches where sequence_id = %s", seq)}
    assert planner.due_touches(conn, MON) == [ids[1]]
    assert planner.due_touches(conn, date(2026, 10, 28)) == [ids[1]]     # step 2/3 wait for step 1
    with conn.cursor() as cur:
        cur.execute("update gtm.touches set status = 'sent', sent_at = %s where id = %s",
                    (datetime(2026, 10, 28, 9, tzinfo=timezone.utc), ids[1]))
    assert planner.due_touches(conn, date(2026, 10, 28)) == []          # never the same day as the previous send
    assert planner.due_touches(conn, date(2026, 10, 29)) == [ids[2]]
    with conn.cursor() as cur:
        cur.execute("update gtm.touches set status = 'skipped' where id = %s", (ids[2],))   # LinkedIn skipped
    assert planner.due_touches(conn, date(2026, 10, 29)) == [ids[3]]
    with conn.cursor() as cur:
        cur.execute("update gtm.sequences set status = 'paused' where id = %s", (seq,))
    assert planner.due_touches(conn, date(2026, 10, 29)) == []


def test_bounce_circuit_breaker_makes_new_sequences_linkedin_only(conn):
    importer.import_run(conn, _run(_co("New", "hiring", [_p("N", "CMO")])))
    with conn.cursor() as cur:
        # 40 sent emails, 3 bounced = 7.5%: ten past sequences x four steps
        for i in range(10):
            cur.execute("insert into gtm.people (full_name, email) values (%s, %s) returning id", (f"X{i}", f"x{i}@y.com"))
            pid = cur.fetchone()["id"]
            cur.execute("insert into gtm.sequences (person_id, kind, sender, status) values (%s, 'cold', 'ana@pavoot.com', "
                        "'finished') returning id", (pid,))
            sid = cur.fetchone()["id"]
            for step in (1, 2, 3, 7):
                bounced = i == 0 and step != 7
                cur.execute("insert into gtm.touches (sequence_id, step, channel, scheduled_for, status, sent_at) "
                            "values (%s, %s, 'email', %s, %s, now())", (sid, step, MON, "bounced" if bounced else "sent"))
        assert planner.email_paused(cur)
    new = planner.plan_day(conn, MON)[0]["sequence_id"]
    chans = {r["channel"] for r in _all(conn, "select channel from gtm.touches where sequence_id = %s", new)}
    assert chans == {"linkedin"}
    with conn.cursor() as cur:
        cur.execute("update gtm.touches set status = 'sent' where status = 'bounced'")
        assert not planner.email_paused(cur)


def test_open_tracking_setting(conn, monkeypatch):
    assert planner.track_opens(1, "email", "all") and not planner.track_opens(1, "email", "followups")
    assert planner.track_opens(3, "email", "followups") and not planner.track_opens(3, "email", "off")
    assert not planner.track_opens(1, "linkedin", "all")
    monkeypatch.setattr(planner, "_open_mode", lambda: "followups")
    importer.import_run(conn, _run(_co("Hot", "funding", [_p("A", "CMO")])))
    seq = planner.plan_day(conn, MON)[0]["sequence_id"]
    rows = _all(conn, "select step, channel, track_opens from gtm.touches where sequence_id = %s order by step", seq)
    assert [(r["step"], r["track_opens"]) for r in rows] == [(1, False), (2, False), (3, True), (7, True)]
