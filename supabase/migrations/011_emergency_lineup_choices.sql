begin;

alter table fantasy_hq.emergency_lineup_preference
    add column if not exists prompt_answered boolean not null default false;
alter table fantasy_hq.emergency_lineup_preference
    add column if not exists replacement_mode text not null default 'projection';

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'emergency_lineup_preference_mode_check'
          and conrelid = 'fantasy_hq.emergency_lineup_preference'::regclass
    ) then
        alter table fantasy_hq.emergency_lineup_preference
            add constraint emergency_lineup_preference_mode_check
            check (replacement_mode in ('projection', 'priority'));
    end if;
end $$;

create table if not exists fantasy_hq.emergency_lineup_priority (
    user_id uuid not null,
    season smallint not null check (season between 2020 and 2100),
    league_id text not null,
    player_id text not null,
    priority smallint not null check (priority between 1 and 64),
    updated_at timestamptz not null default now(),
    primary key (user_id, season, league_id, player_id),
    unique (user_id, season, league_id, priority),
    foreign key (user_id, season, league_id)
        references fantasy_hq.emergency_lineup_preference(user_id, season, league_id)
        on delete cascade
);

alter table fantasy_hq.emergency_lineup_priority enable row level security;
revoke all on fantasy_hq.emergency_lineup_priority from anon, authenticated;

comment on column fantasy_hq.emergency_lineup_preference.prompt_answered is
    'True after the owner answers the one-time per-league yes/no prompt.';
comment on column fantasy_hq.emergency_lineup_preference.replacement_mode is
    'Use MFL projected points or the owner-defined legal bench priority.';
comment on table fantasy_hq.emergency_lineup_priority is
    'Private ordered player preferences for emergency lineup replacement.';

insert into fantasy_hq.schema_migration (version, name)
values (11, 'Emergency lineup onboarding and player priority')
on conflict (version) do nothing;

commit;
