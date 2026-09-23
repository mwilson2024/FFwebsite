begin;

-- Historical league data is private application data. The website reaches it
-- through its server-side PostgreSQL connection; it is never exposed through
-- Supabase's browser-facing Data API.
create table if not exists fantasy_hq.historical_season (
    user_id uuid not null references fantasy_hq.app_user(id) on delete cascade,
    current_league_id text not null,
    season smallint not null check (season between 1990 and 2100),
    source_league_id text not null,
    league_name text not null,
    start_week smallint not null check (start_week between 1 and 18),
    end_week smallint not null check (end_week between 1 and 18),
    regular_season_end smallint not null check (regular_season_end between 1 and 18),
    imported_at timestamptz not null default now(),
    primary key (user_id, current_league_id, season)
);

create table if not exists fantasy_hq.historical_franchise (
    user_id uuid not null,
    current_league_id text not null,
    season smallint not null,
    franchise_id text not null,
    franchise_name text not null,
    division_id text,
    standing_rank smallint,
    wins smallint,
    losses smallint,
    ties smallint,
    points_for numeric(12, 3),
    points_against numeric(12, 3),
    victory_points numeric(12, 3),
    primary key (user_id, current_league_id, season, franchise_id),
    foreign key (user_id, current_league_id, season)
        references fantasy_hq.historical_season(user_id, current_league_id, season)
        on delete cascade
);

create table if not exists fantasy_hq.historical_matchup_team (
    user_id uuid not null,
    current_league_id text not null,
    season smallint not null,
    week smallint not null check (week between 1 and 18),
    matchup_index smallint not null check (matchup_index > 0),
    franchise_id text not null,
    score numeric(12, 3),
    primary key (
        user_id, current_league_id, season, week, matchup_index, franchise_id
    ),
    foreign key (user_id, current_league_id, season)
        references fantasy_hq.historical_season(user_id, current_league_id, season)
        on delete cascade
);

create index if not exists historical_franchise_manager_idx
    on fantasy_hq.historical_franchise
    (user_id, current_league_id, franchise_id, season desc);

create index if not exists historical_matchup_franchise_idx
    on fantasy_hq.historical_matchup_team
    (user_id, current_league_id, franchise_id, season desc, week);

alter table fantasy_hq.historical_season enable row level security;
alter table fantasy_hq.historical_franchise enable row level security;
alter table fantasy_hq.historical_matchup_team enable row level security;

revoke all on fantasy_hq.historical_season from anon, authenticated;
revoke all on fantasy_hq.historical_franchise from anon, authenticated;
revoke all on fantasy_hq.historical_matchup_team from anon, authenticated;

comment on table fantasy_hq.historical_season is
    'Idempotent MFL season imports owned by the connected MFL application account.';
comment on table fantasy_hq.historical_franchise is
    'Season-specific MFL franchise identity and final standings values.';
comment on table fantasy_hq.historical_matchup_team is
    'One participant row per MFL historical matchup, grouped by week and matchup_index.';

insert into fantasy_hq.schema_migration (version, name)
values (3, 'MFL historical league archive')
on conflict (version) do nothing;

commit;
