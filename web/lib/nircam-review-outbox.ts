/**
 * Durable outbox for NIRCam triage decisions.
 *
 * The triage flow's original transport was a fire-and-forget server action:
 * a decision existed only in JS memory between the keypress and the server
 * ack, the action POST had no timeout and could not be retried or marked
 * keepalive, and all actions from the tab shared one serialized dispatch
 * lane. A single dropped POST therefore lost the decision silently — and
 * left a stuck "save in flight" marker that suppressed revalidation, so the
 * UI kept showing the decision as applied until a full reload.
 *
 * This module makes the space-bar press itself the durable commit:
 *
 *  1. `stageReviewDecision` merges the decision into a localStorage-backed
 *     outbox entry (one per exposure, newest-wins per field) BEFORE anything
 *     touches the network. A refresh, crash, or closed laptop lid loses
 *     nothing — entries replay on the next visit to the triage pages.
 *  2. A flush loop delivers entries to POST /api/admin/nircam/review with a
 *     request timeout, capped-backoff retries, and `keepalive` (so a flush
 *     dispatched during page teardown still completes). Delivery is
 *     at-least-once; the server's review_decided_at last-writer-wins guard
 *     makes replays and reordering harmless.
 *  3. `overlayReviewDecisions` lets row readers reconstruct truth as
 *     "server row + not-yet-acked decisions" instead of trusting a cached
 *     optimistic row that might be stale.
 *
 * Failure surface: transient errors (network, timeout, 5xx) retry forever
 * with capped backoff and raise the module save-error banner (kind
 * 'retrying') after a few consecutive misses; permanent rejections (4xx —
 * auth, validation, vanished row) drop the entry, revert the row cache to
 * server truth, and raise the banner as before (kind 'permanent').
 *
 * Multi-tab: tabs share the storage key, and every write MERGES with what's
 * in storage instead of overwriting it (per id, the newer decidedAt wins;
 * entries this tab has seen acknowledged are dropped), so one tab persisting
 * can never evict another tab's undelivered decision. Tabs also adopt each
 * other's entries via storage events and both flush them — duplicate sends
 * are idempotent under the server guard, and the worst cross-tab race is one
 * redundant no-op send of an already-delivered decision.
 */

import type { NircamExposure } from '@/lib/types';
import {
  getCachedExposure,
  setCachedExposure,
  deleteCachedExposure,
  getSaveError,
  setSaveError,
} from '@/lib/nircam-exposure-cache';
import { getNircamExposureById } from '@/lib/actions/nircam-exposures';

export interface ReviewDecisionFields {
  review_status?: NircamExposure['review_status'];
  correction?: NircamExposure['correction'];
  notes?: string;
}

interface OutboxEntry {
  id: number;
  fields: ReviewDecisionFields;
  /** ms epoch of the newest edit folded into this entry (strictly monotonic
   *  per entry, so "did it change while a send was in flight" is one compare). */
  decidedAt: number;
  /** For save-failure banners; entries can outlive the row cache. */
  filename: string | null;
}

const STORAGE_KEY = 'campfire.nircam-review-outbox.v1';
const ENDPOINT = '/api/admin/nircam/review';
const REQUEST_TIMEOUT_MS = 12_000;
// Consecutive transient failures for one entry before the operator is told
// (the entry keeps retrying either way).
const FAILURES_BEFORE_BANNER = 3;
const RETRY_BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 15_000, 30_000];
// Replay after a reload can hold a whole session's tail; don't stampede.
const MAX_CONCURRENT_FLUSHES = 6;

const isBrowser = typeof window !== 'undefined';

const entries = new Map<number, OutboxEntry>();
/** Review fields whose server commit this tab has seen, by exposure id.
 *  Overlaid onto any server read whose row predates the commit, so a slow
 *  response snapshotted pre-commit can't resurrect the old decision. */
const ackedReview = new Map<number, { fields: ReviewDecisionFields; decidedAt: number }>();
const inFlight = new Set<number>();
const consecutiveFailures = new Map<number, number>();
const retryTimers = new Map<number, ReturnType<typeof setTimeout>>();
let activeFlushes = 0;
const waitingIds: number[] = [];

let version = 0;
const listeners = new Set<() => void>();
function emit(): void {
  version++;
  for (const listener of listeners) listener();
}

