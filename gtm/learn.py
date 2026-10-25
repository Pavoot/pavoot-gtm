"""Weekly learning loop (Monday, Ana's Mac).

1. stats(): what converted, per experiment variant, signal, role, size, city, sender, first channel,
   plus content metrics and brand signals. Each variant gets P(best) from Beta posteriors.
2. propose_weights(): scoring weights nudged toward signals that convert (at most +/-30% a week,
   only with enough data). Saved as 'proposed'; they apply only after Ana's ✅ in Slack.
3. The headless learning run (prompts/learn.md) turns stats into learnings with evidence; import
   them with import_learnings(), which also writes learnings.md and the weekly report the bot posts.
Traffic shifting itself happens every day in planner.choose_variant (Thompson sampling).
"""

from __future__ import annotations

import json
import os
import random
from datetime import date, timedelta

from psycopg.types.json import Jsonb

from .score import WEIGHTS, current_weights

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_N_WEIGHT = 30          # contacted companies with a signal before its weight may move
MAX_STEP = 0.30            # max relative weight change per week
CLEAR = 0.95               # P(best) for "clear winner"
MIN_ARM = 100              # people per arm before a winner can be called clear


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def p_best(arms: dict, draws: int = 20000, seed: int = 7) -> dict:
    """{variant: (people, positive)} -> {variant: probability it has the best positive-reply rate}."""
    if not arms:
        return {}
    rng = random.Random(seed)
    wins = {k: 0 for k in arms}
    for _ in range(draws):
        best = max(arms, key=lambda k: rng.betavariate(1 + arms[k][1], 1 + max(0, arms[k][0] - arms[k][1])))
        wins[best] += 1
    return {k: round(v / draws, 3) for k, v in wins.items()}


def _rate(n, k):
    return round(k / n, 3) if n else None


def _breakdown(cur, expr: str, label: str) -> list[dict]:
    """Conversion of contacted people grouped by `expr` (SQL over p, c, s)."""
    cur.execute(f"""
        with contacted as (
            select distinct on (s.person_id) s.person_id, s.sender, s.created_at, p.role, c.employees, c.hq_city, c.signals,
                   (select channel from gtm.touches t where t.sequence_id = s.id order by step limit 1) first_channel
            from gtm.sequences s join gtm.people p on p.id = s.person_id left join gtm.companies c on c.id = p.company_id
            where exists (select 1 from gtm.touches t where t.sequence_id = s.id and t.status in ('sent', 'bounced'))
            order by s.person_id, s.created_at)
        select {expr} as grp, count(*) n,
               count(*) filter (where exists (select 1 from gtm.replies r where r.person_id = x.person_id
                                              and r.received_at >= x.created_at and r.class <> 'out_of_office')) replied,
               count(*) filter (where exists (select 1 from gtm.replies r where r.person_id = x.person_id
                                              and r.received_at >= x.created_at and r.class = 'interested')) positive,
               count(*) filter (where exists (select 1 from gtm.meetings m where m.person_id = x.person_id
                                              and m.created_at >= x.created_at)) meetings
        from contacted x {"cross join lateral jsonb_array_elements(coalesce(x.signals, '[]')) sig" if label == "signal" else ""}
        group by 1 order by 2 desc""")
    return [{label: r["grp"], "n": r["n"], "replied": r["replied"], "positive": r["positive"], "meetings": r["meetings"],
             "positive_rate": _rate(r["n"], r["positive"])} for r in cur.fetchall()]


