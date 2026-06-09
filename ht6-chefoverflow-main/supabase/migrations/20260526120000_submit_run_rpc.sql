-- Atomic score submission: locks the token + leaderboard-row-for-email,
-- validates token age, enforces per-email rate limit, claims the token,
-- and does a best-only upsert. All in one transaction so neither the
-- best-score race nor a partial-failure burned-token can occur.

create or replace function public.submit_run(
    p_run_id        uuid,
    p_email         text,
    p_score         int,
    p_grade         text,
    p_streak        int,
    p_delivered     int,
    p_time_secs     int,
    p_min_age_ms    int default 10000,
    p_max_age_ms    int default 1800000,   -- 30 min
    p_rate_limit_ms int default 30000
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
    --    for the same email serialize here, so the best-score check sees the
    --    latest committed value.
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

    -- 3) Claim the token. From here on, the token is spent regardless of
    --    whether the new score wins, matching at-most-once semantics.
    update public.run_tokens
    set used_at = now()
    where id = p_run_id;

    -- 4) Best-only upsert. If the existing score wins, leave the row alone.
    if found and v_existing.score >= p_score then
        return jsonb_build_object('ok', true, 'kept_existing', true, 'best', v_existing.score);
    end if;

    insert into public.leaderboard
        (email, score, grade, streak, delivered, time_secs, run_id, created_at)
    values
        (v_email, p_score, p_grade, p_streak, p_delivered, p_time_secs, p_run_id, now())
    on conflict (email) do update set
        score      = excluded.score,
        grade      = excluded.grade,
        streak     = excluded.streak,
        delivered  = excluded.delivered,
        time_secs  = excluded.time_secs,
        run_id     = excluded.run_id,
        created_at = excluded.created_at;

    return jsonb_build_object('ok', true, 'best', p_score);
end;
$$;

-- Only the service role should be able to call this. Anon must go nowhere near it.
revoke all on function public.submit_run(uuid, text, int, text, int, int, int, int, int, int) from public;
revoke all on function public.submit_run(uuid, text, int, text, int, int, int, int, int, int) from anon;
revoke all on function public.submit_run(uuid, text, int, text, int, int, int, int, int, int) from authenticated;
grant execute on function public.submit_run(uuid, text, int, text, int, int, int, int, int, int) to service_role;
