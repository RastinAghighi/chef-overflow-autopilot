-- Hygiene for run_tokens:
--   - Index for the per-IP rate-limit lookup in /api/start-run.
--   - Optional pg_cron job to age out stale tokens. If pg_cron isn't
--     enabled on this project, the Vercel cron at /api/cron-cleanup-tokens
--     covers the same DELETE.

create index if not exists run_tokens_ip_issued_idx
    on public.run_tokens (client_ip, issued_at);

do $$
begin
    if exists (select 1 from pg_extension where extname = 'pg_cron') then
        perform cron.schedule(
            'run-tokens-cleanup',
            '*/15 * * * *',
            $cmd$ delete from public.run_tokens where issued_at < now() - interval '2 hours' $cmd$
        );
    end if;
exception when others then
    -- pg_cron not installed or unschedule fails: ignore. The Vercel cron
    -- handler is the fallback.
    null;
end$$;