export function subscribeReviewOutbox(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getReviewOutboxVersion(): number {
  return version;
}

/** Entries staged or in flight — decisions not yet acknowledged by the server. */
export function queuedReviewCount(): number {
  return entries.size;
}

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

let loaded = false;

function parseStoredEntries(raw: string | null): OutboxEntry[] {
  if (!raw) return [];
  const out: OutboxEntry[] = [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    for (const item of parsed) {
      if (typeof item !== 'object' || item === null) continue;
      const e = item as Partial<OutboxEntry>;
      if (typeof e.id !== 'number' || !Number.isInteger(e.id) || e.id <= 0) continue;
      if (typeof e.decidedAt !== 'number' || !Number.isFinite(e.decidedAt)) continue;
      if (typeof e.fields !== 'object' || e.fields === null) continue;
      const fields: ReviewDecisionFields = {};
      if (e.fields.review_status !== undefined
          && ['pending', 'approved', 'excluded'].includes(e.fields.review_status)) {
        fields.review_status = e.fields.review_status;
      }
      if (e.fields.correction !== undefined
          && ['none', 'needed', 'done'].includes(e.fields.correction)) {
        fields.correction = e.fields.correction;
      }
      if (typeof e.fields.notes === 'string') fields.notes = e.fields.notes;
      if (Object.keys(fields).length === 0) continue;
      out.push({
        id: e.id,
        fields,
        decidedAt: e.decidedAt,
        filename: typeof e.filename === 'string' ? e.filename : null,
      });
    }
  } catch {
    return [];
  }
  return out;
}

/** True when this tab has seen a commit at least as new as `entry` — the
 *  entry is settled and must not be (re)adopted or re-persisted. */
function isSettled(entry: OutboxEntry): boolean {
  const acked = ackedReview.get(entry.id);
  return acked !== undefined && acked.decidedAt >= entry.decidedAt;
}

/** Fold entries found in shared storage (another tab's work, or a previous
 *  session's) into this tab's map: per id the newer decidedAt wins, settled
 *  entries are ignored. Returns the adopted ids so callers can kick flushes. */
function adoptEntries(found: OutboxEntry[]): number[] {
  const adopted: number[] = [];
  for (const e of found) {
    if (isSettled(e)) continue;
    const mine = entries.get(e.id);
    if (mine !== undefined && mine.decidedAt >= e.decidedAt) continue;
    entries.set(e.id, e);
    adopted.push(e.id);
  }
  return adopted;
}

function ensureLoaded(): void {
  if (loaded || !isBrowser) return;
  loaded = true;
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return; // storage unavailable (private mode etc.) — memory-only outbox
  }
  if (raw && parseStoredEntries(raw).length === 0) {
    // Corrupt payload: drop it rather than wedge every future stage() call.
    try { window.localStorage.removeItem(STORAGE_KEY); } catch { /* ignore */ }
  }
  adoptEntries(parseStoredEntries(raw));
  // Another tab staging or retiring decisions fires this (storage events
  // never fire in the tab that wrote). Adopt anything newer and deliver it
  // here too — duplicates are idempotent; retirement is handled by acks, not
  // by events, so a tab never drops an entry just because another tab wrote.
  window.addEventListener('storage', (ev) => {
    if (ev.key !== STORAGE_KEY) return;
    const adopted = adoptEntries(parseStoredEntries(ev.newValue));
    if (adopted.length > 0) {
      emit();
      for (const id of adopted) void attemptEntry(id);
    }
  });
}

function persist(): void {
  if (!isBrowser) return;
  try {
    // MERGE with storage rather than overwrite: another tab's undelivered
    // entries must survive this tab's write. Per id the newer decidedAt
    // wins; entries this tab has seen acknowledged are dropped (that is the
    // only retirement path — a tab that merely didn't have an entry in
    // memory can never evict it).
    const merged = new Map<number, OutboxEntry>();
    for (const e of parseStoredEntries(window.localStorage.getItem(STORAGE_KEY))) {
      if (!isSettled(e)) merged.set(e.id, e);
    }
    for (const e of entries.values()) {
      const theirs = merged.get(e.id);
      if (theirs === undefined || theirs.decidedAt <= e.decidedAt) merged.set(e.id, e);
    }
    if (merged.size === 0) window.localStorage.removeItem(STORAGE_KEY);
    else window.localStorage.setItem(STORAGE_KEY, JSON.stringify([...merged.values()]));
  } catch {
    // Quota/private mode: the in-memory outbox still flushes this session.
  }
}

