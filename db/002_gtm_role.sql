-- Login for the GTM agent (Ana's Mac + pavoot-ops). It can use the gtm schema and nothing else.
-- Run AFTER 001_gtm_schema.sql, as the database admin. bin/setup_db.sh does it: it replaces CHANGE_ME
-- with a random password and stores the connection string only in:
--   Mac:        security add-generic-password -a gtm_agent -s pavoot-gtm-db -w '<url>'
--   pavoot-ops: Parameter Store /pavoot-ops/gtm-db-url (SecureString)
-- (?sslmode=require: the database only accepts SSL.)

do $$ begin
    if not exists (select 1 from pg_roles where rolname = 'gtm_agent') then
        create role gtm_agent login password 'CHANGE_ME';
    end if;
end $$;

grant usage on schema gtm to gtm_agent;
grant select, insert, update, delete on all tables in schema gtm to gtm_agent;
grant usage, select on all sequences in schema gtm to gtm_agent;
alter default privileges in schema gtm grant select, insert, update, delete on tables to gtm_agent;
alter default privileges in schema gtm grant usage, select on sequences to gtm_agent;

-- If this ever runs on Supabase: its public API roles get nothing in gtm.
do $$ declare r text; begin
    foreach r in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on schema gtm from %I', r);
            execute format('revoke all on all tables in schema gtm from %I', r);
        end if;
    end loop;
end $$;

-- Row level security on every gtm table, with one rule: the gtm_agent login may use all rows.
-- (Anyone else without a policy sees nothing; the postgres owner is unaffected.)
do $$ declare t text; begin
    for t in select tablename from pg_tables where schemaname = 'gtm' loop
        execute format('alter table gtm.%I enable row level security', t);
        if not exists (select 1 from pg_policies where schemaname = 'gtm' and tablename = t and policyname = 'gtm_agent_all') then
            execute format('create policy gtm_agent_all on gtm.%I for all to gtm_agent using (true) with check (true)', t);
        end if;
    end loop;
end $$;

-- Belt and braces: no reach into the app's schemas.
revoke all on schema public from gtm_agent;
revoke all on all tables in schema public from gtm_agent;

-- Check (should list only gtm tables):
-- select table_schema, table_name from information_schema.role_table_grants
--  where grantee = 'gtm_agent' group by 1, 2 order by 1, 2;
