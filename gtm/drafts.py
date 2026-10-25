"""Drafting: export today's due touches with context for the headless writer, then lint and
store what it wrote. The writer (prompts/draft.md) has no write access; code adds the footer,
thread subject and tracking parameters, and refuses anything that breaks the disclosure rules.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from . import planner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_FILE = os.environ.get("GTM_SETTINGS", os.path.join(ROOT, "config", "outreach.json"))
LINKEDIN_NOTE_MAX = 300          # LinkedIn connection-request note limit

# Disclosure rules (pavoot-content/guidelines/pavoot-today.md section 7). A hit blocks the draft.
BLOCK = [
    (r"\b(aws|amazon web services|fondo|stripe|deel|airwallex|fixture|lightfield)\b", "names a customer / partner"),
    (r"[$€£]\s?\d|\b\d+\s?k\b|\busd\b|\bper event\b.*\d", "mentions a price"),
    (r"\b\d+\+?\s+(clients|customers|companies use)\b", "states a client count"),
    (r"\bmrr\b|\brunway\b|\bvaluation\b", "mentions internal numbers"),
    (r"\b(webinar|virtual event|zoom event|online event)\b", "suggests a virtual format"),
]
WARN = [
    (r"[—–]|\s-\s", "contains a dash (style guide: no dashes)"),
    (r"\bwould love to\b|\bi wanted to reach out\b|\bi'm curious\b|\bjust checking in\b", "uses a banned phrase"),
    (r"\bi hope this (email|message) finds you\b", "uses a banned phrase"),
]


def settings(path: str = SETTINGS_FILE) -> dict:
    with open(path) as f:
        return json.load(f)


def lint(text: str, marketplace_live: bool) -> tuple[list[str], list[str]]:
    low = (text or "").lower()
    blocks = [why for pat, why in BLOCK if re.search(pat, low)]
    if not marketplace_live and "pavoot.events" in low:
        blocks.append("mentions pavoot.events before it is live")
    warns = [why for pat, why in WARN if re.search(pat, text or "", re.I)]
    return blocks, warns


def tag_links(text: str, utm: dict | None) -> str:
    """Add utm_source / utm_campaign / utm_content to pavoot.events and pavoot.com links."""
    if not utm:
        return text

    def fix(m):
        url = m.group(0)
        trail = ""
        while url and url[-1] in ".,;:!?)":
            trail, url = url[-1] + trail, url[:-1]
        p = urlparse(url)
        if not (p.hostname or "").endswith(("pavoot.events", "pavoot.com")):
            return m.group(0)
        q = dict(parse_qsl(p.query))
        q.update({f"utm_{k}": str(v) for k, v in utm.items() if v})
        return urlunparse(p._replace(query=urlencode(q))) + trail
    return re.sub(r"https?://[^\s<>\"]+", fix, text)


def _first_name(full: str) -> str:
    return (full or "").split()[0] if full else ""


def export_due(conn, today: date | None = None) -> dict:
    """Context for each due touch. Mail content from prospects is never included (only our own
    previous messages), so the writer can't be steered by text someone else wrote."""
    ids = planner.due_touches(conn, today)
    out = []
    with conn.cursor() as cur:
        cur.execute("select claim, evidence, n, status from gtm.learnings where status in ('active', 'leaning') "
                    "order by status, id desc limit 30")
        learnings = cur.fetchall()
        for tid in ids:
            cur.execute("""select t.id, t.step, t.channel, s.id seq, s.kind, s.sender, s.variant, s.experiment_id,
                                  p.full_name, p.title, p.role, p.city, c.name company, c.notes company_notes,
                                  c.signals, c.hq_city, c.employees, c.funding_stage,
                                  e.variable exp_variable, e.variants exp_variants
                           from gtm.touches t join gtm.sequences s on s.id = t.sequence_id
                           join gtm.people p on p.id = s.person_id
                           left join gtm.companies c on c.id = p.company_id
                           left join gtm.experiments e on e.id = s.experiment_id where t.id = %s""", (tid,))
            r = cur.fetchone()
            cur.execute("""select step, channel, subject, body, status from gtm.touches
                           where sequence_id = %s and step < %s and status in ('sent', 'gmail_draft', 'drafted')
                           order by step""", (r["seq"], r["step"]))
            previous = cur.fetchall()
            variant = None
            if r["exp_variable"] and r["variant"]:
                desc = next((v.get("description") for v in r["exp_variants"] if v["key"] == r["variant"]), None)
                variant = {"variable": r["exp_variable"], "key": r["variant"], "description": desc}
            out.append({
                "touch_id": r["id"], "step": r["step"], "channel": r["channel"], "sequence_kind": r["kind"],
                "sender": r["sender"], "sign_off": _first_name(r["sender"].split("@")[0].title()),
                "person": {"first_name": _first_name(r["full_name"]), "full_name": r["full_name"],
                           "title": r["title"], "role": r["role"], "city": r["city"]},
                "company": {"name": r["company"], "notes": r["company_notes"], "signals": r["signals"],
                            "city": r["hq_city"], "employees": r["employees"], "funding_stage": r["funding_stage"]},
                "variant": variant,
                "previous_messages": [dict(m) for m in previous],
            })
    return {"date": str(today or date.today()), "learnings": [dict(x) for x in learnings], "touches": out}


