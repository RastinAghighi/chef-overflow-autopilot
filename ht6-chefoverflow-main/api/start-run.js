// POST /api/start-run
// Issues a single-use HMAC-signed run token at game start.
// HT6 session is NOT required to start a run — only to submit a score.
// We bind the token to the IP at issuance time for a soft tamper signal.

import { db, hmacToken, json, readJsonBody } from './_lib.js';

// Cap a single IP at N active tokens/minute. Without this, anonymous clients
// can pile rows into run_tokens unbounded and inflate DB cost. Kept generous
// because legit players at a shared venue (hackathon Wi-Fi, office NAT) all
// egress from one IP — a low cap silently 429s real players, who then complete
// a full run with no token and only discover it at submit time.
const PER_IP_LIMIT = 200;
const PER_IP_WINDOW_MS = 60 * 1000;

export default async function handler(req, res) {
    if (req.method !== 'POST') return json(res, 405, { error: 'method_not_allowed' });

    // Body is optional; we accept it for forward compatibility.
    try { await readJsonBody(req); } catch (_) { /* ignore */ }

    const ip = (req.headers['x-forwarded-for'] || '').split(',')[0].trim() || null;

    let supabase;
    try { supabase = db(); }
    catch (_) { return json(res, 500, { error: 'server_misconfigured' }); }

    if (ip) {
        const since = new Date(Date.now() - PER_IP_WINDOW_MS).toISOString();
        const recent = await supabase
            .from('run_tokens')
            .select('id', { count: 'exact', head: true })
            .eq('client_ip', ip)
            .gte('issued_at', since);
        if (!recent.error && typeof recent.count === 'number' && recent.count >= PER_IP_LIMIT) {
            return json(res, 429, { error: 'rate_limited' });
        }
    }

    const { data, error } = await supabase
        .from('run_tokens')
        .insert({ client_ip: ip })
        .select('id, issued_at')
        .single();
    if (error || !data) return json(res, 500, { error: 'issue_failed' });

    let token;
    try { token = hmacToken(`${data.id}.${data.issued_at}`); }
    catch (_) { return json(res, 500, { error: 'server_misconfigured' }); }

    return json(res, 200, { run_id: data.id, issued_at: data.issued_at, token });
}
