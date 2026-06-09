-- Add HT6 user identity columns to the leaderboard.
--
-- ht6_user_id: stable UUID from HT6's Cognito-backed user table. Used as a
-- secondary dedup guard — email is still the primary key (now always trusted
-- from HT6's /api/users/{userId}), but ht6_user_id prevents the same Cognito
-- account from appearing twice under different emails. Existing rows have no
-- known HT6 user ID so the column is nullable; the partial unique index
-- enforces uniqueness only among non-null values.
--
-- first_name / last_name: display names from HT6's profile, shown on the
-- leaderboard in place of the masked email.

alter table public.leaderboard
    add column if not exists ht6_user_id text,
    add column if not exists first_name  text,
    add column if not exists last_name   text;

create unique index if not exists leaderboard_ht6_user_id_uniq
    on public.leaderboard (ht6_user_id)
    where ht6_user_id is not null;

-- Drop the old signature (parameter list changed; CREATE OR REPLACE requires
-- an identical signature to replace in-place).
drop function if exists public.submit_run(uuid, text, int, text, int, int, int, int, int, int);

create function public.submit_run(
    p_run_id        uuid,
    p_email         text,
    p_score         int,
    p_grade         text,
    p_streak        int,
    p_delivered     int,
    p_time_secs     int,
    p_ht6_user_id   text    default null,
    p_first_name    text    default null,
    p_last_name     text    default null,
    p_min_age_ms    int     default 10000,
    p_max_age_ms    int     default 3900000,  -- 65 min
    p_rate_limit_ms int     default 30000
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_email    text := lower(p_email);
    v_token    record;
    v_age_ms   bigint;
    v_existing record;
begin
    -- 1) Lock the token row. Concurrent submits with the same run_id serialize here.
    select id, issued_at, used_at
    into v_token
    from public.run_tokens
    where id = p_run_id
    for update;

    if not found then
        return jsonb_build_object('ok', false, 'error', 'unknown_token');
    end if;
    if v_token.used_at is not null then
        return jsonb_build_object('ok', false, 'error', 'token_used');
    end if;

    v_age_ms := (extract(epoch from (now() - v_token.issued_at)) * 1000)::bigint;
    if v_age_ms > p_max_age_ms then
        return jsonb_build_object('ok', false, 'error', 'token_expired');
    end if;
    if v_age_ms < p_min_age_ms then
        return jsonb_build_object('ok', false, 'error', 'too_fast');
    end if;

    -- 2) Lock the leaderboard row for this email (if any). Concurrent submits
    --    for the same email serialize here so the best-score check is race-free.
    select score, created_at
    into v_existing
    from public.leaderboard
    where email = v_email
    for update;

    if found then
        if (extract(epoch from (now() - v_existing.created_at)) * 1000)::bigint < p_rate_limit_ms then
            return jsonb_build_object('ok', false, 'error', 'rate_limited');
        end if;
    end if;

    -- 3) Claim the token. Spent regardless of whether the new score wins.
    update public.run_tokens
    set used_at = now()
    where id = p_run_id;

    -- 4) Best-only upsert.
    if found and v_existing.score >= p_score then
        return jsonb_build_object('ok', true, 'kept_existing', true, 'best', v_existing.score);
    end if;

    insert into public.leaderboard
        (email, score, grade, streak, delivered, time_secs, run_id, created_at,
         ht6_user_id, first_name, last_name)
    values
        (v_email, p_score, p_grade, p_streak, p_delivered, p_time_secs, p_run_id, now(),
         p_ht6_user_id, p_first_name, p_last_name)
    on conflict (email) do update set
        score       = excluded.score,
        grade       = excluded.grade,
        streak      = excluded.streak,
        delivered   = excluded.delivered,
        time_secs   = excluded.time_secs,
        run_id      = excluded.run_id,
        created_at  = excluded.created_at,
        ht6_user_id = coalesce(excluded.ht6_user_id, public.leaderboard.ht6_user_id),
        first_name  = coalesce(excluded.first_name,  public.leaderboard.first_name),
        last_name   = coalesce(excluded.last_name,   public.leaderboard.last_name);

    return jsonb_build_object('ok', true, 'best', p_score);
end;
$$;

revoke all on function public.submit_run(uuid,text,int,text,int,int,int,text,text,text,int,int,int) from public;
revoke all on function public.submit_run(uuid,text,int,text,int,int,int,text,text,text,int,int,int) from anon;
revoke all on function public.submit_run(uuid,text,int,text,int,int,int,text,text,text,int,int,int) from authenticated;
grant execute on function public.submit_run(uuid,text,int,text,int,int,int,text,text,text,int,int,int) to service_role;

-- Update the public view: show first + last name when available, fall back to
-- the masked email for legacy rows that predate this migration.
-- Must drop + recreate because CREATE OR REPLACE cannot rename columns.
drop view if exists public.leaderboard_public;
create view public.leaderboard_public as
select
    score,
    grade,
    streak,
    delivered,
    time_secs,
    coalesce(
        nullif(trim(coalesce(first_name, '') || ' ' || coalesce(last_name, '')), ''),
        substr(email, 1, 1) || '***@' || split_part(email, '@', 2)
    ) as display_name,
    created_at
from public.leaderboard;

grant select on public.leaderboard_public to anon, authenticated;
