/**
 * Durable pre-downloaded exposure PNG store for the /admin/nircam triage flow:
 * "warm a whole filter/detector pair before an inspection run, then never
 * wait on a fetch".
 *
 * IndexedDB holds one Blob per exposure — the *display byte*: the full-res
 * mask surface when the exposure has one, else the preview thumbnail. Bytes
 * come from the delivery front (a CORS-readable, content-addressed url that
 * /api/nircam-png?resolve=1 mints) or, when the front is not configured,
 * through the same-origin /api/nircam-png proxy — OSN itself serves no CORS
 * headers, so a bare presigned URL cannot be READ by page JavaScript (the
 * same constraint that shaped /api/nircam-fits).
 * At render time the detail page prefers a stored blob URL over a presigned
 * URL, so a warmed exposure paints with zero network wait — and keeps
 * painting long after its presigned URL would have expired.
 *
 * IndexedDB, not localStorage, on purpose: localStorage caps out around 5 MB
 * and stores strings; IndexedDB blobs are disk-backed and share the origin's
 * storage quota (GBs on desktop — navigator.storage.estimate() is the truth).
 * A thousand full-res exposures is roughly 6 GB.
 *
 * The warm itself is MODULE state, not component state: the control that
 * starts it unmounts as soon as the operator opens an exposure (the whole
 * point is inspecting while the warm continues), so the download loop and its
 * progress live here and every page of the flow just subscribes. One warm at
 * a time; navigation neither cancels nor duplicates it.
 *
 * Tabs cooperate through a BroadcastChannel: each stored image and each
 * clear is announced, and other tabs fold the change into their own map — so
 * warming in one tab feeds a triage session already running in another
 * without a reload.
 *
 * Lifecycle: every record carries storedAt. Expiry (PNG_STORE_TTL_MS after
 * download — inspection session + buffer) is enforced three ways so the
 * "auto-clears" promise holds even for a long-lived SPA session: a sweep at
 * first hydration, a read-time check in getStoredPng (an expired blob is
 * never served), and a periodic re-sweep that actually deletes what expired
 * while the tab stayed open. clearPngStore() empties everything on demand.
 * Object URLs are minted once at hydration (or store time) and revoked when
 * their record is dropped; blobs stay on disk until then, so the in-memory
 * map costs a few strings per exposure, not the image bytes.
 */

export const PNG_STORE_TTL_MS = 24 * 60 * 60 * 1000; // inspection session + buffer

const DB_NAME = 'campfire-nircam-png-store';
const DB_VERSION = 1;
const STORE = 'pngs';
const CHANNEL_NAME = 'campfire-nircam-png-store';
// Modest parallelism: each request streams ~6 MB through a serverless
// function; 4-way keeps the pipe full without hammering the proxy.
const WARM_CONCURRENCY = 4;
// How often a long-lived tab re-checks for records that expired while open.
const SWEEP_INTERVAL_MS = 10 * 60 * 1000;
// Progress emits are throttled so a warm running behind an active triage
// session doesn't re-render the detail page once per downloaded image.
const PROGRESS_EMIT_INTERVAL_MS = 250;

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
  storedAt: number;
}

const mem = new Map<number, StoredPng>();
let db: IDBDatabase | null = null;
let hydrated = false;
let readyPromise: Promise<void> | null = null;
let channel: BroadcastChannel | null = null;

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

function isExpired(storedAt: number): boolean {
  return Date.now() - storedAt > PNG_STORE_TTL_MS;
}

function dropMemEntry(id: number): void {
  const e = mem.get(id);
  if (!e) return;
  URL.revokeObjectURL(e.url);
  mem.delete(id);
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

function getRecord(id: number): Promise<PngRecord | undefined> {
  return new Promise((resolve, reject) => {
    if (!db) { resolve(undefined); return; }
    const req = db.transaction(STORE, 'readonly').objectStore(STORE).get(id);
    req.onsuccess = () => resolve(req.result as PngRecord | undefined);
    req.onerror = () => reject(req.error ?? new Error('PNG store read failed'));
  });
}

function deleteRecords(ids: number[]): Promise<void> {
  return new Promise((resolve, reject) => {
    if (!db || ids.length === 0) { resolve(); return; }
    const tx = db.transaction(STORE, 'readwrite');
    const os = tx.objectStore(STORE);
    for (const id of ids) os.delete(id);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error ?? new Error('PNG store delete failed'));
  });
}

/**
 * Drop everything past its TTL — from this tab's map and from disk. Runs at
 * hydration and on a coarse interval afterwards, so records that age out
 * while the SPA stays open still get deleted, not just hidden by the
 * read-time check in getStoredPng.
 */
