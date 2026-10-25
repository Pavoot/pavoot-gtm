# Pavoot GTM agent: plan (v1)

Status (2026-10-24): built and tested locally, not live. Everything in sections 1-9 exists:
schema (db/001-003), prospecting, planner (Thompson sampling + holdout, bounce circuit breaker),
drafting, the #gtm cards + Gmail drafts/sends + tracker (../pavoot-ops/src/ops/gtm.py), and the
weekly learning loop, open tracking (1x1 image, machine opens kept apart), LinkedIn analytics exports and
LinkedIn notification emails read automatically, and A/B tests proposed weekly and started by a click.
Setup steps: README.md.
Not built: email address verification (the bounce circuit breaker covers it), sending LinkedIn messages
(no API; ToS risk), warm-lead entry form (warm leads: set people.is_warm).

## Goal
Find prospects (YC and non-YC), reach several people per company across email + LinkedIn,
A/B test what works, and book discovery calls. A human approves everything.

## Decisions taken (Ana, 2026-10-24)
- Channels: all. Email is sent through Gmail; LinkedIn and others are drafted, sent by hand and logged.
- Senders: Ana and Gohar, both A/B tested. Gohar connects her mailbox herself by DMing the bot
  `connect google gohar@pavoot.com` (this already exists in pavoot-ops).
- Sending mode: A = the bot creates a Gmail draft in the sender's mailbox and the founder presses Send.
  If a founder tells the bot to send ("send" button / "send all approved"), the bot sends
  (Gmail drafts.send; the existing gmail.compose scope already allows it, so no reconnect).
  This changes the bot's rule in src/ops/google.py from "one write: create_draft" to
  "create_draft + send_draft, send only on a founder's explicit click" (owner-approved 2026-10-24).
- CRM: Pavoot's own sales database: its own small RDS Postgres in AWS (pavoot-ops stack, paid from AWS
  credits), apart from the app's Supabase; the agent login (`gtm_agent`) can only use the `gtm` schema.
  (Briefly created in Supabase production first, then removed: db/undo_001_002.sql, 2026-10-24.) Repo: ~/Documents/CODEBASE/pavoot-gtm.
- Runtime: Ana's Mac for the thinking (prospecting, drafting); pavoot-ops for mailbox work.
- Volume: about 20 new people a day, follow-ups on days 1 / 2 / 3 / 7.
- Warm leads: start empty, with a way to add them later (TODO).
- Cold prospects: YC and non-YC.

## Architecture
```
Ana's Mac (launchd 08:30, claude -p)          pavoot-ops (AWS, Slack bot)
  prospect -> score -> pick 3 roles/company      #gtm cards: [Create draft] [Send] [Skip] [Edit]
  write drafts per variant                       Gmail: create_draft / send_draft
  weekly A/B report                              tracker: sends, bounces, replies, opens, meetings
            \                                   /
             -----> GTM CRM (RDS Postgres, AWS) <-
```

### 1. GTM CRM (new, separate database)
Tables: companies (stage, signals, sponsor_cluster, source: yc|non_yc|referral|warm),
people (role: CEO|CMO|CPO|Field Marketing|GTM/Growth|Head of Events, email, email_verified,
linkedin), sequences, touches (channel, sender, variant, step, scheduled_for, status:
drafted|approved|sent|bounced|replied, gmail ids), replies (class), meetings, experiments,
suppression (unsubscribes, bounces, do-not-contact).
Imports are additive; any conflict with existing data waits for Ana's decision.

