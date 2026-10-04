begin;

-- Emergency lineup automation is a private, explicit per-league choice. The
-- browser Data API has no access; only the authenticated FastAPI server may
-- read or change it through its TLS PostgreSQL connection.
create table if not exists fantasy_hq.emergency_lineup_preference (
    user_id uuid not null,
    season smallint not null check (season between 2020 and 2100),
    league_id text not null,
    enabled boolean not null default false,
    updated_at timestamptz not null default now(),
    primary key (user_id, season, league_id),
    foreign key (user_id, season, league_id)
        references fantasy_hq.connected_league(user_id, season, league_id)
        on delete cascade
);

-- One claimed row means one and only one five-minute submission attempt. An
-- interrupted or uncertain MFL write remains claimed so a restart cannot
-- repeat a potentially successful transaction.
create table if not exists fantasy_hq.emergency_lineup_action (
    user_id uuid not null references fantasy_hq.app_user(id) on delete cascade,
    action_key text not null check (action_key ~ '^[0-9a-f]{64}$'),
    season smallint not null check (season between 2020 and 2100),
    league_id text not null,
    week smallint not null check (week between 1 and 18),
    kickoff_at timestamptz not null,
    changed_player_count smallint not null check (changed_player_count between 1 and 32),
    outcome text not null default 'attempting' check (
        outcome in ('attempting', 'submitted', 'verified', 'failed', 'uncertain')
    ),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (user_id, action_key)
);

create index if not exists emergency_lineup_action_lookup_idx
    on fantasy_hq.emergency_lineup_action (user_id, season, league_id, week, kickoff_at desc);

alter table fantasy_hq.emergency_lineup_preference enable row level security;
alter table fantasy_hq.emergency_lineup_action enable row level security;
revoke all on fantasy_hq.emergency_lineup_preference from anon, authenticated;
revoke all on fantasy_hq.emergency_lineup_action from anon, authenticated;

comment on table fantasy_hq.emergency_lineup_preference is
    'Private opt-in for narrowly scoped five-minute Out/Inactive starter replacement.';
comment on table fantasy_hq.emergency_lineup_action is
    'Idempotency and outcome audit for one-attempt emergency lineup submissions; no player identities are stored.';

insert into fantasy_hq.schema_migration (version, name)
values (10, 'Emergency lineup opt-in and idempotency guard')
on conflict (version) do nothing;

commit;
