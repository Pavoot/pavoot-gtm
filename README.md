# pavoot-gtm

Pavoot's GTM agent. Finds prospects (YC and non-YC), plans who to contact, writes the messages,
and learns every week from what worked. Nothing is sent without a founder's click.

```
08:30 Mac   bin/daily.sh    plan today's sequences -> draft due messages -> prospect for tomorrow
09:00 bot   #gtm            one card per message: Create draft / Send now / Skip (LinkedIn: Mark sent)
hourly bot  tracker         sent from Gmail? bounces, replies, meetings, opens, LinkedIn emails on the calendar
Mon 07:30   bin/weekly.sh   last week's numbers -> learnings + scoring proposals
Mon 10:00   #gtm            weekly report: ✅ scoring changes, ▶️ next A/B test, 🏆 keep winner
```

Plan, decisions and rules: `PLAN.md`. Working rules for Claude: `CLAUDE.md`.
The Slack / Gmail side is `../pavoot-ops/src/ops/gtm.py`.

## Setup (in this order)

1. **Database** (AWS, paid from credits): in `../pavoot-ops`, commit and run `./deploy.sh`. It creates a
   small Postgres (`GtmDb`, about 10 minutes the first time) next to the bot.
2. **Schema + login**: here, run `bin/setup_db.sh` (needs `aws sso login` first). It loads the tables,
   creates the agent login with a random password, and saves the connection in your Keychain and for the
   bot. No passwords to copy.
3. **Email footer**: in `config/outreach.json`, replace `TODO physical address` with Pavoot's real
   postal address (required by law for cold email). Until then email drafts are blocked; LinkedIn works.
4. **Slack**: create **#gtm** as a *private* channel with only Ana, Gohar and the bot. The bot refuses
   to post there otherwise (reply notifications come from your mailboxes).
5. **Gmail**: Gohar DMs the bot `connect google gohar@pavoot.com` (Ana already connected). Until then,
   set `GTM_SENDERS` to `ana` in `launchd/com.pavoot.gtm-daily.plist`.
6. **Mac schedules**:
   ```
   cp launchd/com.pavoot.gtm-daily.plist launchd/com.pavoot.gtm-weekly.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pavoot.gtm-daily.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pavoot.gtm-weekly.plist
   ```
   First run by hand: `bin/daily.sh`, then read `runs/<date>/run.log`.
7. **LinkedIn emails**: in LinkedIn → Settings → Communications → Email, keep notifications on for
   reactions, comments, profile views, invitations and messages (both founders). The bot reads those
   emails to know who engaged and to tick LinkedIn cards.

There's no step for A/B tests: the Monday run proposes them and you click ▶️ Start in #gtm.

## Everyday use

- **Approve**: open the #gtm thread each morning. 📝 Create draft puts the email in your Gmail to edit
  and send there; 🚀 Send now sends it as is. Only the sender's own click works.
- **LinkedIn**: copy the text and send it on LinkedIn (no API for this, so it stays your click; ✅ Mark sent
  if you like). The bot ticks it by itself when they accept the invitation, and records their LinkedIn
  reply from LinkedIn's notification email. Meetings on your calendar are found automatically.
- **Engagement** (who reacted, commented, viewed your profile) comes from LinkedIn's emails: a prospect who
  engages gets a warm signal and moves up the list. Nothing to type.
- **Post stats**: drop LinkedIn's analytics export (Analytics → Export) in #gtm or DM it to the bot, whenever
  you like; LinkedIn's weekly stats emails are read too.
- **A/B tests**: every Monday, 🏆 Keep winner when a test is clear, ▶️ Start for the next one.
- **Never contact a company**: add its domain to `config/exclude_domains.txt`.
- **Open rates**: emails carry a tiny tracking image (`open_tracking` in `config/outreach.json`:
  `all`, `followups` or `off`). Apple Mail's privacy proxy and scanners are counted apart as machine opens.
  Re-reading your own sent email in Gmail counts as an open, so treat open rates as approximate and
  judge tests on positive replies.
- **pavoot.events**: set `"marketplace_live": true` in `config/outreach.json` once it's live; until
  then messages never mention it.

## Commands

```
.venv/bin/python -m pytest -q                       # tests (throwaway Postgres; needs initdb)
.venv/bin/python -m gtm.cli stats                   # what's in the CRM
.venv/bin/python -m gtm.cli conflicts               # imported values that differ, waiting for you
bin/daily.sh / bin/weekly.sh / bin/prospect.sh      # the scheduled runs, by hand
```
