// GET /api/health
// Diagnostic endpoint. Reports the state of every external dependency the
// scoring pipeline needs. Hit this in a browser to see exactly what's
// misconfigured without grovelling through Vercel logs.
//
// Public, but only returns yes/no signals (no secrets, no IPs, no PII).

import { createClient } from '@supabase/supabase-js';

const HT6_API_URL = process.env.HT6_API_URL || 'https://v2.api.hackthe6ix.com';

function summary(req) {
    return {
        env: {
            SUPABASE_URL: !!process.env.SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY: !!process.env.SUPABASE_SERVICE_ROLE_KEY,
            RUN_TOKEN_SECRET: !!process.env.RUN_TOKEN_SECRET,
            HT6_API_URL,
        },
        request: {
            host: req.headers.host || null,
            has_cookie: !!req.headers.cookie,
            cookie_size: req.headers.cookie ? req.headers.cookie.length : 0,
        },
    };
}

async function check(name, fn) {
    try {
        const detail = await fn();
        return { name, ok: true, ...(detail ? { detail } : {}) };
    } catch (err) {
        return { name, ok: false, error: err?.message || String(err) };
    }
}

export default async function handler(req, res) {
    if (req.method !== 'GET') {
        res.statusCode = 405;
        res.setHeader('content-type', 'application/json');
        return res.end(JSON.stringify({ error: 'method_not_allowed' }));
    }

    // Gate: require a shared secret. Return 404 (not 401) so the endpoint
    // doesn't advertise itself to scanners.
    const expected = process.env.HEALTH_TOKEN;
    const provided = req.headers['x-health-token'];
    if (!expected || provided !== expected) {
        res.statusCode = 404;
        res.setHeader('content-type', 'application/json');
        return res.end(JSON.stringify({ error: 'not_found' }));
    }

    const checks = [];

    // 1) Env
    checks.push(await check('env_supabase_url', () => {
        if (!process.env.SUPABASE_URL) throw new Error('SUPABASE_URL not set');
        return null;
    }));
    checks.push(await check('env_service_role', () => {
        if (!process.env.SUPABASE_SERVICE_ROLE_KEY) throw new Error('SUPABASE_SERVICE_ROLE_KEY not set');
        return null;
    }));
    checks.push(await check('env_run_token_secret', () => {
        if (!process.env.RUN_TOKEN_SECRET) throw new Error('RUN_TOKEN_SECRET not set');
        return null;
    }));

    // 2) Supabase connectivity + tables
    let supabase = null;
    if (process.env.SUPABASE_URL && process.env.SUPABASE_SERVICE_ROLE_KEY) {
        supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_SERVICE_ROLE_KEY, {
            auth: { persistSession: false, autoRefreshToken: false },
        });
    }

    checks.push(await check('table_run_tokens', async () => {
        if (!supabase) throw new Error('supabase client not initialized');
        const { error, count } = await supabase.from('run_tokens').select('*', { count: 'exact', head: true });
        if (error) throw new Error(`${error.code || ''} ${error.message}`.trim());
        return { row_count: count ?? null };
    }));

    checks.push(await check('table_leaderboard', async () => {
        if (!supabase) throw new Error('supabase client not initialized');
        // SELECT the columns the RPC inserts into. If any column is missing,
        // PostgREST returns 400 with the offending name.
        const { error, count } = await supabase
            .from('leaderboard')
            .select('email, score, grade, streak, delivered, time_secs, run_id, created_at', { count: 'exact', head: true });
        if (error) throw new Error(`${error.code || ''} ${error.message}`.trim());
        return { row_count: count ?? null };
    }));

    checks.push(await check('rpc_submit_run', async () => {
        if (!supabase) throw new Error('supabase client not initialized');
        // Invoke with a never-matching run_id. The function should return
        // { ok: false, error: 'unknown_token' } — proves it exists and is
        // callable. A 42883 here means the function was never created.
        const { data, error } = await supabase.rpc('submit_run', {
            p_run_id: '00000000-0000-0000-0000-000000000000',
            p_email: 'healthcheck@example.com',
            p_score: 0,
            p_grade: 'F',
            p_streak: 0,
            p_delivered: 0,
            p_time_secs: 10,
        });
        if (error) {
            if (error.code === '42883') throw new Error('submit_run function not deployed (run the 20260526120000_submit_run_rpc.sql migration)');
            throw new Error(`${error.code || ''} ${error.message}`.trim());
        }
        return { returns: data?.error || 'ok' };
    }));

    // 3) HT6 upstream reachability (no cookies — just confirms the host responds)
    checks.push(await check('ht6_reachable', async () => {
        const r = await fetch(`${HT6_API_URL}/api/auth/check`, {
            method: 'GET',
            signal: AbortSignal.timeout(3000),
        });
        // 401 is the expected response when no cookie is sent — proves the
        // endpoint is responding, even if no session is active.
        if (r.status !== 200 && r.status !== 401) {
            throw new Error(`unexpected status ${r.status}`);
        }
        return { status: r.status };
    }));

    // 4) Cookie forwarding sanity (only when caller is signed in)
    if (req.headers.cookie) {
        checks.push(await check('ht6_session_for_caller', async () => {
            const r = await fetch(`${HT6_API_URL}/api/auth/check`, {
                method: 'GET',
                headers: { cookie: req.headers.cookie, accept: 'application/json' },
                signal: AbortSignal.timeout(3000),
            });
            return { status: r.status, signed_in: r.ok };
        }));
    }

    const allOk = checks.every(c => c.ok);
    res.statusCode = allOk ? 200 : 503;
    res.setHeader('content-type', 'application/json');
    res.setHeader('cache-control', 'no-store');
    res.end(JSON.stringify({
        ok: allOk,
        ...summary(req),
        checks,
    }, null, 2));
}
