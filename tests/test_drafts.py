import json
import random
from datetime import date, datetime, timezone

import pytest

from gtm import cli, drafts, importer, planner

MON = date(2026, 10, 26)
CFG = {"email_footer": "Pavoot · 1 Market St, San Francisco\nReply \"stop\" to opt out.", "marketplace_live": False}


def _setup(conn, email=True, li=True):
    p = {"full_name": "Jane Doe", "title": "VP Marketing"}
    if email:
        p["email"] = "jane@acme.com"
    if li:
        p["linkedin_url"] = "https://www.linkedin.com/in/jane"
    importer.import_run(conn, {"companies": [{"name": "Acme", "domain": "acme.com", "source": "non_yc",
                                              "employees": 90, "notes": "Series A, NYC",
                                              "signals": [{"type": "funding", "detail": "Series A", "url": "https://n"}],
                                              "people": [p]}]})
    seq = planner.plan_day(conn, MON, rng=random.Random(0))[0]["sequence_id"]
    with conn.cursor() as cur:
        cur.execute("select id, step, channel from gtm.touches where sequence_id = %s order by step", (seq,))
        return {r["step"]: r["id"] for r in cur.fetchall()}


def _touch(conn, tid):
    with conn.cursor() as cur:
        cur.execute("select * from gtm.touches where id = %s", (tid,))
        return cur.fetchone()


GOOD = "Hi Jane,\n\nCongrats on the Series A. We run company events end to end.\n\nIs that on your plate?\n\nBest,\nAna"


def test_lint_blocks_and_warns():
    b, w = drafts.lint("We ran events for Stripe for $10k — great", marketplace_live=False)
    assert "names a customer / partner" in b and "mentions a price" in b
    assert any("dash" in x for x in w)
    b, _ = drafts.lint("We have 20 clients and an online event", False)
    assert "states a client count" in b and "suggests a virtual format" in b
    assert drafts.lint("see pavoot.events", False)[0] == ["mentions pavoot.events before it is live"]
    assert drafts.lint("see pavoot.events", True)[0] == []
    assert drafts.lint(GOOD, False) == ([], [])


def test_tag_links_only_pavoot_and_keeps_punctuation():
    out = drafts.tag_links("See https://pavoot.com/x?a=1. Or https://other.com/y",
                           {"source": "gtm", "campaign": "exp", "content": "b"})
    assert "https://pavoot.com/x?a=1&utm_source=gtm&utm_campaign=exp&utm_content=b." in out
    assert "https://other.com/y" in out and "other.com/y?" not in out


def test_export_due_has_context_and_no_prospect_mail(conn):
    ids = _setup(conn)
    data = drafts.export_due(conn, MON)
    assert [t["touch_id"] for t in data["touches"]] == [ids[1]]
    t = data["touches"][0]
    assert t["person"]["first_name"] == "Jane" and t["company"]["name"] == "Acme" and t["sign_off"] in ("Ana", "Gohar")
    assert t["previous_messages"] == [] and t["company"]["signals"][0]["type"] == "funding"


def test_import_step1_adds_footer_and_tracking(conn):
    ids = _setup(conn)
    body = GOOD + "\n\nhttps://pavoot.com"
    r = drafts.import_drafts(conn, {"drafts": [{"touch_id": ids[1], "subject": "events for acme", "body": body}]}, CFG)
    assert r == {"drafted": 1, "blocked": [], "skipped": []}
    t = _touch(conn, ids[1])
    assert t["status"] == "drafted" and t["subject"] == "events for acme" and t["warnings"] == []
    assert t["body"].endswith(CFG["email_footer"]) and "utm_source=gtm" in t["body"]


def test_followup_reuses_thread_subject_and_linkedin_has_no_subject(conn):
    ids = _setup(conn)
    drafts.import_drafts(conn, {"drafts": [{"touch_id": ids[1], "subject": "events for acme", "body": GOOD}]}, CFG)
    with conn.cursor() as cur:
        cur.execute("update gtm.touches set status = 'sent', sent_at = %s where id = %s",
                    (datetime(2026, 10, 26, 9, tzinfo=timezone.utc), ids[1]))
        cur.execute("update gtm.touches set status = 'skipped' where id = %s", (ids[2],))
    r = drafts.import_drafts(conn, {"drafts": [{"touch_id": ids[3], "subject": "ignored", "body": GOOD},
                                               {"touch_id": ids[2], "subject": "x", "body": "Hi"}]}, CFG)
    assert r["drafted"] == 1 and "not a planned touch" in r["skipped"][0]
    assert _touch(conn, ids[3])["subject"] == "Re: events for acme"


def test_blocked_draft_stays_planned(conn):
    ids = _setup(conn)
    r = drafts.import_drafts(conn, {"drafts": [{"touch_id": ids[1], "subject": "s",
                                                "body": "Hi Jane, we did this for Fondo."}]}, CFG)
    assert r["drafted"] == 0 and "names a customer" in r["blocked"][0]
    assert _touch(conn, ids[1])["status"] == "planned"


def test_linkedin_draft_ignores_footer_and_warns_on_length(conn):
    ids = _setup(conn, email=False)
    cfg = {**CFG, "email_footer": "Pavoot · TODO physical address"}
    r = drafts.import_drafts(conn, {"drafts": [{"touch_id": ids[1], "subject": "", "body": "Hi Jane, " + "x" * 400}]}, cfg)
    assert r["drafted"] == 1
    t = _touch(conn, ids[1])
    assert t["subject"] is None and any("300" in w for w in t["warnings"]) and "TODO" not in t["body"]


def test_footer_placeholder_blocks_email(conn):
    ids = _setup(conn)
    r = drafts.import_drafts(conn, {"drafts": [{"touch_id": ids[1], "subject": "s", "body": GOOD}]},
                             {**CFG, "email_footer": "Pavoot · TODO physical address"})
    assert r["drafted"] == 0 and "email_footer" in r["blocked"][0]


def test_paused_sequence_is_not_drafted(conn):
    ids = _setup(conn)
    with conn.cursor() as cur:
        cur.execute("update gtm.sequences set status = 'paused'")
    r = drafts.import_drafts(conn, {"drafts": [{"touch_id": ids[1], "subject": "s", "body": GOOD}]}, CFG)
    assert r["drafted"] == 0 and r["skipped"]


def test_repo_settings_file_is_valid_and_safe_by_default():
    s = drafts.settings()
    assert s["marketplace_live"] is False and "email_footer" in s


def test_cli_import_drafts_accepts_claude_answer(conn, tmp_path, monkeypatch):
    ids = _setup(conn)
    monkeypatch.setattr(drafts, "settings", lambda path=None: CFG)
    answer = tmp_path / "claude.txt"
    answer.write_text("note\n```json\n" + json.dumps({"drafts": [{"touch_id": ids[1], "subject": "s", "body": GOOD}]}) + "\n```\n")
    assert cli.main(["import-drafts", str(answer)]) == 0
    assert _touch(conn, ids[1])["status"] == "drafted"
