-- PII protection: anon must not be able to scrape participant emails.
-- Expose a view with masked emails only; revoke direct SELECT on the base
-- table from anon/authenticated. service_role keeps full access (it
-- bypasses RLS and grants).

create or replace view public.leaderboard_public as
select
    score,
    grade,
    streak,
    delivered,
    time_secs,
    substr(email, 1, 1) || '***@' || split_part(email, '@', 2) as masked_email,
    created_at
from public.leaderboard;

-- Views in Postgres run as the view owner's privileges by default. We want
-- the view to run with the *invoker's* privileges so RLS on leaderboard
-- still applies — but since we're also revoking direct SELECT on the base
-- table, we keep the simpler model: anon reads the view, the view reads
-- the table as the (definer) owner. The base table's lb_read policy
-- already allows SELECT, so this is safe either way.
grant select on public.leaderboard_public to anon, authenticated;

-- Take direct table SELECT away from non-service roles. The lb_read policy
-- remains as a defensive belt; the grant revoke is what actually blocks
-- anon scraping at the PostgREST layer.
revoke select on public.leaderboard from anon, authenticated;
