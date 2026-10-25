import json
from datetime import date

import psycopg
import pytest

from gtm import cli, importer, roles, score, yc


# ---------- roles ----------

@pytest.mark.parametrize("title,role", [
    ("Co-founder & CEO", "CEO"),
    ("Founder", "CEO"),
    ("VP of Marketing", "CMO"),
    ("Chief Marketing Officer", "CMO"),
    ("Senior Field Marketing Manager", "Field Marketing"),
    ("Head of Events & Community", "Head of Events"),
    ("Event Marketing Manager", "Head of Events"),
    ("Head of Product", "CPO"),
    ("Head of Growth", "GTM/Growth"),
    ("VP Sales", "GTM/Growth"),
    ("Co-founder & CTO", "Other"),
    ("Founder & Chief Scientist", "Other"),
    ("Co-Founder & CEO / CTO", "CEO"),
    ("Software Engineer", "Other"),
    (None, "Other"),
])
def test_role_for(title, role):
    assert roles.role_for(title) == role


def test_pick_contacts_small_vs_big_and_cap():
    people = [{"role": r, "linkedin_url": f"https://www.linkedin.com/in/{r}"} for r in
              ("CEO", "CMO", "Field Marketing", "Head of Events", "GTM/Growth", "CPO")]
    assert [p["role"] for p in roles.pick_contacts(people, 20)] == ["CEO", "GTM/Growth", "CMO"]
    assert [p["role"] for p in roles.pick_contacts(people, 120)] == ["CMO", "Field Marketing", "Head of Events"]


def test_pick_contacts_skips_unreachable_and_duplicate_roles():
    people = [{"role": "CMO", "email": "a@x.com", "email_status": "invalid"},
              {"role": "CMO", "email": "b@x.com"},
              {"role": "CMO", "linkedin_url": "https://www.linkedin.com/in/c"},
              {"role": "CEO"}]
    picked = roles.pick_contacts(people, 100)
    assert [p.get("email") for p in picked] == ["b@x.com"]


# ---------- score ----------

def test_score_weights_freshness_and_dedupe():
    today = date(2026, 10, 24)
    c = {"signals": [{"type": "new_marketing_leader", "seen_at": "2026-10-01"},
                     {"type": "funding", "seen_at": "2025-01-01"},          # old -> half
                     {"type": "funding", "seen_at": "2026-10-01"},          # same kind counted once
                     {"type": "unknown_kind"}],
         "employees": 80, "hq_city": "San Francisco, CA, USA"}
    assert score.score(c, today) == 40 + 15 + 10 + 5


# ---------- yc ----------

def _yc(**kw):
    base = {"name": "Acme", "slug": "acme", "website": "https://www.Acme.com/", "status": "Active",
            "nonprofit": False, "team_size": 80, "batch": "Summer 2021", "isHiring": True,
            "all_locations": "San Francisco, CA, USA; New York, NY, USA", "one_liner": "Rockets",
            "founders": [{"name": "Jane Doe", "title": "CEO", "linkedin_url": "https://linkedin.com/in/jane/",
                          "is_active": True},
                         {"name": "Old Founder", "title": "CTO", "is_active": False}]}
    base.update(kw)
    return base


def test_yc_filter_and_shape(tmp_path):
    rows = [_yc(),
            _yc(name="Tiny", website="tiny.io", team_size=5),
            _yc(name="Fresh", website="fresh.io", batch="Spring 2026"),
            _yc(name="Dead", website="dead.io", status="Inactive"),
            _yc(name="NoSite", website=None),
            _yc(name="Known", website="known.io"),
            _yc(name="Big", website="big.io", team_size=290, isHiring=False),
            _yc(name="Lagos", website="lagos.io", all_locations="Lagos, LA, Nigeria")]
    (tmp_path / "yc_x.json").write_text(json.dumps(rows))
    got = yc.candidates({"known.io"}, 10, yc_dir=str(tmp_path))
    assert [c["name"] for c in got] == ["Acme", "Big"]
    a = got[0]
    assert a["domain"] == "acme.com" and a["source"] == "yc" and a["hq_city"] == "San Francisco, CA, USA"
    assert [p["full_name"] for p in a["people"]] == ["Jane Doe"]
    assert a["signals"][0]["type"] == "hiring"


