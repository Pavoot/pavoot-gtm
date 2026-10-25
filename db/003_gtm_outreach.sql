-- Outreach + learning loop additions. Run after 001 and 002. Idempotent.
-- Ana applies it; Claude never runs it against production.

-- Touches are planned ahead (all cadence steps), then drafted on the day they are due.
alter table gtm.touches alter column body set default '';
alter table gtm.touches drop constraint if exists touches_status_check;
alter table gtm.touches add constraint touches_status_check
    check (status in ('planned', 'drafted', 'gmail_draft', 'sent', 'bounced', 'skipped', 'failed'));
alter table gtm.touches alter column status set default 'planned';
alter table gtm.touches add column if not exists warnings text[] not null default '{}';  -- lint notes shown on the card
alter table gtm.touches add column if not exists slack_channel text;
alter table gtm.touches add column if not exists drafted_at timestamptz;
-- Open tracking: the bot adds a 1x1 image (served at /o/<tracking_token>.gif) when track_opens is set.
-- open_count / first_open_at = likely human opens; opens_machine = Apple Mail privacy proxy and
-- scanners that fetch images on delivery (kept apart so they don't inflate open rates).
alter table gtm.touches add column if not exists track_opens boolean not null default false;
alter table gtm.touches add column if not exists opens_machine int not null default 0;

-- Share of new people that always gets the baseline variant (measures gains against something real).
alter table gtm.experiments add column if not exists holdout_pct int not null default 10
    check (holdout_pct between 0 and 50);

-- How a sequence got its variant: 'holdout' (baseline on purpose), 'sampled' (Thompson), 'fixed' (no experiment).
alter table gtm.sequences add column if not exists assignment text not null default 'fixed'
    check (assignment in ('holdout', 'sampled', 'fixed'));

-- LinkedIn replies are recorded from Slack buttons, email replies by the tracker.
alter table gtm.replies add column if not exists channel text not null default 'email';

-- Weekly learnings: each claim with its evidence; drafting reads the active ones.
create table if not exists gtm.learnings (
    id          bigserial primary key,
    week        date not null,                 -- Monday of the week it was learned
    claim       text not null,
    evidence    text not null,
    n           int not null,                  -- sample size behind the claim
    status      text not null default 'active' check (status in ('active', 'leaning', 'retired')),
    created_at  timestamptz not null default now()
);

-- Scoring weights with history. Proposed by the learning loop, applied only after Ana approves.
create table if not exists gtm.score_weights (
    id           bigserial primary key,
    kind         text not null,
    weight       int not null check (weight between 0 and 100),
    reason       text,
    status       text not null default 'proposed' check (status in ('proposed', 'approved', 'rejected')),
    approved_by  text,
    valid_from   timestamptz,
    created_at   timestamptz not null default now()
);

-- Post performance (from the content agent / LinkedIn export / manual notes), shared by both agents.
create table if not exists gtm.content_metrics (
    id             bigserial primary key,
    post_url       text not null,
    author         text not null,               -- 'ana' | 'gohar' | 'company'
    posted_at      date,
    impressions    int,
    reactions      int,
    comments       int,
    profile_views  int,
    source         text not null check (source in ('export', 'manual', 'content_agent', 'email')),
    recorded_at    timestamptz not null default now(),
    unique (post_url, recorded_at)
);

-- One #gtm morning post per day (the bot's hourly tick checks this before posting).
create table if not exists gtm.daily_posts (
    day        date primary key,
    slack_ts   text,
    cards      int not null default 0,
    posted_at  timestamptz not null default now()
);

-- Weekly report written by the Mac's learning run, posted to #gtm by the bot (Monday 10:00 SF).
create table if not exists gtm.weekly_reports (
    week       date primary key,              -- Monday of the week reported
    summary    text not null,                 -- Slack mrkdwn
    stats      jsonb not null,
    slack_ts   text,
    created_at timestamptz not null default now()
);

-- Row level security for the tables added here (same rule as 002: only gtm_agent).
do $$ declare t text; begin
    for t in select tablename from pg_tables where schemaname = 'gtm' loop
        execute format('alter table gtm.%I enable row level security', t);
        if not exists (select 1 from pg_policies where schemaname = 'gtm' and tablename = t and policyname = 'gtm_agent_all') then
            execute format('create policy gtm_agent_all on gtm.%I for all to gtm_agent using (true) with check (true)', t);
        end if;
    end loop;
end $$;

-- Grants for tables created after 002 ran (default privileges cover them when 002 ran first,
-- but repeating is harmless and covers any order).
grant select, insert, update, delete on all tables in schema gtm to gtm_agent;
grant usage, select on all sequences in schema gtm to gtm_agent;
