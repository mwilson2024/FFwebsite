begin;

-- The original private schema reserved provider_cache for persistent,
-- account-scoped read-through caching. Ensure it is available for runtime MFL
-- reports without exposing it through the browser Data API.
create table if not exists fantasy_hq.provider_cache (
    scope_hash bytea not null,
    season smallint not null check (season between 2000 and 2100),
    league_id text not null,
    report_key text not null,
    payload jsonb not null,
    source_etag text,
    fetched_at timestamptz not null default now(),
    fresh_until timestamptz not null,
    stale_until timestamptz not null,
    schema_version integer not null default 1,
    primary key (scope_hash, season, league_id, report_key),
    constraint provider_cache_freshness
        check (fresh_until >= fetched_at and stale_until >= fresh_until)
);

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'provider_cache_payload_object'
          and conrelid = 'fantasy_hq.provider_cache'::regclass
    ) then
        alter table fantasy_hq.provider_cache
            add constraint provider_cache_payload_object
            check (jsonb_typeof(payload) = 'object') not valid;
    end if;
    if not exists (
        select 1 from pg_constraint
        where conname = 'provider_cache_payload_size'
          and conrelid = 'fantasy_hq.provider_cache'::regclass
    ) then
        alter table fantasy_hq.provider_cache
            add constraint provider_cache_payload_size
            check (octet_length(payload::text) <= 10485760) not valid;
    end if;
end
$$;

alter table fantasy_hq.provider_cache enable row level security;
revoke all on fantasy_hq.provider_cache from anon, authenticated;

comment on table fantasy_hq.provider_cache is
    'Private display-only provider cache. Never authorizes a transaction or live scoring state.';
comment on column fantasy_hq.provider_cache.scope_hash is
    'One-way account/session scope; never an MFL username, password, cookie, or API key.';
comment on column fantasy_hq.provider_cache.payload is
    'Versioned JSON report payload without credentials, CSRF state, or pending actions.';

insert into fantasy_hq.schema_migration (version, name)
values (7, 'Persistent provider report cache runtime')
on conflict (version) do nothing;

commit;
