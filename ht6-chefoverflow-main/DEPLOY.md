# Deploy

Deployed at https://chefoverflow.hackthe6ix.com (Vercel, custom domain).

## TL;DR — what makes it work

1. **Subdomain of `hackthe6ix.com`** so the HT6 session cookie reaches our `/api/*` functions.
2. **Three Vercel env vars** + an optional fourth.
3. **Two Supabase migrations** applied to the database.
4. **Hit `/api/health` after every deploy** — the diagnostic endpoint will tell you exactly what's misconfigured.

## 1. Topology

The site **must** be served from a subdomain of `hackthe6ix.com` (currently `chefoverflow.hackthe6ix.com`). The HT6 session cookie is scoped to `.hackthe6ix.com`; serving from any other parent domain (including `hackthe6ix.ca`) means the cookie never reaches the `/api/*` functions and server-side auth verification breaks.

- Ask HT6 ops to point `chefoverflow.hackthe6ix.com` CNAME → `cname.vercel-dns.com`.
- Add `chefoverflow.hackthe6ix.com` as a custom domain in Vercel. Vercel auto-issues TLS.
- Verify in DevTools → Application → Cookies that the HT6 cookie has `Domain=.hackthe6ix.com`. If it's host-only (`Domain=v2.api.hackthe6ix.com`), HT6 needs to widen it — without that, no design works.

## 2. Vercel env vars (Settings → Environment Variables)

| Name | Required? | Used by |
|---|---|---|
| `SUPABASE_URL` | yes | `/api/start-run`, `/api/submit-score`, `/api/health` |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | same — server-only, never expose to the browser |
| `RUN_TOKEN_SECRET` | yes | HMAC for run tokens. Long random string (e.g. `openssl rand -hex 32`). Keep stable across deploys or in-flight tokens will fail. |
| `HEALTH_TOKEN` | yes | Shared secret to gate `/api/health`. Set to any long random string. Requests without `x-health-token: <value>` get 404. |
| `CRON_SECRET` | yes | Shared secret for the Vercel cron at `/api/cron-cleanup-tokens`. Vercel sets `Authorization: Bearer <value>` on scheduled invocations. |
| `PLAUSIBILITY_MODE` | no | `strict` (default) or `log`. `strict` rejects over-cap scores; `log` accepts but logs them (for tuning caps from real-run telemetry). **Leaving it unset enforces.** |
| `REPLAY_MODE` | no | `strict` (default) rejects any submission whose server-side replay doesn't match the claimed score — this is the core anti-cheat. `shadow` runs the replay but only logs mismatches (tuning). `off` skips replay entirely. **Leaving it unset enforces;** only set `shadow`/`off` deliberately. |
| `TRAVEL_CHECK` | no | Anti-teleport layer on top of the replay. `enforce` (default) rejects runs whose recorded chef movement is physically impossible (teleporting between distant stations) — catches crafted logs that the score-match check can't. `log` logs violations without rejecting (rollout/tuning). `off` skips it. Only takes effect when `REPLAY_MODE=strict`. **Leaving it unset enforces.** |
| `BEHAVIOR_CHECK` | no | Human-realism heuristics on the replayed input log — looks for offline-solver fingerprints (fixed-cadence gaps, hops pinned to the travel bound, superhuman multi-chef parallelism). **Default is `log`, NOT enforce:** these are statistical signals with false-positive risk, so they ship non-enforcing and are tuned from `log`-mode telemetry before flipping to `enforce` (any flag → `behavior_implausible`). `off` skips them. Only meaningful when `REPLAY_MODE != off`. |
| `HT6_API_URL` | no | Defaults to `https://v2.api.hackthe6ix.com` |

The browser bundle also needs `SUPABASE_URL` and `SUPABASE_ANON_KEY` inline in `index.html` (already there). The anon key is only used for public SELECTs on the leaderboard.

## 3. Supabase migrations

Apply in order from `supabase/migrations/`:

1. `20260521090237_leaderboard_hardening.sql` — enables RLS, locks public writes, adds run_tokens.
2. `20260526120000_submit_run_rpc.sql` — creates the atomic `submit_run` function. **Most likely missing piece if submit fails with `rpc_missing`.**
3. `20260527130000_leaderboard_schema_guard.sql` — idempotent guard that ensures the leaderboard and run_tokens tables have every column the server needs. Safe to run on existing DBs.
4. `20260528000000_leaderboard_public_view.sql` — creates `public.leaderboard_public` (masked emails) and revokes direct SELECT on the base table from `anon`/`authenticated`. The browser reads the view; service-role reads/writes go through the base table as before.
5. `20260528010000_run_tokens_hygiene.sql` — adds the `(client_ip, issued_at)` index for the per-IP rate limit and (if `pg_cron` is available) schedules a 15-minute cleanup of stale tokens.

