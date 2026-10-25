import json
from datetime import date, datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from gtm import cli, learn, score

WEEK = date(2026, 10, 19)
IN_WEEK = datetime(2026, 10, 21, 10, tzinfo=timezone.utc)


def _seed(conn, n_with=40, pos_with=12, n_without=40, pos_without=2, experiment=True):
    """Contacted people at companies with / without a `funding` signal; positives as given."""
    with conn.cursor() as cur:
        if experiment:
            cur.execute("""insert into gtm.experiments (name, variable, hypothesis, win_condition, variants, status)
                           values ('angle-1', 'angle', 'h', 'w', %s, 'running') returning id""",
                        (Jsonb([{"key": "a", "baseline": True}, {"key": "b"}]),))
            exp = cur.fetchone()["id"]
        else:
            exp = None
        i = 0
        for has_signal, n, pos in ((True, n_with, pos_with), (False, n_without, pos_without)):
            for j in range(n):
                i += 1
                sig = [{"type": "funding", "detail": "A"}] if has_signal else [{"type": "hiring", "detail": "h"}]
                cur.execute("insert into gtm.companies (name, domain, source, signals, employees, hq_city) "
                            "values (%s, %s, 'non_yc', %s, 80, 'San Francisco, CA') returning id",
                            (f"C{i}", f"c{i}.com", Jsonb(sig)))
                cid = cur.fetchone()["id"]
                cur.execute("insert into gtm.people (company_id, full_name, role, email) values (%s, %s, 'CMO', %s) returning id",
                            (cid, f"P{i}", f"p{i}@c{i}.com"))
                pid = cur.fetchone()["id"]
                cur.execute("insert into gtm.sequences (person_id, kind, sender, experiment_id, variant, created_at) "
                            "values (%s, 'cold', 'ana@pavoot.com', %s, %s, %s) returning id",
                            (pid, exp, "b" if has_signal else "a", IN_WEEK - timedelta(days=1)))
                sid = cur.fetchone()["id"]
                cur.execute("insert into gtm.touches (sequence_id, step, channel, scheduled_for, subject, body, status, sent_at) "
                            "values (%s, 1, 'email', %s, 's', %s, 'sent', %s) returning id",
                            (sid, IN_WEEK.date(), f"body {i}", IN_WEEK))
                tid = cur.fetchone()["id"]
                if j < pos:
                    cur.execute("insert into gtm.replies (touch_id, person_id, received_at, class) values (%s, %s, %s, 'interested')",
                                (tid, pid, IN_WEEK + timedelta(hours=3)))


def test_p_best():
    pb = learn.p_best({"a": (200, 4), "b": (200, 30)})
    assert pb["b"] > 0.99 and abs(sum(pb.values()) - 1) < 0.01
    even = learn.p_best({"a": (10, 1), "b": (10, 1)})
    assert 0.35 < even["a"] < 0.65
    assert learn.p_best({}) == {}


def test_stats_breakdowns_and_verdict(conn):
    _seed(conn)
    st = learn.stats(conn, WEEK)
    assert st["week_sends"] == {"sent": 80, "bounced": 0, "tracked": 0, "opened": 0} and st["week_replies"] == {"interested": 14}
    assert {"sent", "opened", "open_rate_approx"} <= set(st["experiments"][0]["arms"][0])
    e = st["experiments"][0]
    assert e["leader"] == "b" and e["verdict"] == "leaning"          # < 100 per arm: never "clear"
    sig = {r["signal"]: r for r in st["breakdowns"]["signal"]}
    assert sig["funding"]["n"] == 40 and sig["funding"]["positive"] == 12
    assert st["breakdowns"]["role"][0] == {"role": "CMO", "n": 80, "replied": 14, "positive": 14, "meetings": 0,
                                           "positive_rate": 0.175}
    assert len(st["examples"]) == 16 and st["examples"][0]["positive"] is True


def test_clear_verdict_needs_big_arms(conn):
    _seed(conn, n_with=120, pos_with=30, n_without=120, pos_without=3)
    assert learn.stats(conn, WEEK)["experiments"][0]["verdict"] == "clear"


def test_weight_proposals_bounded_and_need_approval(conn):
    _seed(conn, experiment=False)
    st = learn.stats(conn, WEEK)
    props = learn.propose_weights(conn, st)
    funding = next(p for p in props if p["kind"] == "funding")
    assert funding["from"] == 30 and funding["to"] == 39                  # lift 2.1x, capped at +30%
    hiring = next(p for p in props if p["kind"] == "hiring")
    assert hiring["to"] == 4                                              # lift 0.29x, capped at -30% (5 -> 3.5 -> 4)
    assert score.current_weights(conn)["funding"] == 30                   # proposals don't apply by themselves
    with conn.cursor() as cur:
        cur.execute("update gtm.score_weights set status = 'approved', valid_from = now() where kind = 'funding'")
    assert score.current_weights(conn)["funding"] == 39
    assert score.rescore_all(conn) > 0
    with conn.cursor() as cur:
        cur.execute("select score from gtm.companies where name = 'C1'")
        assert cur.fetchone()["score"] == 39 + 10 + 5


