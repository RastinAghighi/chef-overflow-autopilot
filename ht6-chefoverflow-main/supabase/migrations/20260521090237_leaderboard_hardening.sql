-- Leaderboard hardening: lock writes behind service-role Edge Functions,
-- enforce one best row per email, add single-use run tokens.

alter table public.leaderboard enable row level security;

drop policy if exists "lb_read" on public.leaderboard;
create policy "lb_read" on public.leaderboard for select using (true);
-- No insert/update/delete policy => anon + authenticated cannot write directly.
-- service_role (used inside Edge Functions) bypasses RLS.

-- Normalize: drop duplicate emails (keeping the highest score) before adding the unique constraint.
delete from public.leaderboard a
    using public.leaderboard b
    where lower(a.email) = lower(b.email)
      and (a.score < b.score or (a.score = b.score and a.ctid < b.ctid));

update public.leaderboard set email = lower(email) where email <> lower(email);

alter table public.leaderboard
    add constraint leaderboard_email_uniq unique (email);

create table if not exists public.run_tokens (
    id uuid primary key default gen_random_uuid(),
    issued_at timestamptz not null default now(),
    used_at timestamptz,
    client_ip text
);

alter table public.run_tokens enable row level security;
-- No policies => only service_role can read/write.

create index if not exists run_tokens_issued_idx
    on public.run_tokens (issued_at);