Easiest application: Supabase Dashboard → SQL Editor → paste each file, run. Order matters for #1 only; #2 and #3 can be applied in either order, and both are idempotent.

## 4. After deploy: hit `/api/health`

`https://chefoverflow.hackthe6ix.com/api/health` runs every check this app depends on and reports green/red per-check. It returns HTTP 200 if all green, 503 otherwise. Sample shape:

```json
{
  "ok": false,
  "env": { "SUPABASE_URL": true, "SUPABASE_SERVICE_ROLE_KEY": true, "RUN_TOKEN_SECRET": true, "HT6_API_URL": "https://v2.api.hackthe6ix.com" },
  "checks": [
    { "name": "env_supabase_url", "ok": true },
    { "name": "env_service_role", "ok": true },
    { "name": "env_run_token_secret", "ok": true },
    { "name": "table_run_tokens", "ok": true, "detail": { "row_count": 47 } },
    { "name": "table_leaderboard", "ok": true, "detail": { "row_count": 3 } },
    { "name": "rpc_submit_run", "ok": false, "error": "submit_run function not deployed (run the 20260526120000_submit_run_rpc.sql migration)" },
    { "name": "ht6_reachable", "ok": true, "detail": { "status": 401 } }
  ]
}
```

Each red row tells you precisely what to fix.

## 5. After deploy: smoke-test the submit path

While signed in to HT6 on the deployed site:

1. Play for at least 10 seconds, then let the run end (or trigger game-over).
2. Click Submit.
3. Open DevTools Console — every rejection logs `[ht6] submit-score rejected { status, reason, data }` with the full server response.
4. The UI also shows the reason code in the status text.

Common reasons and what they mean:

| Reason | What's happening |
|---|---|
| `ht6_unauthenticated` | HT6 session not present / expired. Sign in again. |
| `ht6_unreachable` | HT6 API timed out or 5xx'd. Retry. |
| `rpc_missing` | The `submit_run` migration hasn't been applied. See section 3. |
| `table_missing` / `column_missing` | The leaderboard table has the wrong schema. Apply migration #3. |
| `too_fast` | Run was shorter than 10 s. Play longer. |
| `token_expired` | More than 65 min between game start and submit. Play a new game. |
| `rate_limited` (from start-run) | More than 10 game starts from one IP in a minute. |
| `token_used` | This run has already been submitted. Play a new game. |
| `bad_signature` | HMAC mismatch. Most often means `RUN_TOKEN_SECRET` was changed between issuing the token and submitting. Reset and start a new run. |
| `implausible_score` / `_delivered` / `_streak` | Plausibility caps tripped. Either an honest bug in the game's scoring or a tampered client. |
| `replay_mismatch` | Server replay recomputed a different score than the client claimed. Tampered payload or a client/server sim-version skew. |
| `replay_unreachable` | Recorded chef movement was physically impossible (teleporting). Crafted input log, or a sim-version skew. |
| `behavior_implausible` | The input log matched offline-solver heuristics (see `BEHAVIOR_CHECK`). Only fires when `BEHAVIOR_CHECK=enforce`; in the default `log` mode this is logged as `would_reject_behavior` instead. |
| `rate_limited` | Same email submitted within the last 30 s. |

Vercel function logs (Vercel Dashboard → Project → Logs) also include the structured `[submit-score] rejected` log line for every rejection, with the relevant fields for that reason.

## 6. Auth flow at runtime

1. Browser loads page → calls **same-origin** `GET /api/auth-check` → that endpoint forwards the inbound `Cookie:` header to `https://v2.api.hackthe6ix.com/api/auth/check`. 200 means signed in. The browser never makes a cross-origin call to v2.api, so no CORS allowlist is needed.
2. Sign-in click → full-page navigate to `${HT6_API_URL}/api/auth/login?redirectUrl=<current page>`. HT6 handles OAuth, sets the session cookie on `.hackthe6ix.com` during callback, redirects back.
3. Game starts → `POST /api/start-run` issues an HMAC run token.
4. Game over → user clicks Submit → `POST /api/submit-score`. Vercel function re-verifies HT6 session (same forward-cookie pattern), validates HMAC + plausibility, calls `submit_run` RPC for the atomic claim + upsert.

## 7. Hitting `/api/health` post-deploy

`/api/health` is now gated. To call it:

```bash
curl -H 'x-health-token: <HEALTH_TOKEN>' https://chefoverflow.hackthe6ix.com/api/health
```

Without the header, the endpoint returns 404 by design — it shouldn't advertise itself to scanners.
