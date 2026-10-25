"""Job title -> buying role, and which (at most 3) people to contact at a company."""

from __future__ import annotations

import re

ROLES = ("CEO", "CMO", "CPO", "Field Marketing", "GTM/Growth", "Head of Events",
         "Speaker", "Photographer", "Other")

# Order matters: the first rule that matches wins ("Field Marketing Manager" is not CMO,
# "Head of Events & Community" is not GTM).
_RULES = [
    ("Field Marketing", r"\bfield marketing\b|\babm\b|account[- ]based"),
    ("Head of Events", r"\bevents?\b|\bexperiential\b|\bcommunity\b"),
    ("CEO", r"\bceo\b|chief executive|\bpresident\b"),
    ("CMO", r"\bcmo\b|chief marketing|\b(vp|vice president|head|director)\b.*\bmarketing\b|\bmarketing (lead|director)\b"),
    ("CPO", r"\bcpo\b|chief product|\b(vp|vice president|head|director)\b.*\bproduct\b"),
    ("GTM/Growth", r"\bgtm\b|go[- ]to[- ]market|\bgrowth\b|\bcro\b|chief revenue|\brevenue\b|"
                   r"\b(vp|vice president|head)\b.*\b(sales|partnerships|demand)\b|demand gen"),
    # Technical / operations co-founders are not the buyer for events.
    ("Other", r"\bcto\b|\bcoo\b|chief (technology|technical|operating|scientific)|\bengineer|\bscientist\b"),
    # A founder without a C-title is the economic buyer at a startup.
    ("CEO", r"\b(co-?)?founder\b"),
]


def role_for(title: str | None) -> str:
    t = (title or "").lower()
    for role, pattern in _RULES:
        if re.search(pattern, t):
            return role
    return "Other"


def contact_order(team_size: int | None) -> list[str]:
    """Who to reach first. Small teams: the founder decides. Bigger teams: marketing owns events."""
    if team_size is not None and team_size < 50:
        return ["CEO", "GTM/Growth", "CMO", "Head of Events", "Field Marketing", "CPO"]
    return ["CMO", "Field Marketing", "Head of Events", "GTM/Growth", "CEO", "CPO"]


def pick_contacts(people: list[dict], team_size: int | None, limit: int = 3) -> list[dict]:
    """Up to `limit` people, one per role, in contact_order. People without any way to reach
    them (no email and no LinkedIn) or with an invalid email and no LinkedIn are skipped."""
    reachable = [p for p in people
                 if p.get("linkedin_url") or (p.get("email") and p.get("email_status") != "invalid")]
    chosen, seen = [], set()
    for role in contact_order(team_size):
        for p in reachable:
            if p.get("role") == role and role not in seen:
                chosen.append(p)
                seen.add(role)
                break
        if len(chosen) == limit:
            break
    return chosen