def stats(conn, week: date) -> dict:
    with conn.cursor() as cur:
        cur.execute("select * from gtm.variant_stats order by experiment, variant")
        variants = [dict(r) for r in cur.fetchall()]
        cur.execute("select * from gtm.experiments where status = 'running'")
        running = cur.fetchall()
        experiments = []
        for e in running:
            arms = {v["variant"]: (v["people"], v["positive"]) for v in variants if v["experiment"] == e["name"]}
            opens = {v["variant"]: (v["sent"], v["opened"]) for v in variants if v["experiment"] == e["name"]}
            pb = p_best(arms)
            leader = max(pb, key=pb.get) if pb else None
            enough = bool(arms) and all(n >= MIN_ARM for n, _ in arms.values())
            experiments.append({"name": e["name"], "variable": e["variable"], "hypothesis": e["hypothesis"],
                                "win_condition": e["win_condition"], "arms": [
                                    {"variant": k, "people": n, "positive": p, "positive_rate": _rate(n, p),
                                     "p_best": pb.get(k), "sent": opens[k][0], "opened": opens[k][1],
                                     "open_rate_approx": _rate(opens[k][0], opens[k][1])}
                                    for k, (n, p) in sorted(arms.items())],
                                "verdict": ("clear" if leader and enough and pb[leader] >= CLEAR else "leaning"),
                                "leader": leader})
        start, end = week, week + timedelta(days=7)
        cur.execute("""select count(*) filter (where status in ('sent', 'bounced')) sent,
                              count(*) filter (where status = 'bounced') bounced,
                              count(*) filter (where track_opens and status = 'sent') tracked,
                              count(*) filter (where track_opens and first_open_at is not null) opened
                       from gtm.touches where sent_at >= %s and sent_at < %s""", (start, end))
        week_sends = dict(cur.fetchone())
        cur.execute("""select class, count(*) n from gtm.replies where received_at >= %s and received_at < %s
                       group by 1""", (start, end))
        week_replies = {r["class"]: r["n"] for r in cur.fetchall()}
        cur.execute("select count(*) n from gtm.meetings where created_at >= %s and created_at < %s", (start, end))
        week_meetings = cur.fetchone()["n"]
        breakdowns = {
            "signal": _breakdown(cur, "sig->>'type'", "signal"),
            "role": _breakdown(cur, "x.role", "role"),
            "size": _breakdown(cur, "case when x.employees < 50 then '<50' when x.employees <= 150 then '50-150' "
                                    "when x.employees is not null then '>150' else 'unknown' end", "size"),
            "city": _breakdown(cur, "split_part(coalesce(x.hq_city, 'unknown'), ',', 1)", "city"),
            "sender": _breakdown(cur, "x.sender", "sender"),
            "first_channel": _breakdown(cur, "x.first_channel", "first_channel"),
        }
        cur.execute("""select author, count(*) posts, sum(impressions) impressions, sum(reactions) reactions,
                              sum(comments) comments, sum(profile_views) profile_views
                       from gtm.content_metrics where recorded_at >= %s - interval '28 days' group by 1""", (end,))
        content = [dict(r) for r in cur.fetchall()]
        cur.execute("""select kind, count(*) n from gtm.brand_signals where seen_at >= %s and seen_at < %s group by 1""",
                    (start, end))
        brand = {r["kind"]: r["n"] for r in cur.fetchall()}
        # Our own messages that got a positive reply vs none (never prospect mail): the writer learns from these.
        cur.execute("""select t.step, t.channel, t.subject, left(t.body, 900) body, s.variant,
                              exists (select 1 from gtm.replies r where r.touch_id = t.id and r.class = 'interested') positive
                       from gtm.touches t join gtm.sequences s on s.id = t.sequence_id
                       where t.status = 'sent' and t.sent_at >= %s - interval '28 days' and t.step = 1
                       order by positive desc, t.sent_at desc limit 16""", (end,))
        examples = [dict(r) for r in cur.fetchall()]
        cur.execute("select claim, evidence, n, status from gtm.learnings where status in ('active', 'leaning') order by id")
        previous = [dict(r) for r in cur.fetchall()]
    return {"week": str(week), "week_sends": week_sends, "week_replies": week_replies, "week_meetings": week_meetings,
            "experiments": experiments, "breakdowns": breakdowns, "content": content, "brand": brand,
            "examples": examples, "previous_learnings": previous, "weights": current_weights(conn)}