async function sweepExpired(): Promise<void> {
  const expired: number[] = [];
  for (const [id, e] of mem) {
    if (isExpired(e.storedAt)) expired.push(id);
  }
  if (expired.length === 0) return;
  for (const id of expired) dropMemEntry(id);
  try {
    await deleteRecords(expired);
  } catch {
    // Disk cleanup failed; the entries are already unservable here and the
    // next hydration's sweep gets another shot.
  }
  emit();
}

// Cross-tab messages: 'stored' announces one new record (receivers pull it
// from IndexedDB into their own map), 'cleared' announces a full clear.
type ChannelMessage = { type: 'stored'; id: number } | { type: 'cleared' };

function announce(msg: ChannelMessage): void {
  try {
    channel?.postMessage(msg);
  } catch {
    // Channel closed or serialization refused — cross-tab freshness only.
  }
}

async function onChannelMessage(msg: ChannelMessage): Promise<void> {
  if (msg.type === 'cleared') {
    for (const e of mem.values()) URL.revokeObjectURL(e.url);
    mem.clear();
    emit();
    return;
  }
  if (msg.type === 'stored' && typeof msg.id === 'number' && !mem.has(msg.id)) {
    try {
      const rec = await getRecord(msg.id);
      if (rec && !isExpired(rec.storedAt) && !mem.has(rec.id)) {
        mem.set(rec.id, {
          kind: rec.kind,
          url: URL.createObjectURL(rec.blob),
          bytes: rec.bytes,
          storedAt: rec.storedAt,
        });
        emit();
      }
    } catch {
      // Reads race the writer tab; the record stays available on disk.
    }
  }
}

/**
 * Open the database, sweep expired records, mint object URLs for the rest,
 * and start the cross-tab channel + periodic re-sweep. Idempotent
 * single-flight; both triage pages call it on mount. If IndexedDB is
 * unavailable (private browsing, disabled storage) this resolves with an
 * empty store — the pages just fall back to the presigned-URL path.
 */
export function ensurePngStoreReady(): Promise<void> {
  if (readyPromise) return readyPromise;
  readyPromise = (async () => {
    if (typeof indexedDB !== 'undefined') {
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
                storedAt: rec.storedAt,
              });
            }
            cursor.continue();
          };
          tx.oncomplete = () => resolve();
          tx.onerror = () => reject(tx.error ?? new Error('PNG store sweep failed'));
        });
        if (typeof BroadcastChannel !== 'undefined') {
          channel = new BroadcastChannel(CHANNEL_NAME);
          channel.onmessage = (ev) => { onChannelMessage(ev.data as ChannelMessage); };
        }
        setInterval(sweepExpired, SWEEP_INTERVAL_MS);
      } catch {
        // Store unusable — behave as empty rather than breaking triage.
        db = null;
        for (const e of mem.values()) URL.revokeObjectURL(e.url);
        mem.clear();
      }
    }
    hydrated = true;
    emit();
  })();
  return readyPromise;
}

/**
 * Synchronous read for the render path; undefined until warmed/hydrated.
 * Expiry is enforced here too: a record past its TTL is never served, even
 * before the periodic sweep gets around to deleting it.
 */
export function getStoredPng(id: number): StoredPng | undefined {
  const e = mem.get(id);
  if (!e) return undefined;
  if (isExpired(e.storedAt)) return undefined;
  return e;
}

export function getPngStoreStats(): { count: number; bytes: number; hydrated: boolean } {
  let count = 0;
  let bytes = 0;
  for (const e of mem.values()) {
    if (isExpired(e.storedAt)) continue;
    count++;
    bytes += e.bytes;
  }
  return { count, bytes, hydrated };
}