// ---------------------------------------------------------------------------
// Overlay
// ---------------------------------------------------------------------------

function applyFields(
  row: NircamExposure,
  fields: ReviewDecisionFields,
  decidedAt: number,
): NircamExposure {
  return {
    ...row,
    ...(fields.review_status !== undefined ? { review_status: fields.review_status } : null),
    ...(fields.correction !== undefined ? { correction: fields.correction } : null),
    // '' is a deliberate clear and stores as null, mirroring the server.
    ...(fields.notes !== undefined ? { notes: fields.notes || null } : null),
    review_decided_at: new Date(decidedAt).toISOString(),
  };
}

/**
 * Reconstruct current truth for a server-read row: acked-but-possibly-unseen
 * decisions first (skipped when the row already carries an equal-or-newer
 * review_decided_at), then still-queued decisions, which are newer by
 * construction. Pass every freshly fetched exposure row through this before
 * caching or rendering it — it makes stale reads harmless instead of needing
 * to be suppressed.
 */
export function overlayReviewDecisions(row: NircamExposure): NircamExposure {
  ensureLoaded();
  let out = row;
  const acked = ackedReview.get(row.id);
  if (acked) {
    const rowStamp = out.review_decided_at ? Date.parse(out.review_decided_at) : 0;
    if (!(rowStamp >= acked.decidedAt)) {
      out = applyFields(out, acked.fields, acked.decidedAt);
    }
  }
  const entry = entries.get(row.id);
  if (entry) out = applyFields(out, entry.fields, entry.decidedAt);
  return out;
}

// ---------------------------------------------------------------------------
// Staging
// ---------------------------------------------------------------------------

/**
 * Record a triage decision durably and schedule its delivery. This is the
 * commit point: once this returns, the decision survives refresh/close and
 * will reach the server unless it is permanently rejected (which raises the
 * save-error banner). Also applies the decision to the cached row so
 * navigation keeps painting the operator's latest state.
 */
export function stageReviewDecision(
  id: number,
  fields: ReviewDecisionFields,
  filename?: string | null,
): void {
  if (!isBrowser || Object.keys(fields).length === 0) return;
  ensureLoaded();
  const existing = entries.get(id);
  // Strictly monotonic per entry even within one ms, so an in-flight send's
  // snapshot compares unequal to a re-edited entry.
  const decidedAt = Math.max(Date.now(), (existing?.decidedAt ?? 0) + 1);
  const entry: OutboxEntry = {
    id,
    fields: { ...existing?.fields, ...fields },
    decidedAt,
    filename: filename ?? existing?.filename ?? getCachedExposure(id)?.filename ?? null,
  };
  entries.set(id, entry);
  persist();
  const cached = getCachedExposure(id);
  if (cached) setCachedExposure(applyFields(cached, entry.fields, entry.decidedAt));
  emit();
  void attemptEntry(id);
}

/**
 * Load persisted entries and (re)start delivery. Idempotent; call from the
 * triage pages on mount so decisions stranded by a reload replay as soon as
 * the operator is back in the tool.
 */
export function ensureReviewOutboxRunning(): void {
  if (!isBrowser) return;
  ensureLoaded();
  for (const id of entries.keys()) void attemptEntry(id);
}

// ---------------------------------------------------------------------------
// Delivery
// ---------------------------------------------------------------------------

class PermanentSaveError extends Error {}

async function postReview(entry: OutboxEntry): Promise<{
  exposure: NircamExposure | null;
  superseded?: boolean;
}> {
  let res: Response;
  try {
    res = await fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ id: entry.id, decidedAt: entry.decidedAt, fields: entry.fields }),
      // Survives page teardown — the beforeunload flush depends on this.
      keepalive: true,
      // A hung request becomes a retryable error instead of a wedged save.
      signal: typeof AbortSignal.timeout === 'function'
        ? AbortSignal.timeout(REQUEST_TIMEOUT_MS)
        : undefined,
    });
  } catch (err) {
    // Network failure / timeout: retryable.
    throw err instanceof Error ? err : new Error('Network error');
  }
  let body: { exposure?: NircamExposure | null; superseded?: boolean; error?: string } | null = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (res.ok) return { exposure: body?.exposure ?? null, superseded: body?.superseded };
  const message = body?.error || `Save failed (HTTP ${res.status})`;
  // 401 is deliberately retryable, not permanent: an expired session must not
  // drop the decision. The entry stays durable, the banner says saving is
  // stuck, and once the operator is signed in again (this session or after a
  // reload — entries replay) delivery completes.
  if (res.status >= 500 || res.status === 401 || res.status === 408 || res.status === 429) {
    throw new Error(message); // retryable
  }
  throw new PermanentSaveError(message);
}