def import_drafts(conn, data: dict, cfg: dict | None = None) -> dict:
    """Store drafts: {"drafts": [{"touch_id", "subject", "body"}]}. Blocked drafts stay planned
    (re-drafted next run) and are reported; warnings travel to the Slack card."""
    cfg = cfg or settings()
    footer = (cfg.get("email_footer") or "").strip()
    footer_ok = bool(footer) and "TODO" not in footer
    live = bool(cfg.get("marketplace_live"))
    report = {"drafted": 0, "blocked": [], "skipped": []}
    for d in data.get("drafts") or []:
        tid, body = d.get("touch_id"), (d.get("body") or "").strip()
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("""select t.*, s.status seq_status from gtm.touches t join gtm.sequences s on s.id = t.sequence_id
                           where t.id = %s for update of t""", (tid,))
            t = cur.fetchone()
            if not t or t["status"] != "planned" or t["seq_status"] != "active":
                report["skipped"].append(f"touch {tid}: not a planned touch of an active sequence")
                continue
            subject = (d.get("subject") or "").strip()
            if t["channel"] == "email" and t["step"] > 1:
                cur.execute("select subject from gtm.touches where sequence_id = %s and channel = 'email' "
                            "and step < %s and subject is not null order by step limit 1", (t["sequence_id"], t["step"]))
                first = cur.fetchone()
                if first:   # follow-ups reply in the first email's thread
                    subject = first["subject"] if first["subject"].lower().startswith("re:") else "Re: " + first["subject"]
            blocks, warns = lint(subject + "\n" + body, live)
            if not body:
                blocks.append("empty body")
            if t["channel"] == "email" and not subject:
                blocks.append("email without a subject")
            if t["channel"] == "email" and not footer_ok:
                blocks.append("config/outreach.json email_footer is not filled in (physical address + opt-out line)")
            if t["channel"] == "linkedin" and t["step"] == 1 and len(body) > LINKEDIN_NOTE_MAX:
                warns.append(f"longer than LinkedIn's {LINKEDIN_NOTE_MAX}-character connection note")
            if blocks:
                report["blocked"].append(f"touch {tid}: " + "; ".join(blocks))
                continue
            body = tag_links(body, t["utm"])
            if t["channel"] == "email":
                body = body + "\n\n" + footer
            cur.execute("""update gtm.touches set subject = %s, body = %s, warnings = %s, status = 'drafted',
                                  drafted_at = now(), updated_at = now() where id = %s""",
                        (subject if t["channel"] == "email" else None, body, warns, tid))
            report["drafted"] += 1
    return report
