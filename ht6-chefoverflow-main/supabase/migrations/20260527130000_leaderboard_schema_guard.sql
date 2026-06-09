-- Idempotent schema guard for the leaderboard table.
-- Safe to run repeatedly. Creates the table if missing, then adds any
-- columns the submit_run RPC needs. If a column already exists with a
-- compatible type the ADD COLUMN IF NOT EXISTS is a no-op.

create table if not exists public.leaderboard (
    email      text        primary key,
    score      int         not null default 0,
    grade      text        not null default 'F',
    streak     int         not null default 0,
    delivered  int         not null default 0,
    time_secs  int         not null default 0,
    run_id     uuid,
    created_at timestamptz not null default now()
);

-- For installations where the table existed pre-2026 with a partial schema,
-- backfill any missing columns. These are no-ops on a fresh table.
alter table public.leaderboard add column if not exists score      int;
alter table public.leaderboard add column if not exists grade      text;
alter table public.leaderboard add column if not exists streak     int;
alter table public.leaderboard add column if not exists delivered  int;
alter table public.leaderboard add column if not exists time_secs  int;
alter table public.leaderboard add column if not exists run_id     uuid;
alter table public.leaderboard add column if not exists created_at timestamptz default now();

-- Ensure RLS state matches the hardening migration's intent: public SELECT,
-- no public writes.
alter table public.leaderboard enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public' and tablename = 'leaderboard' and policyname = 'lb_read'
    ) then
        execute 'create policy "lb_read" on public.leaderboard for select using (true)';
    end if;
end$$;

-- Make sure the email-uniqueness constraint exists. If a primary key on email
-- already covers it, this is redundant but harmless.
do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'leaderboard_email_uniq'
    ) and not exists (
        select 1 from pg_constraint
        where conrelid = 'public.leaderboard'::regclass and contype = 'p'
    ) then
        execute 'alter table public.leaderboard add constraint leaderboard_email_uniq unique (email)';
    end if;
end$$;

-- Same defensive treatment for run_tokens.
create table if not exists public.run_tokens (
    id         uuid primary key default gen_random_uuid(),
    issued_at  timestamptz not null default now(),
    used_at    timestamptz,
    client_ip  text
);
alter table public.run_tokens add column if not exists issued_at  timestamptz default now();
alter table public.run_tokens add column if not exists used_at    timestamptz;
alter table public.run_tokens add column if not exists client_ip  text;
alter table public.run_tokens enable row level security;

create index if not exists run_tokens_issued_idx on public.run_tokens (issued_at);
