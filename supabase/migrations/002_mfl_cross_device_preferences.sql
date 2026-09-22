begin;

-- MFL is the application's identity provider. The application stores only a
-- one-way fingerprint of the normalized MFL login; usernames and passwords are
-- never written to PostgreSQL.
alter table fantasy_hq.app_user
    add column if not exists identity_provider text not null default 'mfl';

alter table fantasy_hq.app_user
    drop constraint if exists app_user_identity_provider_check;
alter table fantasy_hq.app_user
    add constraint app_user_identity_provider_check
    check (identity_provider = 'mfl');

alter table fantasy_hq.user_preference
    add column if not exists onboarding_complete boolean not null default false;
alter table fantasy_hq.user_preference
    add column if not exists ranking_setup_complete boolean not null default false;

comment on column fantasy_hq.app_user.owner_fingerprint_hash is
    'One-way application fingerprint of the normalized MFL login; never the MFL username.';
comment on table fantasy_hq.user_preference is
    'Non-secret account choices restored after the same MFL user signs in on any device.';

insert into fantasy_hq.schema_migration (version, name)
values (2, 'MFL account identity and cross-device preferences')
on conflict (version) do nothing;

commit;
