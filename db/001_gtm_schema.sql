-- Pavoot GTM CRM: Pavoot's own sales database, kept apart from the app's data.
-- Lives in its own RDS Postgres (pavoot-ops stack, database "gtm", schema "gtm"); only the gtm_agent
-- login (002_gtm_role.sql) is meant to use it, and that login cannot see app tables.
-- Idempotent: safe to run twice. Loaded by bin/setup_db.sh.

create schema if not exists gtm;

-- Companies we sell to (and referral partners such as GTM agencies).
create table if not exists gtm.companies (
    id              bigserial primary key,
    name            text not null,
    domain          text,                         -- lowercase, no www
    linkedin_url    text,
    kind            text not null default 'prospect'
                    check (kind in ('prospect', 'referral_partner', 'customer')),
    source          text not null
                    check (source in ('yc', 'non_yc', 'referral', 'warm', 'manual')),
    stage           text not null default 'new'
                    check (stage in ('new', 'contacting', 'replied', 'meeting', 'pilot',
                                     'customer', 'lost', 'paused')),
    funding_stage   text,                         -- e.g. 'Series A'
    last_funding_at date,
    hq_city         text,
    employees       int,
    signals         jsonb not null default '[]',  -- [{type, detail, url, seen_at}]
    score           int,                          -- higher = contact sooner
    sponsor_cluster text,                         -- non-competing companies with the same ICP
    global_crm_ref  text,                         -- id in the app's global CRM, if it came from there
    notes           text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
create unique index if not exists companies_domain_key on gtm.companies (lower(domain)) where domain is not null;

-- People: buyers at companies, plus speakers and photographers for pavoot.events.
create table if not exists gtm.people (
    id              bigserial primary key,
    company_id      bigint references gtm.companies (id) on delete set null,
    full_name       text not null,
    title           text,
    role            text not null default 'Other'
                    check (role in ('CEO', 'CMO', 'CPO', 'Field Marketing', 'GTM/Growth',
                                    'Head of Events', 'Speaker', 'Photographer', 'Other')),
    email           text,
    email_status    text not null default 'unknown'
                    check (email_status in ('unknown', 'valid', 'risky', 'invalid')),
    linkedin_url    text,
    x_url           text,
    city            text,
    is_warm         boolean not null default false, -- warm leads are added by hand (TODO)
    source          text,                           -- 'global_crm', 'apollo', 'pdl', 'web', 'manual'...
    source_ref      text,
    notes           text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
create unique index if not exists people_email_key on gtm.people (lower(email)) where email is not null;
create unique index if not exists people_linkedin_key on gtm.people (linkedin_url) where linkedin_url is not null;
create index if not exists people_company_idx on gtm.people (company_id);

-- One A/B experiment = one variable, written down before it starts.
create table if not exists gtm.experiments (
    id              bigserial primary key,
    name            text not null unique,
    variable        text not null
                    check (variable in ('sender', 'angle', 'ask', 'channel_order', 'subject', 'other')),
    hypothesis      text not null,
    win_condition   text not null,
    variants        jsonb not null,               -- [{key, description}]
    status          text not null default 'draft'
                    check (status in ('draft', 'running', 'stopped', 'decided')),
    decision        text,
    started_at      timestamptz,
    ended_at        timestamptz,
    created_at      timestamptz not null default now()
);

-- A person's run through the day 1 / 2 / 3 / 7 cadence. Variant is fixed for the whole run.
create table if not exists gtm.sequences (
    id              bigserial primary key,
    person_id       bigint not null references gtm.people (id) on delete cascade,
    kind            text not null
                    check (kind in ('cold', 'warm', 'speaker', 'photographer', 'referral_partner')),
    sender          text not null,                -- mailbox, e.g. ana@pavoot.com / gohar@pavoot.com
    experiment_id   bigint references gtm.experiments (id),
    variant         text,
    status          text not null default 'active'
                    check (status in ('active', 'paused', 'finished', 'stopped')),
    paused_reason   text,                         -- e.g. 'colleague replied'
    started_on      date not null default current_date,
    created_at      timestamptz not null default now()
);
-- At most one open sequence per person.
create unique index if not exists sequences_one_open on gtm.sequences (person_id) where status in ('active', 'paused');

-- Every message, drafted or sent, on any channel.
create table if not exists gtm.touches (
    id               bigserial primary key,
    sequence_id      bigint not null references gtm.sequences (id) on delete cascade,
    step             smallint not null check (step in (1, 2, 3, 7)),  -- cadence day
    channel          text not null check (channel in ('email', 'linkedin', 'x', 'other')),
    scheduled_for    date not null,
    subject          text,
    body             text not null,
    status           text not null default 'drafted'
                     check (status in ('drafted', 'approved', 'gmail_draft', 'sent',
                                       'bounced', 'skipped', 'failed')),
    approved_by      text,
    gmail_draft_id   text,
    gmail_message_id text,
    gmail_thread_id  text,
    slack_ts         text,
    sent_at          timestamptz,
    first_open_at    timestamptz,                 -- only when the open pixel is on (off by default)
    open_count       int not null default 0,
    tracking_token   uuid not null default gen_random_uuid() unique,
    utm              jsonb,                       -- {source, campaign, content} on pavoot.events links
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    unique (sequence_id, step, channel)
);
create index if not exists touches_due_idx on gtm.touches (scheduled_for, status);
create index if not exists touches_thread_idx on gtm.touches (gmail_thread_id) where gmail_thread_id is not null;

create table if not exists gtm.replies (
    id                 bigserial primary key,
    touch_id           bigint references gtm.touches (id) on delete set null,
    person_id          bigint not null references gtm.people (id) on delete cascade,
    gmail_message_id   text unique,
    received_at        timestamptz not null,
    class              text not null
                       check (class in ('interested', 'not_now', 'wrong_person', 'unsubscribe',
                                        'out_of_office', 'other')),
    referral_person_id bigint references gtm.people (id),  -- "talk to X instead"
    summary            text,
    remind_on          date,                    -- not_now -> +90 days
    created_at         timestamptz not null default now()
);

create table if not exists gtm.meetings (
    id                bigserial primary key,
    person_id         bigint not null references gtm.people (id) on delete cascade,
    touch_id          bigint references gtm.touches (id) on delete set null,
    starts_at         timestamptz not null,
    calendar_event_id text unique,
    source            text not null check (source in ('calendar', 'reply', 'manual')),
    outcome           text,
    created_at        timestamptz not null default now()
);

-- Never contact these addresses again.
create table if not exists gtm.suppression (
    email     text primary key check (email = lower(email)),
    reason    text not null check (reason in ('unsubscribe', 'bounce', 'do_not_contact', 'customer')),
    note      text,
    added_at  timestamptz not null default now()
);

-- Imports never overwrite: a differing value lands here and waits for Ana's decision.
create table if not exists gtm.conflicts (
    id             bigserial primary key,
    entity         text not null check (entity in ('company', 'person')),
    entity_id      bigint not null,
    field          text not null,
    current_value  text,
    proposed_value text,
    source         text not null,
    status         text not null default 'open' check (status in ('open', 'keep', 'replace')),
    decided_by     text,
    decided_at     timestamptz,
    created_at     timestamptz not null default now()
);
create unique index if not exists conflicts_one_open on gtm.conflicts (entity, entity_id, field, proposed_value) where status = 'open';

-- Branding: mentions, "how did you hear about us", branded search, sign-ups.
create table if not exists gtm.brand_signals (
    id       bigserial primary key,
    kind     text not null check (kind in ('mention', 'how_heard', 'branded_search', 'signup', 'pledge')),
    source   text,                                 -- 'reddit', 'x', 'call', 'pavoot.events'...
    url      text,
    detail   text,
    utm      jsonb,
    seen_at  timestamptz not null default now()
);

-- Weekly A/B report: one row per experiment variant.
create or replace view gtm.variant_stats as
-- Each count is taken per sequence first, so several replies or meetings never multiply the sends.
select e.name as experiment,
       s.variant,
       count(*)                          as people,
       sum(t.sent)                       as sent,
       sum(t.bounced)                    as bounced,
       sum(t.opened)                     as opened,
       count(*) filter (where r.any)     as replied,
       count(*) filter (where r.pos)     as positive,
       count(*) filter (where m.any)     as meetings
from gtm.experiments e
join gtm.sequences s on s.experiment_id = e.id
cross join lateral (
    select count(*) filter (where status in ('sent', 'bounced')) as sent,
           count(*) filter (where status = 'bounced')            as bounced,
           count(*) filter (where first_open_at is not null)     as opened
    from gtm.touches where sequence_id = s.id) t
cross join lateral (
    select count(*) > 0 as any, coalesce(bool_or(class = 'interested'), false) as pos
    from gtm.replies where person_id = s.person_id and received_at >= s.created_at) r
cross join lateral (
    select count(*) > 0 as any
    from gtm.meetings where person_id = s.person_id and created_at >= s.created_at) m
group by e.name, s.variant;