function scheduleRetry(id: number, failureCount: number): void {
  const delay = RETRY_BACKOFF_MS[Math.min(failureCount - 1, RETRY_BACKOFF_MS.length - 1)];
  const existing = retryTimers.get(id);
  if (existing !== undefined) clearTimeout(existing);
  retryTimers.set(id, setTimeout(() => {
    retryTimers.delete(id);
    void attemptEntry(id);
  }, delay));
}

/** Write a server-confirmed row into the cache unless the cached row is
 *  strictly newer (a mask save confirmed while this response was in flight —
 *  both paths bump updated_at server-side). */
function cacheConfirmedRow(row: NircamExposure): void {
  const cached = getCachedExposure(row.id);
  if (cached?.updated_at && row.updated_at
      && Date.parse(row.updated_at) < Date.parse(cached.updated_at)) {
    return;
  }
  setCachedExposure(overlayReviewDecisions(row));
}

async function attemptEntry(id: number): Promise<void> {
  if (!entries.has(id) || inFlight.has(id)) return;
  if (activeFlushes >= MAX_CONCURRENT_FLUSHES) {
    if (!waitingIds.includes(id)) waitingIds.push(id);
    return;
  }
  const snapshot = entries.get(id)!;
  activeFlushes++;
  inFlight.add(id);
  emit();
  // True when the operator re-edited this exposure while the send was in
  // flight: the current entry is then a superset of the snapshot's fields
  // with a newer stamp, and it must go back out — never be retired (on ack)
  // or dropped (on permanent rejection) on the strength of the snapshot's
  // outcome alone. The re-send is deferred to `finally`: attemptEntry bails
  // on in-flight ids, and this one stays marked in-flight until then.
  const changedMidFlight = () => {
    const current = entries.get(id);
    return current !== undefined && current.decidedAt !== snapshot.decidedAt;
  };
  let reattempt = false;
  try {
    const result = await postReview(snapshot);

    // Settled. Retire the entry unless it changed mid-flight.
    reattempt = changedMidFlight();
    if (!reattempt) {
      entries.delete(id);
      persist();
    }
    consecutiveFailures.delete(id);
    if (!result.superseded) {
      // Remember what committed so later stale reads can be overlaid. On a
      // superseded response our snapshot did NOT commit — a newer decision
      // (another tab, or a pre-teardown keepalive from a previous session)
      // already owns the row, and the returned row reflects it.
      ackedReview.set(id, { fields: { ...snapshot.fields }, decidedAt: snapshot.decidedAt });
    }
    if (result.exposure) cacheConfirmedRow(result.exposure);
    if (getSaveError()?.id === id) setSaveError(null);
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Save failed';
    const filename = snapshot.filename ?? `exposure #${id}`;
    if (err instanceof PermanentSaveError) {
      if (changedMidFlight()) {
        // The rejection judged a payload the operator has since replaced —
        // the merged entry deserves its own verdict. Re-send it; if it is
        // rejected too, THAT failure drops and reports it.
        consecutiveFailures.delete(id);
        reattempt = true;
      } else {
        // The server rejected the write — retrying the same payload can't
        // succeed. Drop the decision LOUDLY: revert the cached row to server
        // truth (best effort) and tell the operator to re-apply.
        entries.delete(id);
        persist();
        consecutiveFailures.delete(id);
        try {
          const fresh = await getNircamExposureById(id);
          if (fresh.exposure) cacheConfirmedRow(fresh.exposure);
          else deleteCachedExposure(id);
        } catch {
          deleteCachedExposure(id);
        }
        setSaveError({ id, filename, message, kind: 'permanent' });
      }
    } else {
      const failures = (consecutiveFailures.get(id) ?? 0) + 1;
      consecutiveFailures.set(id, failures);
      if (failures >= FAILURES_BEFORE_BANNER) {
        setSaveError({ id, filename, message, kind: 'retrying' });
      }
      scheduleRetry(id, failures);
    }
  } finally {
    activeFlushes--;
    inFlight.delete(id);
    emit();
    if (reattempt) void attemptEntry(id);
    const next = waitingIds.shift();
    if (next !== undefined) void attemptEntry(next);
  }
}
