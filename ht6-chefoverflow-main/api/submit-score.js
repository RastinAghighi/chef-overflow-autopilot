// POST /api/submit-score
//
// Server-side trust chain:
//   1. Forward inbound Cookie header to HT6 /api/auth/check. Reject on non-200.
//   2. Validate payload schema and HMAC signature on the run token.
//   3. Recompute plausibility caps (per-second score, delivered, streak).
//   4. (B4) Server replay: decode input log and run simulate() to verify score.
//   5. Hand off to the submit_run RPC, which atomically locks the token row
//      and the leaderboard row for this email, enforces age + rate limit,
//      claims the token, and does a best-only upsert.
//
// If HT6 returns user info in /auth/check, the trusted email comes from
// there; otherwise we accept the client-typed email (still gated by the
// HT6 session being live).

import { db, hmacToken, json, readJsonBody, timingSafeEqual, verifyHt6Session } from './_lib.js';
import { simulate, defaultConfig, getCanonicalCounterIds } from '../sim/core.js';
import { seedFromRunId } from '../sim/prng.js';
import { buildStationTable, decodeInputLog } from '../sim/inputlog.js';
import { analyzeBehavior } from '../sim/behavior.js';

// Fix #2: Lowered from 250 to 150 (interim value). The definitive number should
// come from real-run telemetry collected with PLAUSIBILITY_MODE=log. With replay
// validation (Tier B) enforced, this cap becomes a cheap pre-filter that rejects
// garbage before the more expensive replay runs.
const MAX_SCORE_PER_SEC = 150;
// MAX_BURST can be revisited now that Fix #1 bounds total claimed time.
const MAX_BURST = 1000;
const MIN_RUN_SECONDS = 10;
const MAX_RUN_SECONDS = 3600;
// Fix #1: budget for network round-trip + client/server clock skew.
// Must match the p_time_slack_ms default in the submit_run RPC.
const TIME_SLACK_MS = 15 * 1000;
// 110 min: leaves headroom for a full 60-min run plus an expired-HT6-session
// re-login roundtrip (which can take many minutes if the user steps away).
// Must stay >= MAX_RUN_SECONDS + slack, and < the 2h run_tokens cleanup window
// (run_tokens_hygiene migration) so the token isn't deleted out from under us.
const TOKEN_MAX_AGE_MS = 110 * 60 * 1000;
const TOKEN_MIN_AGE_MS = MIN_RUN_SECONDS * 1000;
const RATE_LIMIT_MS = 30 * 1000;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// Hard upper bound on input-log tuple count.  5000 events at 60 Hz = ~83 s of
// continuous interactions — well above any real run.
const MAX_INPUT_LOG_TUPLES = 5000;

// Submit-score body can be up to 512 KiB to accommodate the input log.
// All other endpoints keep the 16 KiB default.
const SUBMIT_MAX_BYTES = 512 * 1024;

// 'strict' (default): plausibility violations are rejected. This is the
// fail-safe default — an unset/typo'd env var must not silently disable the
// anti-cheat caps. Set PLAUSIBILITY_MODE=log explicitly to only log violations
// (e.g. while tuning caps from real-run telemetry).
const PLAUSIBILITY_MODE = process.env.PLAUSIBILITY_MODE === 'log' ? 'log' : 'strict';

// REPLAY_MODE controls server-side replay validation.
//   'strict' (default / fail-safe): mismatch → 400 replay_mismatch; RPC uses
//             server-recomputed values. An unset or typo'd env var resolves to
//             'strict' so the anti-cheat is never silently disabled — the
//             non-enforcing modes below must be set EXPLICITLY.
//   'shadow': replay runs but mismatches are only LOGGED, not rejected. For
//             tuning caps / collecting telemetry; client values are kept.
//   'off':    replay is skipped entirely.
const _rawReplayMode = process.env.REPLAY_MODE;
const REPLAY_MODE = _rawReplayMode === 'off'    ? 'off'
                  : _rawReplayMode === 'shadow' ? 'shadow'
                  :                               'strict';  // default / fail-safe

