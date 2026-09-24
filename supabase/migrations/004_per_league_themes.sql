begin;

-- Optional per-league themes are private account preferences. The browser
-- never talks to this table; the FastAPI server uses its TLS PostgreSQL
-- connection after the same MFL account has authenticated.
create table if not exists fantasy_hq.league_theme (
    user_id uuid not null,
    season smallint not null check (season between 2020 and 2100),
    league_id text not null,
    theme text not null check (
        theme in ('michigan', 'lions', 'aurora', 'tigers', 'redwings', 'pistons')
    ),
    updated_at timestamptz not null default now(),
    primary key (user_id, season, league_id),
    foreign key (user_id, season, league_id)
        references fantasy_hq.connected_league(user_id, season, league_id)
        on delete cascade
);

alter table fantasy_hq.league_theme enable row level security;
revoke all on fantasy_hq.league_theme from anon, authenticated;

comment on table fantasy_hq.league_theme is
    'Private per-league theme overrides for an authenticated MFL application account.';

insert into fantasy_hq.schema_migration (version, name)
values (4, 'Private per-league theme preferences')
on conflict (version) do nothing;

commit;
