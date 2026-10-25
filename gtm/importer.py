"""Import a prospecting run (JSON, shape in prompts/prospect.md) into the GTM CRM.

Additive only: new rows are inserted, empty fields are filled, and a value that differs from
what is stored becomes an open row in gtm.conflicts for Ana to decide. Nothing is overwritten.
Signals are merged (union). Suppressed emails are skipped. One short transaction per company.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from psycopg.types.json import Jsonb

from . import score as scoring
from .roles import ROLES, role_for
from .yc import domain_of

COMPANY_FIELDS = ("name", "linkedin_url", "kind", "hq_city", "employees", "funding_stage",
                  "last_funding_at", "sponsor_cluster", "global_crm_ref")
PERSON_FIELDS = ("full_name", "title", "email", "linkedin_url", "x_url", "city", "company_id")
SOURCES = {"yc", "non_yc", "referral", "warm", "manual"}
KINDS = {"prospect", "referral_partner", "customer"}
EMAIL_STATUSES = {"unknown", "valid", "risky", "invalid"}


@dataclass
class Summary:
    companies_new: int = 0
    companies_updated: int = 0
    people_new: int = 0
    people_updated: int = 0
    suppressed: int = 0
    conflicts: int = 0
    errors: list = field(default_factory=list)


def norm_linkedin(u: str | None) -> str | None:
    if not u or "linkedin.com" not in u:
        return None                      # bookface redirect links etc. are not usable
    p = urlparse(u if "//" in u else "https://" + u)
    path = re.sub(r"/+$", "", p.path)
    return f"https://www.linkedin.com{path}".lower() if path else None


def norm_email(e: str | None) -> str | None:
    e = (e or "").strip().lower()
    return e if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", e) else None


def _differs(old, new) -> bool:
    if isinstance(old, str) and isinstance(new, str):
        return old.strip().lower() != new.strip().lower()
    return str(old) != str(new)


def _merge(cur, table: str, entity: str, row: dict, incoming: dict, fields, source: str, s: Summary) -> bool:
    """Fill empty fields; record conflicts for differing ones. Returns True if anything was filled."""
    fills = {}
    for f in fields:
        new = incoming.get(f)
        if new in (None, ""):
            continue
        old = row.get(f)
        if old in (None, ""):
            fills[f] = new
        elif _differs(old, new):
            cur.execute("""insert into gtm.conflicts (entity, entity_id, field, current_value, proposed_value, source)
                           values (%s, %s, %s, %s, %s, %s) on conflict do nothing""",
                        (entity, row["id"], f, str(old), str(new), source))
            s.conflicts += cur.rowcount
    if fills:
        sets = ", ".join(f"{f} = %s" for f in fills)
        cur.execute(f"update gtm.{table} set {sets}, updated_at = now() where id = %s",
                    (*fills.values(), row["id"]))
    return bool(fills)


def _merge_signals(old: list, new: list) -> list:
    seen = {(x.get("type"), x.get("url") or x.get("detail")) for x in old}
    out = list(old)
    for x in new:
        key = (x.get("type"), x.get("url") or x.get("detail"))
        if x.get("type") and key not in seen:
            out.append(x)
            seen.add(key)
    return out


def _company(cur, c: dict, run_source: str, s: Summary) -> int:
    src = c.get("source")
    if src not in SOURCES:
        raise ValueError(f"source must be one of {sorted(SOURCES)}, got {src!r}")
    kind = c.get("kind") or "prospect"
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}, got {kind!r}")
    data = {**c, "kind": kind, "domain": domain_of(c.get("domain")), "linkedin_url": c.get("linkedin_url") or None}
    if data["domain"]:
        cur.execute("select * from gtm.companies where lower(domain) = %s", (data["domain"],))
    else:
        cur.execute("select * from gtm.companies where domain is null and lower(name) = lower(%s)", (c["name"],))
    row = cur.fetchone()
    signals = [x for x in c.get("signals") or [] if isinstance(x, dict)]
    if row is None:
        data["signals"] = signals
        cur.execute("""insert into gtm.companies (name, domain, linkedin_url, kind, source, hq_city, employees,
                           funding_stage, last_funding_at, signals, score, sponsor_cluster, global_crm_ref, notes)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                    (c["name"], data["domain"], data["linkedin_url"], kind, src, c.get("hq_city"), c.get("employees"),
                     c.get("funding_stage"), c.get("last_funding_at"), Jsonb(signals), scoring.score(data),
                     c.get("sponsor_cluster"), c.get("global_crm_ref"), c.get("notes")))
        s.companies_new += 1
        return cur.fetchone()["id"]
    changed = _merge(cur, "companies", "company", row, data, COMPANY_FIELDS, run_source, s)
    merged = _merge_signals(row["signals"] or [], signals)
    if not row.get("notes") and c.get("notes"):
        cur.execute("update gtm.companies set notes = %s where id = %s", (c["notes"], row["id"]))
        changed = True
    view = {**row, **{k: v for k, v in data.items() if row.get(k) in (None, "")}, "signals": merged}
    cur.execute("update gtm.companies set signals = %s, score = %s where id = %s",
                (Jsonb(merged), scoring.score(view), row["id"]))
    if changed or len(merged) != len(row["signals"] or []):
        s.companies_updated += 1
    return row["id"]


