/**
 * Durable pre-downloaded exposure PNG store for the /admin/nircam triage flow:
 * "warm a whole filter/detector pair before an inspection run, then never
 * wait on a fetch".
 *
 * IndexedDB holds one Blob per exposure — the *display byte*: the full-res
 * mask surface when the exposure has one, else the preview thumbnail. Bytes
 * come through the same-origin /api/nircam-png proxy, because OSN serves no
 * CORS headers: the presigned URLs the viewer streams into <img> cannot be
 * READ by page JavaScript (the same constraint that shaped /api/nircam-fits).
 * At render time the detail page prefers a stored blob URL over a presigned
 * URL, so a warmed exposure paints with zero network wait — and keeps
 * painting long after its presigned URL would have expired.
 *
 * IndexedDB, not localStorage, on purpose: localStorage caps out around 5 MB
 * and stores strings; IndexedDB blobs are disk-backed and share the origin's
 * storage quota (GBs on desktop — navigator.storage.estimate() is the truth).
 * A thousand full-res exposures is roughly 6 GB.
 *
 * Lifecycle: every record carries storedAt, and ensurePngStoreReady() —
 * called from both triage pages on mount — sweeps records older than
 * PNG_STORE_TTL_MS, so a finished inspection's cache clears itself after the
 * buffer window without the operator doing anything. clearPngStore() empties
 * it on demand. Object URLs are minted once at hydration (or store time) and
 * revoked when their record is dropped; blobs stay on disk until then, so the
 * in-memory map costs a few strings per exposure, not the image bytes.
 */

export const PNG_STORE_TTL_MS = 24 * 60 * 60 * 1000; // inspection session + buffer

const DB_NAME = 'campfire-nircam-png-store';
const DB_VERSION = 1;
const STORE = 'pngs';
// Modest parallelism: each request streams ~6 MB through a serverless
// function; 4-way keeps the pipe full without hammering the proxy.
const WARM_CONCURRENCY = 4;

interface PngRecord {
  id: number;                    // nircam_exposures.id (the IDB key)
  kind: 'full' | 'preview';      // which byte /api/nircam-png served
  bytes: number;
  storedAt: number;
  blob: Blob;
}

export interface StoredPng {
  kind: 'full' | 'preview';
  url: string;                   // object URL backed by the stored blob
  bytes: number;
}

const mem = new Map<number, StoredPng>();
let db: IDBDatabase | null = null;
let hydrated = false;
let readyPromise: Promise<void> | null = null;

// useSyncExternalStore plumbing (same version-counter pattern as the save
// state in lib/nircam-exposure-cache.ts): consumers re-read the maps after
// the version changes.
let version = 0;
const listeners = new Set<() => void>();
function emit(): void {
  version++;
  for (const l of listeners) l();
}
export function subscribePngStore(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
export function getPngStoreVersion(): number {
  return version;
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE)) {
        req.result.createObjectStore(STORE, { keyPath: 'id' });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error('IndexedDB open failed'));
  });
}

/**
 * Open the database, sweep expired records, and mint object URLs for the
 * rest. Idempotent single-flight; both triage pages call it on mount. If
 * IndexedDB is unavailable (private browsing, disabled storage) this resolves
 * with an empty store — the pages just fall back to the presigned-URL path.
 */
export function ensurePngStoreReady(): Promise<void> {
  if (readyPromise) return readyPromise;
  readyPromise = (async () => {
    if (typeof indexedDB === 'undefined') { hydrated = true; return; }
    try {
      db = await openDb();
      const cutoff = Date.now() - PNG_STORE_TTL_MS;
      await new Promise<void>((resolve, reject) => {
        const tx = db!.transaction(STORE, 'readwrite');
        const cursorReq = tx.objectStore(STORE).openCursor();
        cursorReq.onsuccess = () => {
          const cursor = cursorReq.result;
          if (!cursor) return; // tx.oncomplete resolves
          const rec = cursor.value as PngRecord;
          if (rec.storedAt < cutoff) {
            cursor.delete();
          } else {
            mem.set(rec.id, {
              kind: rec.kind,
              url: URL.createObjectURL(rec.blob),
              bytes: rec.bytes,
            });
          }
          cursor.continue();
        };
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error ?? new Error('PNG store sweep failed'));
      });
    } catch {
      // Store unusable — behave as empty rather than breaking triage.
      db = null;
      for (const e of mem.values()) URL.revokeObjectURL(e.url);
      mem.clear();
    }
    hydrated = true;
    emit();
  })();
  return readyPromise;
}

/** Synchronous read for the render path; undefined until warmed/hydrated. */
export function getStoredPng(id: number): StoredPng | undefined {
  return mem.get(id);
}

export function getPngStoreStats(): { count: number; bytes: number; hydrated: boolean } {
  let bytes = 0;
  for (const e of mem.values()) bytes += e.bytes;
  return { count: mem.size, bytes, hydrated };
}

