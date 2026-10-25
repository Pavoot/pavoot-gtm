-- Removes everything 001_gtm_schema.sql and 002_gtm_role.sql created (used once, 2026-10-24, to move
-- the GTM CRM out of the Supabase production project to RDS). Touches nothing else.
-- On Supabase, postgres isn't a superuser: it must join gtm_agent before it may drop what gtm_agent owns.
drop schema if exists gtm cascade;
grant gtm_agent to postgres;
drop owned by gtm_agent;
drop role gtm_agent;