def _person(cur, p: dict, company_id: int, run_source: str, s: Summary) -> None:
    email = norm_email(p.get("email"))
    if email:
        cur.execute("select 1 from gtm.suppression where email = %s", (email,))
        if cur.fetchone():
            s.suppressed += 1
            return
    status = p.get("email_status") or "unknown"
    if status not in EMAIL_STATUSES:
        status = "unknown"
    role = p.get("role") if p.get("role") in ROLES else role_for(p.get("title"))
    data = {**p, "email": email, "linkedin_url": norm_linkedin(p.get("linkedin_url")), "company_id": company_id}
    row = None
    if email:
        cur.execute("select * from gtm.people where lower(email) = %s", (email,))
        row = cur.fetchone()
    if row is None and data["linkedin_url"]:
        cur.execute("select * from gtm.people where linkedin_url = %s", (data["linkedin_url"],))
        row = cur.fetchone()
    if row is None:  # last resort: same name at the same company
        cur.execute("select * from gtm.people where company_id = %s and lower(full_name) = lower(%s)",
                    (company_id, p["full_name"]))
        row = cur.fetchone()
    if row is None:
        cur.execute("""insert into gtm.people (company_id, full_name, title, role, email, email_status, linkedin_url,
                           x_url, city, source, source_ref, notes)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (company_id, p["full_name"], p.get("title"), role, email, status if email else "unknown",
                     data["linkedin_url"], p.get("x_url"), p.get("city"), p.get("source"), p.get("source_ref"),
                     p.get("notes")))
        s.people_new += 1
        return
    filled = _merge(cur, "people", "person", row, data, PERSON_FIELDS, run_source, s)
    if row.get("role") == "Other" and role != "Other" and not row.get("title"):
        cur.execute("update gtm.people set role = %s where id = %s", (role, row["id"]))
        filled = True
    if filled:
        s.people_updated += 1


def import_run(conn, run: dict, run_source: str = "prospecting") -> Summary:
    s = Summary()
    for i, c in enumerate(run.get("companies") or []):
        part = Summary()  # counted only if this company's transaction commits
        try:
            if not c.get("name"):
                raise ValueError("company without a name")
            with conn.transaction(), conn.cursor() as cur:
                cid = _company(cur, c, run_source, part)
                for p in c.get("people") or []:
                    if p.get("full_name"):
                        _person(cur, p, cid, run_source, part)
        except Exception as e:  # one bad company never blocks the rest
            s.errors.append(f"company #{i} {c.get('name')!r}: {e}")
            continue
        for k in ("companies_new", "companies_updated", "people_new", "people_updated", "suppressed", "conflicts"):
            setattr(s, k, getattr(s, k) + getattr(part, k))
    return s


def import_file(conn, path: str) -> Summary:
    with open(path) as f:
        return import_run(conn, json.load(f), run_source=f"prospecting:{path.rsplit('/', 1)[-1]}")