# ---------- importer ----------

RUN = {"companies": [{
    "name": "Acme", "domain": "https://www.acme.com", "source": "non_yc", "hq_city": "New York, NY",
    "employees": 90, "funding_stage": "Series A",
    "signals": [{"type": "funding", "detail": "Series A $20M", "url": "https://news/acme", "seen_at": "2026-10-01"}],
    "people": [{"full_name": "Jane Doe", "title": "VP Marketing", "email": "Jane@Acme.com", "email_status": "valid",
                "linkedin_url": "https://linkedin.com/in/JaneDoe/?trk=x", "source": "web"},
               {"full_name": "Sam Roe", "title": "Field Marketing Lead", "linkedin_url": "https://linkedin.com/in/samroe"},
               {"full_name": "No Contact", "title": "Engineer"}]}]}


def _one(conn, q, *a):
    with conn.cursor() as cur:
        cur.execute(q, a)
        return cur.fetchone()


def test_import_inserts_and_normalizes(conn):
    s = importer.import_run(conn, RUN)
    assert (s.companies_new, s.people_new, s.errors) == (1, 3, [])
    c = _one(conn, "select * from gtm.companies")
    assert c["domain"] == "acme.com" and c["score"] == 30 + 10 + 5 and c["stage"] == "new"
    jane = _one(conn, "select * from gtm.people where full_name = 'Jane Doe'")
    assert jane["email"] == "jane@acme.com" and jane["role"] == "CMO" and jane["email_status"] == "valid"
    assert jane["linkedin_url"] == "https://www.linkedin.com/in/janedoe"
    assert _one(conn, "select role from gtm.people where full_name = 'Sam Roe'")["role"] == "Field Marketing"


def test_reimport_is_idempotent(conn):
    importer.import_run(conn, RUN)
    s = importer.import_run(conn, RUN)
    assert (s.companies_new, s.people_new, s.conflicts, s.companies_updated) == (0, 0, 0, 0)
    assert _one(conn, "select count(*) n from gtm.people")["n"] == 3


def test_never_overwrites_differences_become_conflicts(conn):
    importer.import_run(conn, RUN)
    changed = json.loads(json.dumps(RUN))
    co = changed["companies"][0]
    co["employees"] = 120                                   # differs -> conflict
    co["linkedin_url"] = "https://linkedin.com/company/acme"  # was empty -> filled
    co["signals"].append({"type": "new_marketing_leader", "detail": "new CMO", "seen_at": "2026-10-20"})
    co["people"][0]["title"] = "CMO"                        # differs -> conflict
    s = importer.import_run(conn, changed)
    c = _one(conn, "select * from gtm.companies")
    assert c["employees"] == 90 and c["linkedin_url"] == "https://linkedin.com/company/acme"
    assert len(c["signals"]) == 2 and c["score"] == 30 + 40 + 10 + 5
    assert _one(conn, "select title from gtm.people where full_name = 'Jane Doe'")["title"] == "VP Marketing"
    assert s.conflicts == 2
    # the same differing value again does not pile up duplicate conflicts
    assert importer.import_run(conn, changed).conflicts == 0
    assert _one(conn, "select count(*) n from gtm.conflicts where status = 'open'")["n"] == 2


def test_matches_person_by_linkedin_when_email_missing(conn):
    importer.import_run(conn, RUN)
    again = {"companies": [{"name": "Acme", "domain": "acme.com", "source": "non_yc",
                            "people": [{"full_name": "Sam Roe", "email": "sam@acme.com",
                                        "linkedin_url": "https://www.linkedin.com/in/samroe/"}]}]}
    s = importer.import_run(conn, again)
    assert (s.people_new, s.people_updated) == (0, 1)
    assert _one(conn, "select email from gtm.people where full_name = 'Sam Roe'")["email"] == "sam@acme.com"