// TRAVEL_CHECK controls the anti-teleport travel-time validation layered on top
// of the replay (only meaningful when REPLAY_MODE !== 'off').  The replay alone
// cannot catch a crafted log that teleports a chef across the map every tick:
// the server recomputes the SAME inflated score from that log, so it "matches".
// This check rejects runs containing physically impossible chef movement.
//   'enforce' (default / fail-safe): reject with replay_unreachable — but only
//             when REPLAY_MODE === 'strict' (so it inherits the same rollout gate).
//   'log':    violations are logged but not rejected (rollout / tuning).
//   'off':    the travel check is skipped entirely.  Must be set EXPLICITLY.
const _rawTravelCheck = process.env.TRAVEL_CHECK;
const TRAVEL_CHECK = _rawTravelCheck === 'off' ? 'off'
                   : _rawTravelCheck === 'log' ? 'log'
                   :                             'enforce';  // default / fail-safe

// BEHAVIOR_CHECK controls the human-realism heuristics (sim/behavior.js) layered
// on top of the replay (only meaningful when REPLAY_MODE !== 'off').  Unlike the
// checks above, the SAFE DEFAULT is 'log', NOT 'enforce': these are statistical
// signals that carry false-positive risk, so they ship non-enforcing and are
// tuned from real-run telemetry before 'enforce' is flipped on deliberately.
//   'log' (default):  flags are LOGGED as would_reject_behavior_<flag>; accepted.
//   'enforce':        any flag → 400 behavior_implausible.  Set EXPLICITLY.
//   'off':            heuristics + their telemetry collection are skipped.
const _rawBehaviorCheck = process.env.BEHAVIOR_CHECK;
const BEHAVIOR_CHECK = _rawBehaviorCheck === 'off'     ? 'off'
                     : _rawBehaviorCheck === 'enforce' ? 'enforce'
                     :                                   'log';  // default (non-enforcing)

// Canonical station table — built once at module load.
const STATION_TABLE = buildStationTable(getCanonicalCounterIds());

function isInt(n, min, max) {
    return typeof n === 'number' && Number.isInteger(n) && n >= min && n <= max;
}

// Server-authoritative grade. Mirrors gradeFromScore in game.js but is the
// source of truth — we never trust the client-supplied grade.
function gradeFromScore(score) {
    const s = Math.floor(score);
    if (s < 0) return 'F';
    if (s < 500) return 'D';
    if (s < 2000) return 'C';
    if (s < 5000) return 'B';
    if (s < 10000) return 'A';
    return 'S';
}

// Plausibility caps. Evaluated TWICE: as a cheap pre-filter on the
// client-claimed values, and (authoritatively, post-replay) on the
// server-recomputed values against the replay's REAL duration. Sharing the math
// keeps the two call sites identical.
function plausibilityViolations(score, delivered, streak, timeSecs) {
    return [
        score > timeSecs * MAX_SCORE_PER_SEC + MAX_BURST
            ? { reason: 'implausible_score', ctx: { score, time_secs: timeSecs, cap: timeSecs * MAX_SCORE_PER_SEC + MAX_BURST } }
            : null,
        delivered > Math.floor(timeSecs / 2)
            ? { reason: 'implausible_delivered', ctx: { delivered, time_secs: timeSecs, cap: Math.floor(timeSecs / 2) } }
            : null,
        streak > delivered
            ? { reason: 'implausible_streak', ctx: { streak, delivered } }
            : null,
    ].filter(Boolean);
}

// Compact, structured logger so the reason for every rejection lands
// next to the request in Vercel function logs.
function logReject(reason, ctx) {
    try {
        console.warn('[submit-score] rejected', JSON.stringify({ reason, ...ctx }));
    } catch (_) {
        console.warn('[submit-score] rejected', reason);
    }
}

function rejectAndLog(res, status, reason, ctx) {
    logReject(reason, ctx);
    return json(res, status, { error: reason, ...(ctx?.detail ? { detail: ctx.detail } : {}) });
}

/**
 * Validate that `inputs` is an array of well-formed 3-integer tuples.
 * Returns an error string if invalid, or null if ok.
 * Hard-caps at MAX_INPUT_LOG_TUPLES.
 */
function validateInputTuples(inputs) {
    if (!Array.isArray(inputs)) return 'not_array';
    if (inputs.length > MAX_INPUT_LOG_TUPLES) return 'too_many_tuples';
    for (let i = 0; i < inputs.length; i++) {
        const t = inputs[i];
        if (!Array.isArray(t) || t.length !== 3) return `tuple_${i}_not_3_array`;
        if (!Number.isInteger(t[0]) || t[0] < 0) return `tuple_${i}_bad_delta`;
        if (!Number.isInteger(t[1]) || t[1] < 0 || t[1] > 4) return `tuple_${i}_bad_chefId`;
        if (!Number.isInteger(t[2]) || t[2] < 0) return `tuple_${i}_bad_code`;
    }
    return null;
}

