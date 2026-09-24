begin;

-- The runtime prunes a small, bounded batch of rows after their stale fallback
-- window. This index keeps that periodic oldest-first lookup off the JSON payload.
create index if not exists provider_cache_stale_until_idx
    on fantasy_hq.provider_cache (stale_until);

insert into fantasy_hq.schema_migration (version, name)
values (8, 'Provider cache maintenance index')
on conflict (version) do nothing;

commit;