def test_no_proposals_without_enough_data(conn):
    _seed(conn, n_with=10, pos_with=5, n_without=10, pos_without=1, experiment=False)
    assert learn.propose_weights(conn, learn.stats(conn, WEEK)) == []


def test_import_learnings_validates_and_reports(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(learn, "ROOT", str(tmp_path))
    _seed(conn)
    st = json.loads(json.dumps(learn.stats(conn, WEEK), default=str))
    data = {"learnings": [
        {"claim": "Lead with funding news", "evidence": "b 12/40 vs a 2/40", "n": 80, "status": "active"},
        {"claim": "Short subjects", "evidence": "examples", "n": 12, "status": "active"},     # tiny n -> leaning
        {"claim": "No evidence", "n": 50},                                                  # dropped
        {"claim": "Bad n", "evidence": "x", "n": "lots"}]}                                  # dropped
    r = learn.import_learnings(conn, st, data, [{"kind": "funding"}])
    assert r == {"learnings": 2, "dropped": 2, "proposals": 1, "next_experiment": None}
    with conn.cursor() as cur:
        cur.execute("select claim, status from gtm.learnings order by id")
        assert [tuple(x.values()) for x in cur.fetchall()] == [("Lead with funding news", "active"), ("Short subjects", "leaning")]
        cur.execute("select summary from gtm.weekly_reports where week = %s", (WEEK,))
        summary = cur.fetchone()["summary"]
    assert "Sent 80" in summary and "angle-1" in summary and "waiting for your ✅" in summary
    assert "Lead with funding news" in (tmp_path / "learnings.md").read_text()
    # next week's import retires last week's set
    st2 = {**st, "week": str(WEEK + timedelta(days=7))}
    learn.import_learnings(conn, st2, {"learnings": []}, [])
    with conn.cursor() as cur:
        cur.execute("select count(*) n from gtm.learnings where status <> 'retired'")
        assert cur.fetchone()["n"] == 0


def test_drafting_export_includes_active_learnings(conn):
    from gtm import drafts
    with conn.cursor() as cur:
        cur.execute("insert into gtm.learnings (week, claim, evidence, n, status) values "
                    "(%s, 'A', 'e', 50, 'active'), (%s, 'R', 'e', 50, 'retired')", (WEEK, WEEK))
    assert [x["claim"] for x in drafts.export_due(conn, date(2026, 10, 26))["learnings"]] == ["A"]


def test_cli_learn_roundtrip(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(learn, "ROOT", str(tmp_path))
    _seed(conn)
    out = tmp_path / "stats.json"
    assert cli.main(["learn-stats", "--week", str(WEEK), "--out", str(out)]) == 0
    answer = tmp_path / "learn.txt"
    answer.write_text('note\n```json\n{"learnings": [{"claim": "c", "evidence": "e", "n": 80, "status": "active"}]}\n```')
    assert cli.main(["learn-import", str(answer), "--stats", str(out)]) == 0
    with conn.cursor() as cur:
        cur.execute("select count(*) n from gtm.weekly_reports")
        assert cur.fetchone()["n"] == 1


NEXT = {"name": "angle-2", "variable": "angle", "hypothesis": "h", "win_condition": "w",
        "variants": [{"key": "time", "description": "Lead with the time it saves."},
                     {"key": "pipeline", "description": "Lead with who they will meet."}]}


def test_valid_experiment_rules():
    assert learn.valid_experiment(NEXT) == ""
    assert "unknown variable" in learn.valid_experiment({**NEXT, "variable": "colour"})
    assert "2 or 3" in learn.valid_experiment({**NEXT, "variants": NEXT["variants"][:1]})
    assert "must be" in learn.valid_experiment({**NEXT, "variable": "sender"})
    bad = {**NEXT, "variants": [NEXT["variants"][0], {"key": "x", "description": "Mention we work with Stripe"}]}
    assert "disclosure" in learn.valid_experiment(bad)


def test_import_stores_proposed_experiment_as_draft(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(learn, "ROOT", str(tmp_path))
    _seed(conn, experiment=False)
    st = json.loads(json.dumps(learn.stats(conn, WEEK), default=str))
    r = learn.import_learnings(conn, st, {"learnings": [], "next_experiment": NEXT}, [])
    assert r["next_experiment"] == "angle-2"
    with conn.cursor() as cur:
        cur.execute("select status, variants from gtm.experiments")
        row = cur.fetchone()
        assert row["status"] == "draft" and row["variants"][0]["baseline"] is True
        cur.execute("select summary from gtm.weekly_reports")
        assert "Next A/B test proposed: angle-2" in cur.fetchone()["summary"]
    # a new proposal replaces an unstarted one; the same name gets a suffix
    r = learn.import_learnings(conn, st, {"learnings": [], "next_experiment": NEXT}, [])
    with conn.cursor() as cur:
        cur.execute("select name from gtm.experiments")
        names = [x["name"] for x in cur.fetchall()]
    assert len(names) == 1 and names[0].startswith("angle-2-")
    r = learn.import_learnings(conn, st, {"learnings": [], "next_experiment": {**NEXT, "variable": "colour"}}, [])
    assert r["next_experiment"] is None and "unknown variable" in r["experiment_rejected"]
