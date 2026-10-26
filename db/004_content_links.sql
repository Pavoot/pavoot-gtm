-- Links the content agent (pavoot-content) puts in posts, so both agents can join "this post" to what its link did
-- (visits, registrations, pledges, meetings) through the UTM campaign. Written by pavoot-content's run.sh after each
-- daily run (drafted), marked posted when the founder logs the post; read by both weekly learning runs.
-- Idempotent: safe to re-run (bin/migrate.sh).

create table if not exists gtm.content_links (
    id            bigserial primary key,
    campaign      text not null,                 -- utm_campaign, e.g. content-2026-10-27-show-up-rates
    platform      text not null check (platform in ('linkedin', 'x', 'reddit', 'instagram', 'other')),
    author        text not null,                 -- 'ana' | 'gohar' | 'company' | reddit username
    utm_content   text,                          -- <author>-<variant/test arm>
    destination   text not null,                 -- the full tagged URL as written in the post
    target        text not null check (target in ('event_page', 'pavoot_events', 'pavoot_com', 'booking', 'other')),
    test_arm      text,                          -- the A/B arm from the content playbook, if any
    draft_ref     text,                          -- drafts/daily/<date>.md + slot
    post_url      text,                          -- filled when the founder logs the post
    status        text not null default 'drafted' check (status in ('drafted', 'posted', 'skipped')),
    drafted_on    date not null default current_date,
    posted_on     date,
    created_at    timestamptz not null default now(),
    unique (campaign, platform, author, destination)
);
create index if not exists content_links_campaign on gtm.content_links (campaign);
create index if not exists content_links_drafted on gtm.content_links (drafted_on);

-- Attribution per campaign, as reported by the Pavoot app (get_campaign_attribution) and stored weekly by the
-- learning runs, so trends survive the app's 90-day visit retention.
create table if not exists gtm.campaign_results (
    week              date not null,             -- Monday of the week the numbers were read
    utm_source        text not null,
    utm_campaign      text not null,
    utm_content       text not null default '',
    event_page_views  int,
    registrations     int,
    marketplace_visits int,
    marketplace_signups int,
    pledges           int,
    meetings          int,                       -- meetings booked with people who came through it (if known)
    recorded_at       timestamptz not null default now(),
    primary key (week, utm_source, utm_campaign, utm_content)
);

-- Row level security (same rule as 002/003: only gtm_agent).
do $$
declare t text;
begin
    foreach t in array array['content_links', 'campaign_results'] loop
        execute format('alter table gtm.%I enable row level security', t);
        if not exists (select 1 from pg_policies where schemaname = 'gtm' and tablename = t and policyname = 'gtm_agent_all') then
            execute format('create policy gtm_agent_all on gtm.%I for all to gtm_agent using (true) with check (true)', t);
        end if;
    end loop;
end $$;

grant select, insert, update, delete on gtm.content_links, gtm.campaign_results to gtm_agent;
grant usage, select on all sequences in schema gtm to gtm_agent;
