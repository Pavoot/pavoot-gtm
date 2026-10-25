"""python -m gtm.cli <command>

  yc-candidates --limit N --out FILE   YC companies not yet in the CRM, as a run file
  known-domains --out FILE             domains in the CRM + never-prospect list, one per line
  extract-json TEXT_FILE --out FILE    pull the last ```json block out of a run's answer
  import FILE                          add a prospecting run to the CRM (additive)
  plan [--limit N]                     start today's sequences (default 20 new people)
  export-due --out FILE                today's due touches + context for the drafting run
  import-drafts FILE                   store drafts written by the drafting run (lint + footer)
  rescore                              recompute company scores with the approved weights
  learn-stats --out FILE               last week's numbers + weight proposals (weekly run)
  learn-import FILE --stats FILE       store the learning run's learnings + the weekly report
  stats                                counts per table / stage
  conflicts                            open conflicts waiting for Ana
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import date, timedelta

from . import db, drafts, importer, learn, planner, score, yc


EXCLUDE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config", "exclude_domains.txt")


def excluded_domains(path: str = EXCLUDE_FILE) -> set[str]:
    """Customers / partners that must never be prospected (config/exclude_domains.txt)."""
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        return {ln.split("#")[0].strip().lower() for ln in f} - {""}


def _known_domains(conn) -> set[str]:
    """Domains already in the CRM plus the never-prospect list."""
    with conn.cursor() as cur:
        cur.execute("select lower(domain) as d from gtm.companies where domain is not null")
        return {r["d"] for r in cur.fetchall()} | excluded_domains()


def extract_json(text: str, key: str = "companies") -> dict:
    """The last ```json fenced block in a headless run's answer, parsed. The run has no write
    access on purpose; it returns the result in its answer instead."""
    blocks = re.findall(r"```json\s*\n(.*?)\n```", text, re.S)
    for b in reversed(blocks):
        try:
            data = json.loads(b)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get(key), list):
            return data
    raise ValueError(f"no ```json block with a {key} list found")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gtm")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("yc-candidates")
    c.add_argument("--limit", type=int, default=10)
    c.add_argument("--out", required=True)
    k = sub.add_parser("known-domains")
    k.add_argument("--out", required=True)
    i = sub.add_parser("import")
    i.add_argument("file")
    pl = sub.add_parser("plan")
    pl.add_argument("--limit", type=int, default=int(os.environ.get("GTM_DAILY_NEW", "20")))
    pl.add_argument("--date", type=date.fromisoformat, default=None, help="plan as if today were DATE (tests)")
    ed = sub.add_parser("export-due")
    ed.add_argument("--out", required=True)
    ed.add_argument("--date", type=date.fromisoformat, default=None, help="as if today were DATE (tests)")
    idr = sub.add_parser("import-drafts")
    idr.add_argument("file")
    sub.add_parser("rescore")
    ls = sub.add_parser("learn-stats")
    ls.add_argument("--out", required=True)
    ls.add_argument("--week", type=date.fromisoformat, default=None, help="Monday of the week (default: last week)")
    li = sub.add_parser("learn-import")
    li.add_argument("file")
    li.add_argument("--stats", required=True)
    sub.add_parser("stats")
    sub.add_parser("conflicts")
    x = sub.add_parser("extract-json")
    x.add_argument("file")
    x.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "extract-json":  # no database needed
        with open(a.file) as f:
            data = extract_json(f.read())
        with open(a.out, "w") as f:
            json.dump(data, f, indent=2)
        print(f"{len(data['companies'])} companies -> {a.out}")
        return 0

    with db.connect() as conn:
        if a.cmd == "yc-candidates":
            rows = yc.candidates(_known_domains(conn), a.limit)
            with open(a.out, "w") as f:
                json.dump({"companies": rows}, f, indent=2)
            print(f"{len(rows)} YC candidates -> {a.out}")
        elif a.cmd == "known-domains":
            with open(a.out, "w") as f:
                f.write("".join(d + "\n" for d in sorted(_known_domains(conn))))
        elif a.cmd == "import":
            s = importer.import_file(conn, a.file)
            print(json.dumps(asdict(s), indent=2))
            return 1 if s.errors else 0
        elif a.cmd == "plan":
            started = planner.plan_day(conn, a.date, new_limit=a.limit)
            print(json.dumps({"started": len(started), "sequences": started}, indent=2))
        elif a.cmd == "export-due":
            data = drafts.export_due(conn, a.date)
            with open(a.out, "w") as f:
                json.dump(data, f, indent=2, default=str)
            print(f"{len(data['touches'])} touches due -> {a.out}")
        elif a.cmd == "import-drafts":
            with open(a.file) as f:
                raw = f.read()
            data = json.loads(raw) if raw.lstrip().startswith("{") else extract_json(raw, key="drafts")
            report = drafts.import_drafts(conn, data)
            print(json.dumps(report, indent=2))
            return 1 if report["blocked"] else 0
        elif a.cmd == "rescore":
            print(f"{score.rescore_all(conn)} company scores changed")
        elif a.cmd == "learn-stats":
            week = a.week or learn.monday(date.today()) - timedelta(days=7)
            st = learn.stats(conn, week)
            st["proposals"] = learn.propose_weights(conn, st)
            with open(a.out, "w") as f:
                json.dump(st, f, indent=2, default=str)
            print(f"week {week}: {st['week_sends']['sent']} sent, {len(st['proposals'])} weight proposals -> {a.out}")
        elif a.cmd == "learn-import":
            with open(a.stats) as f:
                st = json.load(f)
            with open(a.file) as f:
                raw = f.read()
            data = json.loads(raw) if raw.lstrip().startswith("{") else extract_json(raw, key="learnings")
            print(json.dumps(learn.import_learnings(conn, st, data, st.get("proposals") or []), indent=2))
        elif a.cmd == "stats":
            with conn.cursor() as cur:
                for q in ("select source, stage, count(*) n from gtm.companies group by 1, 2 order by 1, 2",
                          "select role, count(*) n, count(email) with_email from gtm.people group by 1 order by 2 desc",
                          "select count(*) open_conflicts from gtm.conflicts where status = 'open'"):
                    cur.execute(q)
                    for r in cur.fetchall():
                        print(dict(r))
        elif a.cmd == "conflicts":
            with conn.cursor() as cur:
                cur.execute("""select id, entity, entity_id, field, current_value, proposed_value, source
                               from gtm.conflicts where status = 'open' order by id""")
                for r in cur.fetchall():
                    print(dict(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
