# pavoot-gtm

Pavoot's GTM agent: prospecting, outreach drafts, A/B tests. Runs on Ana's Mac (launchd + `claude -p`).
Plan and decisions: `PLAN.md`. Mailbox work (Gmail drafts/sends, reply and meeting tracking, the
#gtm Slack cards) lives in `../pavoot-ops`, not here. The two meet only in the `gtm` database schema.

## Layout
- `db/` SQL for the `gtm` schema + the `gtm_agent` login, loaded into the RDS Postgres by `bin/setup_db.sh`.
- `gtm/` Python: `db` (connection), `yc` (YC candidates from the scraped directory, read-only),
  `roles` (title -> role, who to contact), `score` (signal scoring, approved weights), `importer`
  (additive import), `planner` (sequences, variants, cadence), `drafts` (export / lint / import),
  `learn` (weekly stats, weight proposals, learnings), `cli`.
- `config/` outreach.json (footer, marketplace_live), exclude_domains.txt (never prospect).
- `prompts/` headless prompts. `bin/` run scripts. `launchd/` schedules. `runs/<date>/` outputs (gitignored).

## Commands
- Tests: `.venv/bin/python -m pytest -q` (spins up a throwaway Postgres; needs `initdb`).
- CLI: `.venv/bin/python -m gtm.cli --help` (prospecting, plan, export-due, import-drafts, rescore,
  learn-stats, learn-import, stats, conflicts; `--date` / `--week` for testing on a given day).
- Runs: `bin/daily.sh` (plan, draft, then prospect), `bin/weekly.sh` (learning), `bin/prospect.sh`.
  Env: RUN_DIR, YC_N, NONYC_N, FIND_BUDGET, GTM_SENDERS, GTM_DAILY_NEW, GTM_WEEK, GTM_SETTINGS.
- Headless runs are read-only (`--allowedTools Read ...`) and return a final ```json block that the
  CLI extracts; never give them write or Bash access.

## Rules
- Never commit or push; hand Ana the commit message. No Co-Authored-By or any Claude attribution.
- Never run `db/*.sql` or `bin/setup_db.sh` against the real database yourself; ask before connecting to it. Test SQL on a
  throwaway Postgres (the test suite does this).
- The DB URL lives only in GTM_DB_URL or the Keychain item `pavoot-gtm-db`. Never print or write it.
- Imports are additive: fill empty fields, send differing values to `gtm.conflicts`. Never overwrite.
- Keep transactions short (production kills 5-min idle transactions and 2-min statements); never hold
  one open across an LLM/HTTP call; never run bulk imports in parallel.
- Nothing is sent to a prospect without a founder's click. No guessed emails or LinkedIn URLs.
- Reddit: mentions of Pavoot always disclose the connection (lives in the content agent, not here).
- Disclosure (outreach copy, learnings, reports): never name customers (AWS for Startups, Fondo);
  Stripe / Deel never presented as customers; no client counts; event numbers only when impressive
  and approved; prices / MRR / runway / round stay internal. See PLAN.md section 3.
- Customers and partners go in `config/exclude_domains.txt` so they are never prospected.
- Gohar uses she/her.
