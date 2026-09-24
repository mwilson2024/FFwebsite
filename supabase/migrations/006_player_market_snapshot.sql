begin;

-- A last-known player market makes the Players page useful during a cold app
-- start without treating cached ownership or availability as transaction-safe.
-- The FastAPI server refreshes this snapshot from MFL and continues to perform
-- live ownership, availability, and lock validation before every write.
create table if not exists fantasy_hq.player_market_snapshot (
    user_id uuid not null,
    season smallint not null check (season between 2020 and 2100),
    league_id text not null,
    payload jsonb not null,
    captured_at timestamptz not null default now(),
    primary key (user_id, season, league_id),
    foreign key (user_id, season, league_id)
        references fantasy_hq.connected_league(user_id, season, league_id)
        on delete cascade,
    constraint player_market_snapshot_payload_object
        check (jsonb_typeof(payload) = 'object'),
    constraint player_market_snapshot_payload_size
        check (octet_length(payload::text) <= 5242880)
);

alter table fantasy_hq.player_market_snapshot enable row level security;
revoke all on fantasy_hq.player_market_snapshot from anon, authenticated;

comment on table fantasy_hq.player_market_snapshot is
    'Private, browse-only last-known MFL player pool. Never authorizes an add/drop transaction.';
comment on column fantasy_hq.player_market_snapshot.payload is
    'Versioned player identity, ownership, and availability snapshot; excludes credentials and pending actions.';

insert into fantasy_hq.schema_migration (version, name)
values (6, 'Private browse-only player market snapshot')
on conflict (version) do nothing;

commit;
