// Shared helpers for the /api functions.
// Runs on Vercel's Node.js runtime. These endpoints assume the site is hosted
// on a subdomain of hackthe6ix.com so that the HT6 session cookie reaches us.

import { createClient } from '@supabase/supabase-js';
import crypto from 'node:crypto';

export const HT6_API_URL = process.env.HT6_API_URL || 'https://v2.api.hackthe6ix.com';

const SUPABASE_URL = process.env.SUPABASE_URL;
const SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;

export function db() {
    if (!SUPABASE_URL || !SERVICE_ROLE_KEY) {
        throw new Error('missing_supabase_env');
    }
    return createClient(SUPABASE_URL, SERVICE_ROLE_KEY, {
        auth: { persistSession: false, autoRefreshToken: false },
    });
}

export function json(res, status, body) {
    res.statusCode = status;
    res.setHeader('content-type', 'application/json');
    // Same-origin: no CORS headers needed.
    res.end(JSON.stringify(body));
}

/**
 * Read a JSON body in a way that works on both Vercel (which pre-parses
 * application/json into req.body and drains the stream) and any other Node
 * runtime where the body is still a readable stream.
 *
 * @param {import('http').IncomingMessage} req
 * @param {{ maxBytes?: number }} [opts]  — maxBytes defaults to 16 KiB.
 *   Pass a larger value (e.g. 512 * 1024) for endpoints that accept input logs.
 */
export async function readJsonBody(req, opts) {
    const maxBytes = (opts && opts.maxBytes) ? opts.maxBytes : 16 * 1024;

    // Vercel's @vercel/node runtime parses JSON bodies for us. If it already
    // ran, req.body is the parsed object (or string / Buffer for other types).
    const pre = req.body;
    if (pre !== undefined && pre !== null) {
        if (typeof pre === 'object' && !Buffer.isBuffer(pre)) {
            // Pre-parsed: check serialised size as a rough proxy for byte length.
            // This is an approximation — the actual wire bytes may differ — but it
            // is good enough to block absurdly large payloads that sneak through
            // Vercel's body parser before we can check them.
            const serialised = JSON.stringify(pre);
            if (serialised.length > maxBytes) throw new Error('payload_too_large');
            return pre;
        }
        const text = Buffer.isBuffer(pre) ? pre.toString('utf8') : String(pre);
        if (!text) return {};
        if (text.length > maxBytes) throw new Error('payload_too_large');
        try { return JSON.parse(text); } catch (_) { throw new Error('bad_json'); }
    }

    // Fallback: drain the request stream manually.
    return await new Promise((resolve, reject) => {
        let buf = '';
        req.on('data', (chunk) => {
            buf += chunk;
            if (buf.length > maxBytes) reject(new Error('payload_too_large'));
        });
        req.on('end', () => {
            if (!buf) return resolve({});
            try { resolve(JSON.parse(buf)); } catch (_) { reject(new Error('bad_json')); }
        });
        req.on('error', reject);
    });
}

function b64url(buf) {
    return Buffer.from(buf).toString('base64')
        .replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');
}

export function hmacToken(message) {
    const secret = process.env.RUN_TOKEN_SECRET;
    if (!secret) throw new Error('missing_run_token_secret');
    return b64url(crypto.createHmac('sha256', secret).update(message).digest());
}

export function timingSafeEqual(a, b) {
    if (typeof a !== 'string' || typeof b !== 'string') return false;
    const ab = Buffer.from(a);
    const bb = Buffer.from(b);
    if (ab.length !== bb.length) return false;
    return crypto.timingSafeEqual(ab, bb);
}

const AUTH_CHECK_TIMEOUT_MS = 3000;
/**
 * Verify the HT6 session and resolve the authenticated user's profile.
 *
 * Single source of truth: GET /api/users/me returns the CURRENT caller's own
 * user record, e.g.
 *   { userId, email, firstName, lastName, roles, ... }
 * This is the only correct way to identify the caller.
 *
 * History/why: we used to derive the userId from
 * `/api/seasons/{code}/responses` -> data[0].userId. That endpoint is NOT
 * scoped to the caller for privileged accounts (organizers/admins see ALL
 * applicants), so data[0] was a *random other applicant* — we then fetched and
 * trusted that person's email/name. That impersonation bug is why this now
 * goes straight to /api/users/me.
 *
 * Returns { ok: boolean, status, user? } where user has { email, userId,
 * firstName, lastName } drawn entirely from HT6's database — never from the
 * client request body.
 *   401 -> signed out;  403 -> signed in but not permitted;
 *   502/504 -> upstream error/timeout.
 */
export async function verifyHt6Session(req) {
    const cookie = req.headers['cookie'];
    if (!cookie) return { ok: false, status: 401 };

    const ht6Headers = {
        cookie,
        accept: 'application/json',
        'user-agent': 'chef-overflow-server/1.0',
    };

    let body;
    try {
        const res = await fetch(`${HT6_API_URL}/api/users/me`, {
            method: 'GET',
            headers: ht6Headers,
            signal: AbortSignal.timeout(AUTH_CHECK_TIMEOUT_MS),
        });
        if (res.status === 401) return { ok: false, status: 401 };
        if (res.status === 403) return { ok: false, status: 403 };
        if (!res.ok) return { ok: false, status: 502 };
        body = await res.json();
    } catch (err) {
        const status = err?.name === 'TimeoutError' ? 504 : 502;
        return { ok: false, status };
    }

    // /api/users/me returns the user object directly; unwrap defensively in case
    // an envelope ({ message } / { data }) is ever added upstream.
    let user = body?.message ?? body?.data ?? body;
    if (Array.isArray(user)) user = user[0];
    if (!user || typeof user.email !== 'string') return { ok: false, status: 502 };

    return {
        ok: true,
        status: 200,
        user: {
            email:     user.email,
            userId:    user.userId ?? user._id ?? null,
            firstName: user.firstName ?? null,
            lastName:  user.lastName  ?? null,
        },
    };
}
