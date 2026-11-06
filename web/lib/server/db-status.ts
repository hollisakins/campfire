import 'server-only';
import { promises as fs } from 'node:fs';
import { createServiceClient } from '@/lib/supabase/service';

/**
 * Is the database answering, and is it stuck? One in-memory probe per web
 * process, behind `/api/health` (container healthcheck, deploy gate, the
 * external site check) and `/api/status` (the site-wide banner).
 *
 * The probe is a READ (`db_stall_status()`): during the 2026-11-06 Jetstream2
 * volume stall reads were served from cache while every COMMIT waited minutes
 * on the WAL flush, so the old one-row read stayed green, and a write probe
 * would itself have hung and held a PostgREST connection per attempt. The
 * function reports the backends waiting on the WAL or on a lock instead.
 *
 * Probes are lazy and single-flight: a caller gets the last sample while it is
 * younger than PROBE_TTL_MS, otherwise it waits for one probe (bounded by
 * PROBE_TIMEOUT_MS) that concurrent callers share. Once the process has had a
 * good sample, a non-ok state is reported only after BAD_SAMPLES consecutive
 * bad samples, so one slow answer does not raise the banner; before that, the
 * first bad sample counts (the deploy gate). A single good sample clears it.
 */

export type DbState = 'ok' | 'degraded' | 'down';

export interface DbStatus {
  state: DbState;
  /** Why the last sample was bad: `wal` (commits stuck on the disk), `locks`, `unreachable`. */
  reason: 'wal' | 'locks' | 'unreachable' | null;
  /** When the last probe finished (ms since epoch); 0 before the first. */
  checked_at: number;
}

interface StallCounts {
  wal_waiters: number;
  wal_wait_s: number;
  lock_waiters: number;
  lock_wait_s: number;
}

export const PROBE_TTL_MS = 15_000;
export const PROBE_TIMEOUT_MS = 4_000;
export const BAD_SAMPLES = 2;
/** A commit waiting this long on the WAL flush means the disk is stalled. */
export const WAL_STALL_S = 10;

interface Sample {
  bad: DbStatus['reason'];
  at: number;
}

let last: Sample | null = null;
let consecutiveBad = 0;
/** Has this process ever had a good sample? Until it has, nothing is delayed. */
let everOk = false;
let inflight: Promise<void> | null = null;

/** Classify one probe answer. Exported for tests. */
export function classify(counts: StallCounts | null): DbStatus['reason'] {
  if (!counts) return 'unreachable';
  if (counts.wal_waiters > 0 && counts.wal_wait_s >= WAL_STALL_S) return 'wal';
  if (counts.lock_waiters > 0) return 'locks';
  return null;
}

async function probe(): Promise<void> {
  let counts: StallCounts | null = null;
  try {
    const { data, error } = await createServiceClient()
      .rpc('db_stall_status')
      .abortSignal(AbortSignal.timeout(PROBE_TIMEOUT_MS));
    if (!error && data) counts = data as StallCounts;
  } catch {
    counts = null;
  }
  const bad = classify(counts);
  consecutiveBad = bad ? consecutiveBad + 1 : 0;
  if (!bad) everOk = true;
  last = { bad, at: Date.now() };
}

function current(): DbStatus {
  if (!last) return { state: 'ok', reason: null, checked_at: 0 };
  // The two-sample delay only absorbs a blip in a process that has worked: a
  // fresh container whose first probe fails must not answer 200, because
  // deploy.sh accepts the first healthy answer and would skip its rollback.
  const reported = !everOk || consecutiveBad >= BAD_SAMPLES ? last.bad : null;
  return {
    state: reported === 'unreachable' ? 'down' : reported ? 'degraded' : 'ok',
    reason: reported,
    checked_at: last.at,
  };
}

export async function getDbStatus(): Promise<DbStatus> {
  if (!last || Date.now() - last.at >= PROBE_TTL_MS) {
    inflight ??= probe().finally(() => {
      inflight = null;
    });
    await inflight;
  }
  return current();
}

/** Forget every sample (tests). */
export function resetDbStatus(): void {
  last = null;
  consecutiveBad = 0;
  everOk = false;
  inflight = null;
}

/**
 * An operator's message for the banner (planned maintenance, a provider
 * incident): the first 300 characters of STATUS_MESSAGE_FILE, re-read at most
 * every PROBE_TTL_MS. The file sits on the VM's root disk (ops/docker-compose.yml
 * mounts /etc/campfire/status read-only), never the data volume, so it can be
 * written while the volume is stalled and needs no restart. Missing or empty
 * file = no message.
 */
let messageCache: { text: string | null; at: number } | null = null;

export async function getStatusMessage(): Promise<string | null> {
  const path = process.env.STATUS_MESSAGE_FILE;
  if (!path) return null;
  if (messageCache && Date.now() - messageCache.at < PROBE_TTL_MS) return messageCache.text;
  let text: string | null = null;
  try {
    text = (await fs.readFile(path, 'utf8')).trim().slice(0, 300) || null;
  } catch {
    text = null;
  }
  messageCache = { text, at: Date.now() };
  return text;
}

/** Forget the cached message (tests). */
export function resetStatusMessage(): void {
  messageCache = null;
}