def propose_weights(conn, st: dict) -> list[dict]:
    """Nudge each signal's weight by its lift in positive-reply rate vs all contacted people,
    bounded to +/-30% and only with MIN_N_WEIGHT contacted people. Saved as proposals."""
    rows = st["breakdowns"]["signal"]
    with conn.cursor() as cur:
        cur.execute("""select count(distinct person_id) n,
                              count(distinct person_id) filter (where exists (select 1 from gtm.replies r
                                    where r.person_id = s.person_id and r.class = 'interested')) pos
                       from gtm.sequences s where exists (select 1 from gtm.touches t where t.sequence_id = s.id
                                                          and t.status in ('sent', 'bounced'))""")
        base = cur.fetchone()
    if not base["n"] or not base["pos"]:
        return []
    base_rate = base["pos"] / base["n"]
    weights = st["weights"]
    out = []
    for r in rows:
        kind = r["signal"]
        if kind not in WEIGHTS or r["n"] < MIN_N_WEIGHT:
            continue
        lift = (r["positive"] / r["n"]) / base_rate
        factor = max(1 - MAX_STEP, min(1 + MAX_STEP, lift))
        new = max(0, min(100, round(weights[kind] * factor)))
        if new == weights[kind]:
            continue
        reason = (f"{kind}: {r['positive']}/{r['n']} positive ({r['positive_rate']:.0%}) vs "
                  f"{base['pos']}/{base['n']} overall ({base_rate:.0%}); weight {weights[kind]} -> {new}")
        with conn.cursor() as cur:
            cur.execute("delete from gtm.score_weights where kind = %s and status = 'proposed'", (kind,))
            cur.execute("insert into gtm.score_weights (kind, weight, reason) values (%s, %s, %s) returning id",
                        (kind, new, reason))
            out.append({"id": cur.fetchone()["id"], "kind": kind, "from": weights[kind], "to": new, "reason": reason})
    return out


def _report(st: dict, learnings: list[dict], proposals: list[dict], proposed: str | None = None) -> str:
    ws, wr = st["week_sends"], st["week_replies"]
    pos = wr.get("interested", 0)
    opened = (f" · opened ~{ws.get('opened', 0)}/{ws.get('tracked', 0)} (approximate)" if ws.get("tracked") else "")
    lines = [f"*GTM week of {st['week']}*",
             f"Sent {ws['sent']} · bounced {ws['bounced']}{opened} · replies {sum(wr.values())} (interested {pos}) · "
             f"meetings {st['week_meetings']}"]
    for e in st["experiments"]:
        arms = ", ".join(f"{a['variant']} {a['positive']}/{a['people']} (P best {a['p_best']:.0%})" for a in e["arms"])
        lines.append(f"Test *{e['name']}* ({e['variable']}): {arms or 'no data yet'}. "
                     + ("*Clear winner: " + e["leader"] + "*" if e["verdict"] == "clear" else "Leaning, keep testing."))
    if learnings:
        lines.append("*Learnings* (used by tomorrow's drafts):")
        lines += [f"• {x['claim']} _(n={x['n']}, {x['status']})_" for x in learnings[:8]]
    if proposed:
        lines.append(f"*Next A/B test proposed: {proposed}* (▶️ Start below).")
    if proposals:
        lines.append("*Scoring changes waiting for your ✅* (buttons below).")
    return "\n".join(lines)


FIXED_KEYS = {"sender": {"ana", "gohar"}, "channel_order": {"email_first", "linkedin_first"}}


def valid_experiment(x) -> str:
    """'' if a proposed experiment can be stored, else why not."""
    if not isinstance(x, dict):
        return "not an object"
    if x.get("variable") not in ("sender", "angle", "ask", "channel_order", "subject"):
        return f"unknown variable {x.get('variable')!r}"
    vs = x.get("variants") or []
    keys = [v.get("key") for v in vs if isinstance(v, dict)]
    if not (2 <= len(vs) <= 3) or len(set(keys)) != len(vs) or not all(keys):
        return "needs 2 or 3 variants with distinct keys"
    if x["variable"] in FIXED_KEYS and set(keys) != FIXED_KEYS[x["variable"]]:
        return f"{x['variable']} variants must be {sorted(FIXED_KEYS[x['variable']])}"
    if not all((v.get("description") or "").strip() for v in vs):
        return "every variant needs a description"
    if not (x.get("name") and x.get("hypothesis") and x.get("win_condition")):
        return "needs name, hypothesis and win_condition"
    from .drafts import lint
    if any(lint(v["description"], False)[0] for v in vs):
        return "a variant breaks the disclosure rules"
    return ""