export default async function handler(req, res) {
    if (req.method !== 'POST') return json(res, 405, { error: 'method_not_allowed' });

    // 1) HT6 session gate.
    const auth = await verifyHt6Session(req);
    if (!auth.ok) {
        const code = auth.status === 504 ? 'ht6_unreachable' :
                     auth.status === 502 ? 'ht6_unreachable' :
                                           'ht6_unauthenticated';
        logReject(code, { upstream_status: auth.status });
        return json(res, auth.status, { error: code });
    }

    // 2) Parse + validate payload.
    // Use the larger byte cap to accommodate the input log.
    let body;
    try { body = await readJsonBody(req, { maxBytes: SUBMIT_MAX_BYTES }); }
    catch (e) {
        const reason = e?.message === 'payload_too_large' ? 'payload_too_large' : 'bad_json';
        return rejectAndLog(res, 400, reason);
    }

    const { run_id, token, email: clientEmail, score, streak, delivered, time_secs, inputs: rawInputs } = body;

    if (typeof run_id !== 'string' || run_id.length < 10 || run_id.length > 64)
        return rejectAndLog(res, 400, 'bad_run_id', { run_id_type: typeof run_id, run_id_len: typeof run_id === 'string' ? run_id.length : null });
    if (typeof token !== 'string' || token.length < 20 || token.length > 200)
        return rejectAndLog(res, 400, 'bad_token', { token_type: typeof token, token_len: typeof token === 'string' ? token.length : null });
    if (!isInt(score, 0, 10_000_000))
        return rejectAndLog(res, 400, 'bad_score', { score });
    if (!isInt(streak, 0, 10_000))
        return rejectAndLog(res, 400, 'bad_streak', { streak });
    if (!isInt(delivered, 0, 10_000))
        return rejectAndLog(res, 400, 'bad_delivered', { delivered });
    if (!isInt(time_secs, MIN_RUN_SECONDS, MAX_RUN_SECONDS))
        return rejectAndLog(res, 400, 'bad_time', { time_secs });

    // Validate the inputs field when replay is enabled.
    // rawInputs may be absent (old clients / fallback runs) — treat as empty log.
    let inputTuples = [];
    if (REPLAY_MODE !== 'off') {
        if (rawInputs !== undefined && rawInputs !== null) {
            const tupleErr = validateInputTuples(rawInputs);
            if (tupleErr) {
                return rejectAndLog(res, 400, 'bad_inputs', { detail: tupleErr });
            }
            inputTuples = rawInputs;
        }
        // If rawInputs is absent (legacy client): inputTuples stays [].
        // Replay will run with no events; mismatches will be logged in shadow or
        // rejected in strict.
    }

    // Email must come from HT6's user profile — client-typed email is ignored.
    if (!auth.user?.email) {
        return rejectAndLog(res, 503, 'ht6_profile_unavailable');
    }
    const email = auth.user.email.toLowerCase();

    // 3) Plausibility. In 'log' mode we record violations but accept the
    //    submission, so we can tune caps from real telemetry.
    const plausibilityChecks = plausibilityViolations(score, delivered, streak, time_secs);
    for (const v of plausibilityChecks) {
        if (PLAUSIBILITY_MODE === 'strict') {
            return rejectAndLog(res, 400, v.reason, v.ctx);
        }
        logReject(`would_reject_${v.reason}`, { ...v.ctx, mode: 'log' });
    }

    // Server-authoritative grade (may be overwritten by replay below in strict mode).
    let grade = gradeFromScore(score);

    // 4) HMAC signature on the run token. We still need this in Node since the
    //    secret lives in env. The RPC just trusts that we verified it.
    let supabase;
    try { supabase = db(); }
    catch (_) { return rejectAndLog(res, 500, 'server_misconfigured'); }

    const tokenRow = await supabase
        .from('run_tokens')
        .select('id, issued_at')
        .eq('id', run_id)
        .maybeSingle();
    if (tokenRow.error || !tokenRow.data) {
        return rejectAndLog(res, 400, 'unknown_token', { pg_error: tokenRow.error?.code || null });
    }

    let expected;
    try { expected = hmacToken(`${tokenRow.data.id}.${tokenRow.data.issued_at}`); }
    catch (_) { return rejectAndLog(res, 500, 'server_misconfigured'); }
    if (!timingSafeEqual(expected, token)) {
        return rejectAndLog(res, 400, 'bad_signature', { issued_at: tokenRow.data.issued_at });
    }

    // 5) B4 — Server replay validation.
    //
    // Values to pass to the RPC.  In 'strict' mode these are overwritten with
    // server-recomputed values so the leaderboard is authoritative.  In 'shadow'
    // mode we keep the client values (parity not yet enforced) but log any diff.
    let rpcScore     = score;
    let rpcStreak    = streak;
    let rpcDelivered = delivered;
    let rpcTimeSecs  = time_secs;
    let rpcGrade     = grade;

    if (REPLAY_MODE !== 'off') {
        // Derive replay ceiling from client-claimed time + a generous margin.
        // Cap at 1.5 × MAX_RUN_SECONDS so a pathological large time_secs can't
        // spin the server for minutes.
        const TICK_HZ = 60;
        const maxTicks = Math.min(
            Math.ceil(time_secs * TICK_HZ * 1.1) + TICK_HZ * 30,
            Math.ceil(MAX_RUN_SECONDS * TICK_HZ * 1.5),
        );

        let replaySummary = null;
        let replayErr     = null;
        const decodedInputs = decodeInputLog(inputTuples, STATION_TABLE);

        try {
            const seed       = seedFromRunId(run_id);
            replaySummary = simulate({
                seed,
                config: {
                    ...defaultConfig(),
                    checkTravel:     TRAVEL_CHECK !== 'off',
                    // Collect per-hop telemetry only when the behavior heuristics
                    // will consume it.
                    travelTelemetry: BEHAVIOR_CHECK !== 'off',
                },
                inputs: decodedInputs,
                maxTicks,
            });
        } catch (e) {
            replayErr = String(e?.message || e);
        }

        if (replayErr !== null) {
            const ctx = { run_id, replay_error: replayErr, mode: REPLAY_MODE };
            if (REPLAY_MODE === 'strict') {
                return rejectAndLog(res, 400, 'replay_error', ctx);
            }
            // shadow: log and continue with client values
            console.warn('[submit-score] replay_error', JSON.stringify(ctx));

        } else {
            // Compare server-recomputed vs client-claimed.
            const serverScore     = Math.floor(replaySummary.score);
            const serverDelivered = replaySummary.delivered;
            const serverStreak    = replaySummary.bestStreak;

            // Anti-teleport: the replay flags interactions that occur faster than
            // the fastest possible chef travel between stations.  A score-matching
            // teleport cheat is invisible to the mismatch check below (the server
            // recomputes the same inflated score), so this is the layer that
            // catches it.  Enforced only in strict + enforce; logged otherwise.
            const travelViolations = replaySummary.travelViolations || 0;
            if (travelViolations > 0) {
                const tctx = {
                    run_id,
                    travel_violations: travelViolations,
                    detail: replaySummary.firstTravelViolation,
                    replay_mode: REPLAY_MODE,
                    travel_check: TRAVEL_CHECK,
                };
                if (REPLAY_MODE === 'strict' && TRAVEL_CHECK === 'enforce') {
                    return rejectAndLog(res, 400, 'replay_unreachable', tctx);
                }
                console.warn('[submit-score] would_reject_replay_unreachable', JSON.stringify(tctx));
            }

            // Behavioral heuristics (sim/behavior.js): look for fingerprints of an
            // offline solver — fixed cadence, hops pinned to the travel bound,
            // superhuman multi-chef parallelism.  Default 'log' (non-enforcing).
            if (BEHAVIOR_CHECK !== 'off') {
                const behavior = analyzeBehavior({
                    telemetry: replaySummary.travelTelemetry || [],
                    inputs:    decodedInputs,
                });
                if (behavior.flags.length > 0) {
                    const bctx = {
                        run_id,
                        behavior_flags: behavior.flags,
                        behavior_stats: behavior.stats,
                        behavior_check: BEHAVIOR_CHECK,
                    };
                    if (BEHAVIOR_CHECK === 'enforce') {
                        return rejectAndLog(res, 400, 'behavior_implausible', { ...bctx, detail: behavior.flags.join(',') });
                    }
                    console.warn('[submit-score] would_reject_behavior', JSON.stringify(bctx));
                }
            }

            // Allow small floating-point drift on score (±1 point).
            const scoreMismatch     = Math.abs(serverScore - score) > 1;
            const deliveredMismatch = serverDelivered !== delivered;
            const streakMismatch    = serverStreak    !== streak;

            const hasMismatch = scoreMismatch || deliveredMismatch || streakMismatch;

            if (hasMismatch) {
                const diff = {
                    score:     { server: serverScore,     client: score },
                    delivered: { server: serverDelivered, client: delivered },
                    streak:    { server: serverStreak,    client: streak },
                };
                const ctx = { run_id, diff, mode: REPLAY_MODE };

                if (REPLAY_MODE === 'strict') {
                    return rejectAndLog(res, 400, 'replay_mismatch', ctx);
                }
                // shadow: log and continue with client values
                console.warn('[submit-score] would_reject_replay_mismatch', JSON.stringify(ctx));

            } else {
                // Replay succeeded and matches.
                console.info('[submit-score] replay_ok', JSON.stringify({ run_id, server_score: serverScore, mode: REPLAY_MODE }));
            }

            // Server-authoritative duration for the plausibility caps + the stored
            // time. The pre-replay caps ran against the CLIENT-claimed time_secs,
            // which a tamperer can inflate (e.g. claim 3600 for a 150 s run) to
            // dodge the per-second cap. Re-run the caps here against the replay's
            // REAL duration:
            //   • on game-over, replaySummary.time_secs is the true end tick;
            //   • otherwise the run genuinely lasted the claimed duration — the
            //     submit_run RPC bounds time_secs to wall-clock (+slack) — so the
            //     claim is authoritative and endurance passive score is counted.
            const effectiveSeconds = replaySummary.gameOver
                ? Math.floor(replaySummary.time_secs)
                : time_secs;
            const serverPlausibility = plausibilityViolations(
                serverScore, serverDelivered, serverStreak, effectiveSeconds,
            );
            for (const v of serverPlausibility) {
                const ctx = { run_id, ...v.ctx, effective_seconds: effectiveSeconds, source: 'replay', mode: REPLAY_MODE };
                if (REPLAY_MODE === 'strict') {
                    return rejectAndLog(res, 400, v.reason, ctx);
                }
                // shadow: log and continue with client values
                logReject(`would_reject_${v.reason}`, ctx);
            }

            // In strict mode: use server-recomputed authoritative values for the RPC.
            if (REPLAY_MODE === 'strict') {
                rpcScore     = serverScore;
                rpcDelivered = serverDelivered;
                rpcStreak    = serverStreak;
                // Store the replay's REAL duration, not the client claim, so the
                // leaderboard time_secs can't be inflated.
                rpcTimeSecs  = effectiveSeconds;
                rpcGrade     = gradeFromScore(serverScore);
            }
        }
    }

    // 6) Atomic claim + best-only upsert. The RPC re-fetches the token row
    //    under FOR UPDATE so we cannot lose a race against a concurrent submit.
    const rpc = await supabase.rpc('submit_run', {
        p_run_id:       run_id,
        p_email:        email,
        p_score:        rpcScore,
        p_grade:        rpcGrade,
        p_streak:       rpcStreak,
        p_delivered:    rpcDelivered,
        p_time_secs:    rpcTimeSecs,
        p_ht6_user_id:  auth.user.userId    || null,
        p_first_name:   auth.user.firstName || null,
        p_last_name:    auth.user.lastName  || null,
        p_min_age_ms:    TOKEN_MIN_AGE_MS,
        p_max_age_ms:    TOKEN_MAX_AGE_MS,
        p_rate_limit_ms: RATE_LIMIT_MS,
        p_time_slack_ms: TIME_SLACK_MS,   // Fix #1: bind claimed time_secs to token age
    });

    if (rpc.error) {
        // Surface specific Supabase failure modes so the operator can fix
        // them without reading server logs.
        let code = 'write_failed';
        const pgCode = rpc.error.code;
        if (pgCode === '42883') code = 'rpc_missing';        // function does not exist
        else if (pgCode === '42P01') code = 'table_missing';  // relation does not exist
        else if (pgCode === '42703') code = 'column_missing'; // column does not exist
        else if (pgCode === '23505') code = 'duplicate';      // unique violation (shouldn't happen)
        logReject(code, { pg_code: pgCode, pg_message: rpc.error.message, hint: rpc.error.hint });
        return json(res, 500, { error: code, pg_code: pgCode || null, detail: rpc.error.message });
    }

    const result = rpc.data || {};
    if (!result.ok) {
        const status = result.error === 'rate_limited' ? 429 : 400;
        logReject(result.error || 'submit_failed', { from: 'rpc' });
        return json(res, status, { error: result.error || 'submit_failed' });
    }

    return json(res, 200, {
        ok: true,
        best: result.best,
        kept_existing: !!result.kept_existing,
        email_source: auth.user ? 'ht6' : 'client',
    });
}