function putRecord(rec: PngRecord): Promise<void> {
  return new Promise((resolve, reject) => {
    if (!db) { reject(new Error('PNG store unavailable')); return; }
    const tx = db.transaction(STORE, 'readwrite');
    const req = tx.objectStore(STORE).put(rec);
    tx.oncomplete = () => resolve();
    // Reject from the REQUEST's error handler: request.error is populated
    // before its error event fires, while tx.error is still null when that
    // event bubbles to the transaction (the abort that populates it happens
    // after dispatch). Rejecting from tx.onerror surfaced a generic Error and
    // made the warm's QuotaExceededError fatal-stop unreachable.
    req.onerror = () => reject(req.error ?? new Error('PNG store write failed'));
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

// The single active warm + the last finished one, module-scoped so the run
// survives the list page unmounting (navigating into an exposure) and so any
// page can render its progress. State changes flow through emit().
let activeWarm: { controller: AbortController; progress: PngWarmProgress } | null = null;
let lastWarmResult: PngWarmResult | null = null;

export function getWarmState(): {
  running: boolean;
  progress: PngWarmProgress | null;
  lastResult: PngWarmResult | null;
} {
  return {
    running: activeWarm !== null,
    progress: activeWarm ? { ...activeWarm.progress } : null,
    lastResult: lastWarmResult,
  };
}

export function cancelPngWarm(): void {
  activeWarm?.controller.abort();
}

/**
 * Download-and-store every id not already held, in the order given (= the
 * list's inspection order, so the exposures the operator will hit first are
 * warmed first — inspecting can start while the warm continues; the run is
 * module state, so navigating into the triage flow does NOT cancel it).
 * Resumable by construction: already-stored ids are skipped, so re-running
 * after a cancel or failure only fetches what's missing. A 404 (exposure has
 * no PNG deployed) is a silent skip, not a failure — those render "No PNG
 * available" in triage regardless. One warm at a time: starting while one is
 * running returns null and leaves the active run alone.
 */
export async function startPngWarm(ids: number[]): Promise<PngWarmResult | null> {
  if (activeWarm) return null;
  await ensurePngStoreReady();
  const todo = ids.filter((i) => !getStoredPng(i));
  const progress: PngWarmProgress = {
    done: ids.length - todo.length,
    total: ids.length,
    bytes: 0,
    failed: 0,
  };
  const controller = new AbortController();
  activeWarm = { controller, progress };
  lastWarmResult = null;
  emit();
  if (!db && todo.length > 0) {
    activeWarm = null;
    lastWarmResult = {
      ...progress, aborted: false,
      error: 'Browser storage is unavailable (private browsing?)',
    };
    emit();
    return lastWarmResult;
  }

  let lastProgressEmit = 0;
  const progressed = () => {
    const now = Date.now();
    if (now - lastProgressEmit >= PROGRESS_EMIT_INTERVAL_MS) {
      lastProgressEmit = now;
      emit();
    }
  };

  let fatal: string | undefined;
  let next = 0;
  const worker = async () => {
    while (!fatal && !controller.signal.aborted) {
      const idx = next++;
      if (idx >= todo.length) return;
      const id = todo[idx];
      try {
        // Resolve first: the route names the byte (full | preview) and, when
        // the delivery front is configured, a content-addressed url the
        // browser can read directly (perf T2-D1, #507). Otherwise the bytes
        // stream through the same route.
        const res = await fetch(`/api/nircam-png?id=${id}&resolve=1`, { signal: controller.signal });
        if (!res.ok) {
          if (res.status !== 404) progress.failed++;
          progress.done++;
          progressed();
          continue;
        }
        const resolved = (await res.json()) as { url: string | null; kind: 'full' | 'preview' };
        const kind = resolved.kind === 'preview' ? 'preview' : 'full';
        let bytesRes = await fetch(resolved.url ?? `/api/nircam-png?id=${id}`, { signal: controller.signal });
        if (!bytesRes.ok && resolved.url) {
          // The front answered but not with bytes (misconfigured or mid-
          // cutover): the same-origin proxy still streams them.
          bytesRes = await fetch(`/api/nircam-png?id=${id}`, { signal: controller.signal });
        }
        if (!bytesRes.ok) {
          if (bytesRes.status !== 404) progress.failed++;
          progress.done++;
          progressed();
          continue;
        }
        const blob = await bytesRes.blob();
        const storedAt = Date.now();
        await putRecord({ id, kind, bytes: blob.size, storedAt, blob });
        dropMemEntry(id); // replace a stale/expired leftover, if any
        mem.set(id, { kind, url: URL.createObjectURL(blob), bytes: blob.size, storedAt });
        announce({ type: 'stored', id });
        progress.bytes += blob.size;
        progress.done++;
        progressed();
      } catch (err) {
        if (controller.signal.aborted) return;
        if (isQuotaError(err)) {
          fatal = 'Browser storage quota exhausted — the warm stored what fit. ' +
            'Clear the cache or free disk space and re-run to continue.';
          return;
        }
        progress.failed++;
        progress.done++;
        progressed();
      }
    }
  };
  await Promise.all(Array.from({ length: WARM_CONCURRENCY }, worker));
  lastWarmResult = { ...progress, aborted: controller.signal.aborted, error: fatal };
  activeWarm = null;
  emit();
  return lastWarmResult;
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
  lastWarmResult = null;
  announce({ type: 'cleared' });
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
