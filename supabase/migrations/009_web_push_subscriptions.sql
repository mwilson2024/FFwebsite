begin;

-- Push endpoints are bearer-capability URLs. Keep the complete subscription
-- encrypted with the same application-managed key used for remembered MFL
-- sessions; only a one-way endpoint digest is indexed.
create table if not exists fantasy_hq.web_push_subscription (
    id bigint generated always as identity primary key,
    user_id uuid not null references fantasy_hq.app_user(id) on delete cascade,
    endpoint_hash bytea not null,
    encrypted_subscription bytea not null,
    season smallint not null check (season between 2000 and 2100),
    league_id text not null default '',
    last_signature text not null default '',
    last_sent_at timestamptz,
    expires_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint web_push_subscription_endpoint_unique unique (user_id, endpoint_hash),
    constraint web_push_subscription_ciphertext_size
        check (octet_length(encrypted_subscription) between 80 and 16384)
);

create index if not exists web_push_subscription_user_id_idx
    on fantasy_hq.web_push_subscription (user_id);
create index if not exists web_push_subscription_expiry_idx
    on fantasy_hq.web_push_subscription (expires_at)
    where expires_at is not null;

alter table fantasy_hq.web_push_subscription enable row level security;
revoke all on fantasy_hq.web_push_subscription from anon, authenticated;

comment on table fantasy_hq.web_push_subscription is
    'Private encrypted Web Push subscriptions. The browser Data API has no access.';
comment on column fantasy_hq.web_push_subscription.endpoint_hash is
    'SHA-256 lookup digest; the capability-bearing endpoint exists only inside encrypted_subscription.';

insert into fantasy_hq.schema_migration (version, name)
values (9, 'Encrypted background Web Push subscriptions')
on conflict (version) do nothing;

commit;
