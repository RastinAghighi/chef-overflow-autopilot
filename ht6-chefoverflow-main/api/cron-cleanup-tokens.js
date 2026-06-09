// GET /api/cron-cleanup-tokens
// Deletes run_tokens older than 2 hours. Intended to be invoked by Vercel
// Cron. Gated by CRON_SECRET (Vercel sets Authorization: Bearer <secret>
// on scheduled invocations). Safe to run repeatedly; safe to skip a run.

import { db, json } from './_lib.js';

export default async function handler(req, res) {
    const expected = process.env.CRON_SECRET;
    const auth = req.headers['authorization'] || '';
    const provided = auth.startsWith('Bearer ') ? auth.slice(7) : null;
    if (!expected || provided !== expected) {
        return json(res, 404, { error: 'not_found' });
    }

    let supabase;
    try { supabase = db(); }
    catch (_) { return json(res, 500, { error: 'server_misconfigured' }); }

    const cutoff = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
    const { error, count } = await supabase
        .from('run_tokens')
        .delete({ count: 'exact' })
        .lt('issued_at', cutoff);

    if (error) return json(res, 500, { error: 'delete_failed', detail: error.message });
    return json(res, 200, { ok: true, deleted: count ?? null, cutoff });
}