function putRecord(rec: PngRecord): Promise<void> {
  return new Promise((resolve, reject) => {
    if (!db) { reject(new Error('PNG store unavailable')); return; }
    const tx = db.transaction(STORE, 'readwrite');
    tx.objectStore(STORE).put(rec);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error ?? new Error('PNG store write failed'));
    tx.onabort = () => reject(tx.error ?? new Error('PNG store write aborted'));
  });
}

function isQuotaError(err: unknown): boolean {
  return err instanceof DOMException &&
    (err.name === 'QuotaExceededError' || err.name === 'NS_ERROR_DOM_QUOTA_REACHED');
}

export interface PngWarmProgress {
  /** Exposures processed this run (stored, skipped, or failed) out of total. */
  done: number;
  total: number;
  /** Bytes downloaded this run (already-stored exposures contribute nothing). */
  bytes: number;
  /** Exposures whose download failed this run (re-running retries them). */
  failed: number;
}

export interface PngWarmResult extends PngWarmProgress {
  aborted: boolean;
  /** Set when the warm stopped early (quota exhausted, store unusable). */
  error?: string;
}

/**
 * Download-and-store every id not already held, in the order given (= the
 * list's inspection order, so the exposures the operator will hit first are
 * warmed first — inspecting can start while the warm continues). Resumable
 * by construction: already-stored ids are skipped, so re-running after a
 * cancel or failure only fetches what's missing. A 404 (exposure has no PNG
 * deployed) is a silent skip, not a failure — those render "No PNG available"
 * in triage regardless.
 */
export async function warmPngStore(
  ids: number[],
  opts: { signal?: AbortSignal; onProgress?: (p: PngWarmProgress) => void } = {},
): Promise<PngWarmResult> {
  await ensurePngStoreReady();
  const todo = ids.filter((i) => !mem.has(i));
  const progress: PngWarmProgress = {
    done: ids.length - todo.length,
    total: ids.length,
    bytes: 0,
    failed: 0,
  };
  opts.onProgress?.({ ...progress });
  if (!db && todo.length > 0) {
    return { ...progress, aborted: false, error: 'Browser storage is unavailable (private browsing?)' };
  }

  let fatal: string | undefined;
  let next = 0;
  const worker = async () => {
    while (!fatal && !opts.signal?.aborted) {
      const idx = next++;
      if (idx >= todo.length) return;
      const id = todo[idx];
      try {
        const res = await fetch(`/api/nircam-png?id=${id}`, { signal: opts.signal });
        if (!res.ok) {
          if (res.status !== 404) progress.failed++;
          progress.done++;
          opts.onProgress?.({ ...progress });
          continue;
        }
        const kind = res.headers.get('x-png-kind') === 'preview' ? 'preview' : 'full';
        const blob = await res.blob();
        await putRecord({ id, kind, bytes: blob.size, storedAt: Date.now(), blob });
        mem.set(id, { kind, url: URL.createObjectURL(blob), bytes: blob.size });
        progress.bytes += blob.size;
        progress.done++;
        opts.onProgress?.({ ...progress });
        // Keep any store-stats UI roughly live without a global re-render per
        // image; the final emit below settles the exact numbers.
        if (progress.done % 25 === 0) emit();
      } catch (err) {
        if (opts.signal?.aborted) return;
        if (isQuotaError(err)) {
          fatal = 'Browser storage quota exhausted — the warm stored what fit. ' +
            'Clear the cache or free disk space and re-run to continue.';
          return;
        }
        progress.failed++;
        progress.done++;
        opts.onProgress?.({ ...progress });
      }
    }
  };
  await Promise.all(Array.from({ length: WARM_CONCURRENCY }, worker));
  emit();
  return { ...progress, aborted: !!opts.signal?.aborted, error: fatal };
}

/**
 * Drop everything now (the explicit "clear cache" affordance; the TTL sweep
 * is the automatic path). Revokes the object URLs, so an <img> currently
 * painting one goes blank — the detail page falls back to a presigned URL on
 * its next step.
 */
export async function clearPngStore(): Promise<void> {
  await ensurePngStoreReady();
  if (db) {
    await new Promise<void>((resolve, reject) => {
      const tx = db!.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).clear();
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error ?? new Error('PNG store clear failed'));
    });
  }
  for (const e of mem.values()) URL.revokeObjectURL(e.url);
  mem.clear();
  emit();
}

/** Origin storage headroom, for the pre-warm size hint. Null when unsupported. */
export async function estimateStorage(): Promise<{ usage: number; quota: number } | null> {
  if (typeof navigator === 'undefined' || !navigator.storage?.estimate) return null;
  try {
    const { usage = 0, quota = 0 } = await navigator.storage.estimate();
    return { usage, quota };
  } catch {
    return null;
  }
}
