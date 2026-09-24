begin;

-- Supabase keeps PostgREST running when the Data API is disabled. In that
-- state it may repeatedly try to inspect a non-existent placeholder schema
-- named pg_pgrst_no_exposed_schemas. Supabase's documented workaround is to
-- point PostgREST at a real, intentionally empty schema instead.
create schema if not exists pgrst_no_exposed_schemas;

comment on schema pgrst_no_exposed_schemas is
    'Intentionally empty PostgREST target while the Supabase Data API is disabled.';

alter role authenticator set pgrst.db_schemas = 'pgrst_no_exposed_schemas';

insert into fantasy_hq.schema_migration (version, name)
values (5, 'Quiet disabled Supabase Data API placeholder errors')
on conflict (version) do nothing;

commit;

-- Apply the new role setting without waiting for PostgREST to restart.
notify pgrst, 'reload config';
