-- Anti-cheat hardening — Tier A (fixes #1, #2, #3).
--
-- Fix #1: Bind the claimed time_secs to the real token age.
--   The submit_run RPC now accepts p_time_slack_ms (default 15 s) and rejects
--   any submission whose claimed duration exceeds the token's actual elapsed
--   wall-clock time plus that slack.
--
-- Fix #3: Bind run token to HT6 identity at submit time.
--   run_tokens.ht6_user_id is set on the first submit with a known identity
--   and compared on any subsequent use of the same token, preventing one
--   identity from burning tokens issued to another.
--
-- Fix #2 (plausibility caps) lives entirely in api/submit-score.js; no schema
-- change is required.

-- Fix #3 schema: add ht6_user_id to run_tokens.
alter table public.run_tokens
    add column if not exists ht6_user_id text;

-- Drop the previous signature so we can replace the function with an updated
-- parameter list (CREATE OR REPLACE requires an identical signature).
drop function if exists public.submit_run(uuid, text, int, text, int, int, int, text, text, text, int, int, int);

create function public.submit_run(
    p_run_id         uuid,
    p_email          text,
    p_score          int,
    p_grade          text,
    p_streak         int,
    p_delivered      int,
    p_time_secs      int,
    p_ht6_user_id    text    default null,
    p_first_name     text    default null,
    p_last_name      text    default null,
    p_min_age_ms     int     default 10000,
    p_max_age_ms     int     default 3900000,  -- 65 min
    p_rate_limit_ms  int     default 30000,
    p_time_slack_ms  int     default 15000     -- Fix #1: clock/network skew budget (ms)
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
    select id, issued_at, used_at, ht6_user_id  -- Fix #3: also fetch ht6_user_id
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

    -- Fix #1: Reject when the claimed run duration exceeds real elapsed time.
    -- p_time_slack_ms absorbs network round-trip and client/server clock skew.
    if (p_time_secs::bigint * 1000) > (v_age_ms + p_time_slack_ms) then
        return jsonb_build_object('ok', false, 'error', 'time_exceeds_elapsed');
    end if;

    -- Fix #3: Enforce HT6 identity binding on the token.
    -- Only check when BOTH the stored value and the submitted value are non-null;
    -- p_ht6_user_id may be null if HT6 did not return one for this session.
    if v_token.ht6_user_id is not null
       and p_ht6_user_id is not null
       and v_token.ht6_user_id <> p_ht6_user_id then
        return jsonb_build_object('ok', false, 'error', 'token_foreign');
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
    --    Fix #3: stamp ht6_user_id on the token row (coalesce keeps any value
    --    already stored and sets it for the first time when it was null).
    update public.run_tokens
    set used_at     = now(),
        ht6_user_id = coalesce(ht6_user_id, p_ht6_user_id)
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

-- Re-issue revoke/grant with the NEW signature (14 params).
revoke all on function public.submit_run(uuid,text,int,text,int,int,int,text,text,text,int,int,int,int) from public;
revoke all on function public.submit_run(uuid,text,int,text,int,int,int,text,text,text,int,int,int,int) from anon;
revoke all on function public.submit_run(uuid,text,int,text,int,int,int,text,text,text,int,int,int,int) from authenticated;
grant execute on function public.submit_run(uuid,text,int,text,int,int,int,text,text,text,int,int,int,int) to service_role;