def _store_experiment(cur, x) -> str:
    """Saved as 'draft': it starts only when a founder clicks ▶️ Start in #gtm. Returns the name."""
    vs = [{"key": v["key"], "description": v["description"].strip(), **({"baseline": True} if v.get("baseline") else {})}
          for v in x["variants"]]
    if not any(v.get("baseline") for v in vs):
        vs[0]["baseline"] = True
    name = x["name"].strip()[:60]
    cur.execute("select count(*) n from gtm.experiments where name = %s or name like %s", (name, name + "-%"))
    if cur.fetchone()["n"]:
        name = f"{name}-{date.today():%m%d}"
    cur.execute("delete from gtm.experiments where status = 'draft'")    # one proposal at a time
    cur.execute("""insert into gtm.experiments (name, variable, hypothesis, win_condition, variants, status)
                   values (%s, %s, %s, %s, %s, 'draft')""",
                (name, x["variable"], x["hypothesis"].strip(), x["win_condition"].strip(), Jsonb(vs)))
    return name


def import_learnings(conn, st: dict, data: dict, proposals: list[dict]) -> dict:
    """Store the run's learnings (claims must carry evidence and n), retire last week's set,
    write learnings.md and the weekly report the bot posts."""
    week = date.fromisoformat(st["week"])
    good, dropped = [], []
    for x in data.get("learnings") or []:
        n = x.get("n")
        if not (x.get("claim") and x.get("evidence") and isinstance(n, int) and n > 0):
            dropped.append(x)
            continue
        status = x.get("status") if x.get("status") in ("active", "leaning") else "leaning"
        if n < 30:
            status = "leaning"          # never a rule from a tiny sample
        good.append({"claim": x["claim"].strip(), "evidence": x["evidence"].strip(), "n": n, "status": status})
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("update gtm.learnings set status = 'retired' where status in ('active', 'leaning') and week < %s", (week,))
        cur.execute("delete from gtm.learnings where week = %s", (week,))   # re-running the same week replaces it
        for x in good:
            cur.execute("insert into gtm.learnings (week, claim, evidence, n, status) values (%s, %s, %s, %s, %s)",
                        (week, x["claim"], x["evidence"], x["n"], x["status"]))
        proposed, why_not = None, ""
        nx = data.get("next_experiment")
        if nx:
            why_not = valid_experiment(nx)
            if not why_not:
                proposed = _store_experiment(cur, nx)
        cur.execute("""insert into gtm.weekly_reports (week, summary, stats) values (%s, %s, %s)
                       on conflict (week) do update set summary = excluded.summary, stats = excluded.stats""",
                    (week, _report(st, good, proposals, proposed), Jsonb(json.loads(json.dumps(
                        {k: v for k, v in st.items() if k != "examples"}, default=str)))))
    with open(os.path.join(ROOT, "learnings.md"), "w") as f:
        f.write(f"# What works (week of {week})\n\nWritten by the weekly learning run; drafting reads the "
                "same list from the database. Disclosure rules always win over a learning.\n\n")
        for x in good:
            f.write(f"- **{x['claim']}** ({x['status']}, n={x['n']}). {x['evidence']}\n")
        if not good:
            f.write("- Nothing yet: not enough sends with outcomes.\n")
    return {"learnings": len(good), "dropped": len(dropped), "proposals": len(proposals),
            "next_experiment": proposed, **({"experiment_rejected": why_not} if why_not else {})}