### 2. Prospecting (Mac)
- YC: the scraped YC directory (read-only): active, 15-300 people, batch 2015-2024, located in
  SF / NYC (~509 companies). Founders come with it; marketing / events people via web sourcing.
  (The app's global CRM is deliberately not readable by gtm_agent.)
- Non-YC: Series A/B companies via Pavoot sourcing (Apollo / PDL / Crustdata) + web search.
- Referral partners: GTM agencies (starting with Arooba) as their own list.
- Signals, scored: new CMO / VP Marketing (highest), recent Series A/B, hiring field marketing
  or events roles, hosting or sponsoring events (Luma, conferences), product launch.
- Up to 3 people per company from the role list, each with its own angle:
  CEO = cost of an events hire, CMO / GTM = pipeline and attribution,
  field marketing / events = time saved, CPO = community and product feedback.
  Staggered days; any reply at a company pauses its other threads.
- Verify every email before it is used (target bounces under 2%).

### 3. Drafting (Mac)
Reads outreach/skills/ (pitch, style guide: no dashes), learnings.md (section 9), plus the call's
pitch lines (big companies spend six figures on a single event; "the ones that do the job vs throwing
events blindly"; budget + audience -> topics + guest list -> approve -> show up). Pilot event ->
contract for the next 5. Every email: plain text, unsubscribe line, physical address.
Disclosure rules (Ana, 2026-10-24; same as the content agent's pavoot-today.md section 7):
- Never name customers (AWS for Startups, Fondo); describe the type of company instead.
- Stripe / Deel are never presented as customers.
- No client counts. Event numbers (e.g. "~100 events", show-up rates) only when genuinely
  impressive for the point, and Ana approves the phrasing once.
- Prices, MRR, runway, round are internal: no prices in outreach copy.

### 4. Approval and sending (pavoot-ops, Slack #gtm)
One morning post with the day's cards. Create draft (default) / Send / Edit / Skip.
LinkedIn cards: copy text + "Mark sent".

### 5. Measurement (pavoot-ops tracker, runs hourly)
- Sent: the message appears in the sender's Sent folder (catches manual sends from a draft too).
- Bounced: mailer-daemon replies in that thread -> suppress address.
- Replied: new inbound message in the thread; the LLM classifies it as interested / not now /
  wrong person (+ referral) / unsubscribe / out of office. Unsubscribe -> suppression;
  not now -> remind in 90 days; interested -> Slack ping to the sender.
- Meeting: a calendar event (calendar.readonly) with the prospect's email as attendee, or
  the reply mentions a booked time. Counted per variant.
- Opens: optional 1x1 pixel served by pavoot-ops. Off by default on first emails (it hurts
  deliverability and Apple Mail pre-opens make it inflated); shown as a secondary number only.
- Deliverability: bounce rate, spam-complaint signals, Google Postmaster Tools for the domain
  (meaningful only at higher volume), SPF / DKIM / DMARC check at setup.

### 6. A/B testing
- One variable per experiment, written down before it starts (hypothesis + win condition).
- Variables, big swings first: sender (Ana vs Gohar), opening angle, ask ("worth a look?"
  vs meeting request), channel order (LinkedIn first vs email first), then subject lines.
- Assigned randomly per person, fixed for the whole sequence.
- Main metric: positive reply rate, then meetings booked. Opens are secondary.
- Weekly Slack report (Monday): per variant sent / bounced / replied / positive / meetings,
  labelled "leaning" until the sample is big enough (at 20/day, about 430 per arm to see 5% -> 10%).

### 7. Marketplace: pavoot.events
What it is (site, 2026-10-24): "Events worth being in". Roles: Attend, Host or sponsor, Speak,
Photograph. Ideas ("not happening yet") collect free, non-binding pledges; once there is enough
interest + a host + a sponsor, Pavoot sets date and place and curates who's in the room.
Also: "Ask for an event" (/ask), personalized search (/find), "Notify me" email alerts.

GTM uses:
- Companies (same prospects as above): the A/B test of the ask gets a lighter option.
  "We run your event" vs "co-sponsor an idea on pavoot.events" (cost shared with non-competing
  companies) vs "pitch your own idea". No prices in the copy (internal); pricing comes up on the call.
- Speakers: invite founders / operators to speak at listed ideas. Speaking invites get replies,
  speakers bring their audience, and many are prospects themselves. Own small sequence.
- Photographers: local photographers for the supply side (set their own price). Own small list.
- Attendees: driven by content (content agent), the weekly "best events this week" list,
  and every company / speaker sharing their event page. Goal metric: Notify-me sign-ups + pledges.
- Every link to pavoot.events carries utm_source / utm_campaign (experiment) / utm_content (variant)
  so pledges and sign-ups count per variant. TODO: confirm the site records UTMs.

### 8. Branding
- Every Pavoot event: "Run with Pavoot" on Luma pages and invites; ask clients to post and tag;
  case studies with logos.
- Publish benchmarks from our events ("State of startup events"): citable, feeds AI search answers.
  Numbers and phrasing approved by Ana first (disclosure rules, section 3).
- Reddit: lives in the content agent (other session). Anonymous account is fine; replies are mostly
  pure help; when Pavoot / pavoot.events is mentioned, the comment discloses the connection
  ("I work on Pavoot"). No undisclosed recommendations (Reddit rules + FTC; risk of domain ban).
- GTM agent tracks brand signals: "how did you hear about us", Pavoot mentions on Reddit / X,
  branded search, direct sign-ups; included in the weekly report.

### 9. Learning loop (the agent gets better every week)
Weekly run, Monday on Ana's Mac, after the A/B report:
1. Inputs: variant_stats, every reply with its class, meetings, bounces, unsubscribes, the text of
   winning and losing messages, which signals / roles / company sizes / cities led to replies and
   meetings, pavoot.events sign-ups and pledges per UTM, content signals (below), this week's trends.
2. Output `learnings.md` (in this repo, versioned): a short playbook where every line has evidence
   and a sample size, e.g. "CEO time-cost angle: 3x positive replies vs attribution angle with
   founders (n=140)". The daily drafting prompt reads it.
3. Traffic shifting (Thompson sampling): each day's sends are split by the current probability that
   each variant is best on positive replies, so winners get more volume early without waiting
   ~2 months for significance. 10% always gets the baseline (holdout) so gains are measured
   against something real. A variant that is clearly behind is retired; the next challenger
   changes one variable.
4. Prospecting weights: score.WEIGHTS adjusted from which signals actually convert (bounded: each
   weight moves at most +/-30% per week; stored in the DB with history).
5. Content -> outreach: a prospect who liked / commented on a founder post, viewed a profile, or
   pledged on pavoot.events gets an `engaged_with_content` signal (high weight) and a warm opener.

What can be measured:
- Automatic: replies (classified), positive replies, meetings, bounces, unsubscribes (Gmail +
  Calendar via pavoot-ops); pavoot.events sign-ups / pledges per UTM (once the site records UTMs);
  trends (web search).
- Opens: only with the pixel (off by default; unreliable because of Apple Mail pre-opens).
- LinkedIn post impressions / likes: no API for personal profiles -> Ana drops the weekly analytics
  export in Slack (or the content agent asks for the numbers).
- LinkedIn profile views / who engaged: no API; scraping breaks LinkedIn's terms and risks the
  account -> quick manual note in Slack ("Jane from Acme liked my post") becomes a signal.
- Content agent and GTM agent share one table (gtm.content_metrics + gtm.brand_signals) so post
  performance and outreach results inform each other.

Guardrails:
- No conclusions from small samples: results are "leaning" until the sample is big enough.
- Learnings and small traffic shifts apply automatically; new angles, retiring a variant early, and
  scoring weight changes are proposed in Slack and apply only after Ana's ✅.
- Disclosure rules (section 3) always win over anything the loop learns.

Schema additions (when built): gtm.learnings (week, claim, evidence, n, status), gtm.score_weights
(kind, weight, valid_from, approved_by), gtm.content_metrics (post_url, author, posted_at,
impressions, reactions, comments, profile_views, source: export|manual), experiments.holdout_pct.

## Out of scope for v1 / TODO
- [ ] Warm leads: add by hand later (form or Slack "add lead ..."). Include Arooba's launch-event client.
- [ ] Second sending domain (e.g. trypavoot.com) before scaling past ~30/day per mailbox.
- [ ] Ask each new client for their customer list (lookalikes + sponsor clusters).
- [ ] Luma watcher: flag target companies hosting events in Ana's city (show up in person).
- [ ] Ask Arooba: referral/affiliate terms, and the agent tool with "$5 free credit".
- [ ] Move the Mac part to a Mac mini once it works.

## Rules
- pavoot-ops: never commit / push / deploy from Claude; hand Ana the commit message.
  Don't touch the uncommitted YC-sync work or the content agent (src/ops/content.py).
- Nothing is sent without a founder's click.

## Sources
metaflow.life/blog/claude-code-loop-for-outbound-cold-email · syncgtm.com/blog/claude-code-cold-email-automation ·
apollo.io/insights/how-do-i-run-an-outbound-ab-test-across-subject-lines-messaging-and-call-to-actions ·
pravinkumar.co/blog/how-to-sequence-outbound-to-a-buying-committee-of-five-2026 ·
dupple.com/learn/cold-email-deliverability-2026 · getboomerang.ai/glossaries/7-sins-cold-outbound-committee-buying-era
