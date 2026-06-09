// GET /api/auth-check
// Proxies the HT6 session check server-side so the browser never makes a
// cross-origin request to v2.api.hackthe6ix.com (which has no CORS allowance
// for our origin).

import { json, verifyHt6Session } from './_lib.js';

export default async function handler(req, res) {
    if (req.method !== 'GET') return json(res, 405, { error: 'method_not_allowed' });

    const auth = await verifyHt6Session(req);
    if (!auth.ok) {
        return json(res, auth.status === 401 ? 401 : auth.status, { error: 'unauthenticated' });
    }

    return json(res, 200, auth.user ?? {});
}