def test_suppressed_email_is_skipped(conn):
    with conn.cursor() as cur:
        cur.execute("insert into gtm.suppression (email, reason) values ('jane@acme.com', 'unsubscribe')")
    s = importer.import_run(conn, RUN)
    assert s.suppressed == 1 and _one(conn, "select count(*) n from gtm.people where email = 'jane@acme.com'")["n"] == 0


def test_bad_company_does_not_block_others(conn):
    run = {"companies": [{"name": "Bad", "source": "made_up"}, {"name": ""}, RUN["companies"][0]]}
    s = importer.import_run(conn, run)
    assert s.companies_new == 1 and len(s.errors) == 2 and s.people_new == 3


def test_failed_company_rolls_back_and_is_not_counted(conn, monkeypatch):
    real = importer._person

    def boom(cur, p, *a):
        if p["full_name"] == "Sam Roe":
            raise RuntimeError("boom")
        return real(cur, p, *a)
    monkeypatch.setattr(importer, "_person", boom)
    s = importer.import_run(conn, RUN)
    assert (s.companies_new, s.people_new, len(s.errors)) == (0, 0, 1)
    assert _one(conn, "select count(*) n from gtm.companies")["n"] == 0


def test_bookface_redirect_linkedin_dropped():
    assert importer.norm_linkedin("https://bookface.ycombinator.com/user/1/linkedin_redirect?key=x") is None


def test_agent_login_cannot_touch_app_tables(conn):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("select * from public.app_secret")


def test_cli_yc_candidates_and_import(conn, tmp_path, monkeypatch, capsys):
    (tmp_path / "yc").mkdir()
    (tmp_path / "yc" / "yc_x.json").write_text(json.dumps([_yc()]))
    monkeypatch.setattr(yc, "YC_DIR", str(tmp_path / "yc"))
    monkeypatch.setattr(yc.candidates, "__defaults__", (str(tmp_path / "yc"),))
    out = tmp_path / "run.json"
    assert cli.main(["yc-candidates", "--limit", "5", "--out", str(out)]) == 0
    assert cli.main(["import", str(out)]) == 0
    assert _one(conn, "select source from gtm.companies")["source"] == "yc"
    # second time the company is known, so no candidates
    cli.main(["yc-candidates", "--limit", "5", "--out", str(out)])
    assert json.loads(out.read_text())["companies"] == []


def test_cli_known_domains_includes_never_prospect_list(conn, tmp_path, monkeypatch):
    excl = tmp_path / "exclude.txt"
    excl.write_text("# customers\nFondo.com  # customer\n\n")
    monkeypatch.setattr(cli, "EXCLUDE_FILE", str(excl))
    monkeypatch.setattr(cli.excluded_domains, "__defaults__", (str(excl),))
    importer.import_run(conn, RUN)
    out = tmp_path / "known.txt"
    assert cli.main(["known-domains", "--out", str(out)]) == 0
    assert out.read_text() == "acme.com\nfondo.com\n"


def test_real_exclude_list_blocks_fondo():
    assert "fondo.com" in cli.excluded_domains()


def test_extract_json_takes_last_valid_companies_block():
    text = ("summary\n```json\n{\"companies\": [{\"name\": \"Old\"}]}\n```\n"
            "```json\n{not valid\n```\n```json\n{\"companies\": [{\"name\": \"New\"}]}\n```\n")
    assert cli.extract_json(text)["companies"][0]["name"] == "New"
    with pytest.raises(ValueError):
        cli.extract_json("no json here")


def test_rls_on_and_api_roles_locked_out(conn, anon):
    with conn.cursor() as cur:
        cur.execute("select count(*) n from pg_tables where schemaname = 'gtm' and not rowsecurity")
        assert cur.fetchone()["n"] == 0                     # every gtm table has RLS
        cur.execute("insert into gtm.suppression (email, reason) values ('a@b.co', 'bounce')")
        cur.execute("select count(*) n from gtm.suppression")
        assert cur.fetchone()["n"] == 1                     # the agent still sees its rows
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        anon.execute("select * from gtm.suppression")
