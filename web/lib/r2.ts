// Storage client helpers for the `data` bucket (FITS, RGB, SED, …).
//
// Endpoint/region/addressing-style are resolved per-purpose by the storage
// backend factory (`./storage`), so this layer is backend-agnostic.
//
// DUAL-READ (epic #210 / #215): an object's home (R2 or OSN) is recorded in
// storage_objects.backend. `generateDownloadUrl` resolves each key's backend
// from the registry and presigns there, falling back to R2. This is gated by
// the OSN_READ_ENABLED env flag — when unset/false it short-circuits to today's
// behavior (presign R2 with the input key, no DB query), so the change is inert
// until creds + flag are set. Resolution fails OPEN to R2 on any error.

import { GetObjectCommand } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import {
  getS3Client,
  getBucketName,
  getS3ClientForBackend,
  getBucketNameForBackend,
  type DataBackend,
} from './storage';
import { storageKey, toCanonicalKey, toLegacyKey } from './layout';
import { createServiceClient } from './supabase/server';

// Max keys per storage_objects lookup. The `.in()` list rides in the request URL,
// and canonical keys are ~70+ chars (URL-encoded ~110), so this is kept small to
// stay well under the PostgREST/Kong URI limit — overflowing it yields a bare
// 400 'Bad Request'. A large observation manifest is split across several queries.
const LOOKUP_CHUNK = 50;

/** Legacy form of a key for R2 reads; identity on unrecognized keys (fail-safe).
 * R2 retains objects under their LEGACY key, so every R2-bound presign must sign
 * the legacy form — including canonical keys arriving from the client's
 * storage_objects mirror during a rollback or a fail-open. */
function toLegacyKeySafe(key: string): string {
  try {
    return toLegacyKey(key);
  } catch {
    return key;
  }
}

/** S3 client for the `data` storage backend (lazy, cached). */
export function getDataClient() {
  return getS3Client('data');
}

/** Where a requested key should be read from: a backend label + the key to
 * sign, plus the registry's content identity (null when unregistered or when
 * resolution failed open) — what the delivery front keys its cache on. */
export interface ResolvedObject {
  backend: DataBackend;
  key: string;
  contentHash: string | null;
  /** When the registry row was last (re)registered — storage_objects.updated_at
   * as unix seconds; bumps on every re-deploy, after the bytes landed. The
   * delivery front binds it into the object token so the Worker can tell an
   * in-place overwrite (upstream Last-Modified newer than this) from the
   * bytes the hash names. null when the key has no active row. */
  registeredAt: number | null;
}

// ---------------------------------------------------------------------------
// Registry memo (perf T2-D1, #507 / audit CA-16). Every presign used to cost a
// storage_objects round trip; the routes that mint delivery-front urls do it
// per request. A canonical key's backend + content_hash change only on a
// re-deploy, and the deploy invariant is "bytes land before the new hash is
// registered", so a 60 s-stale row can at worst serve the previous, still
// valid product once more. Negative results are NOT memoized: a key that has
// no row yet (deploy in flight) must resolve live on the next request.
// ---------------------------------------------------------------------------

const REGISTRY_MEMO_TTL_MS = 60_000;
const REGISTRY_MEMO_MAX = 4096;

interface RegistryEntry {
  backend: DataBackend;
  contentHash: string;
  registeredAt: number | null;
  at: number;
}

const registryMemo = new Map<string, RegistryEntry>();

function memoGet(canonical: string): RegistryEntry | undefined {
  const e = registryMemo.get(canonical);
  if (!e) return undefined;
  if (Date.now() - e.at > REGISTRY_MEMO_TTL_MS) {
    registryMemo.delete(canonical);
    return undefined;
  }
  return e;
}

function memoSet(canonical: string, entry: RegistryEntry): void {
  if (registryMemo.size >= REGISTRY_MEMO_MAX) {
    const oldest = registryMemo.keys().next().value;
    if (oldest !== undefined) registryMemo.delete(oldest);
  }
  registryMemo.set(canonical, entry);
}

/** Test hook: forget every memoized registry row. */
export function _resetRegistryMemo(): void {
  registryMemo.clear();
}

/** Dual-read master switch. Off (default) => behave exactly like pre-migration. */
function osnReadEnabled(): boolean {
  return (process.env.OSN_READ_ENABLED || '').trim().toLowerCase() === 'true';
}

/**
 * Resolve, for each input key, which backend holds the object and the key to
 * sign there. Canonical-only lookup: each input is mapped to its canonical form
 * and looked up in storage_objects; a matching `osn` row => read OSN with the
 * canonical key; anything else (no row, or an r2 row) => read R2 with the input
 * key (R2 bytes are retained, so this is always a safe answer).
 *
 * Fails OPEN: with OSN reads disabled, or on any derivation/DB error, returns
 * R2 + the input key for every entry — never converts a today-working download
 * into an error.
 */
export async function resolveObjectBackends(keys: string[]): Promise<ResolvedObject[]> {
  // R2 reads always use the LEGACY key (where R2 retains the bytes), even if the
  // caller passed a canonical key (the client mirror holds canonical keys after
  // migration). This is the rollback / fail-open answer for every key.
  const r2Only = (): ResolvedObject[] =>
    keys.map((key) => ({ backend: 'r2' as const, key: toLegacyKeySafe(key), contentHash: null, registeredAt: null }));

  if (!osnReadEnabled()) return r2Only();

  try {
    // Canonical form per input (LayoutError on an unrecognized key => R2 fallback).
    const canonicalByInput = new Map<string, string | null>();
    for (const k of keys) {
      try {
        canonicalByInput.set(k, toCanonicalKey(k));
      } catch {
        canonicalByInput.set(k, null);
      }
    }

    const canonicalForms = Array.from(
      new Set(
        Array.from(canonicalByInput.values()).filter((v): v is string => v !== null)
      )
    );

    const rowByCanonical = new Map<string, RegistryEntry>();
    const toLookup: string[] = [];
    for (const c of canonicalForms) {
      const memo = memoGet(c);
      if (memo) rowByCanonical.set(c, memo);
      else toLookup.push(c);
    }
    if (toLookup.length > 0) {
      const supabase = createServiceClient();
      // Chunked so the `.in()` list never overflows the request URL.
      for (let i = 0; i < toLookup.length; i += LOOKUP_CHUNK) {
        const chunk = toLookup.slice(i, i + LOOKUP_CHUNK);
        const { data, error } = await supabase
          .from('storage_objects')
          .select('storage_key, backend, content_hash, updated_at')
          .in('storage_key', chunk)
          .eq('status', 'active');
        if (error) throw error;
        for (const row of data ?? []) {
          const registeredMs = row.updated_at ? Date.parse(String(row.updated_at)) : NaN;
          const entry: RegistryEntry = {
            backend: row.backend as DataBackend,
            contentHash: String(row.content_hash),
            registeredAt: Number.isFinite(registeredMs) ? Math.floor(registeredMs / 1000) : null,
            at: Date.now(),
          };
          rowByCanonical.set(row.storage_key as string, entry);
          memoSet(row.storage_key as string, entry);
        }
      }
    }

    return keys.map((key) => {
      const canonical = canonicalByInput.get(key);
      const row = canonical ? rowByCanonical.get(canonical) : undefined;
      // Only divert to OSN when the registry explicitly homes the canonical key
      // there; every other case reads R2 under the legacy key. The content
      // identity rides along either way (an r2 row is still the same bytes).
      if (canonical && row?.backend === 'osn') {
        return { backend: 'osn' as const, key: canonical, contentHash: row.contentHash, registeredAt: row.registeredAt };
      }
      return {
        backend: 'r2' as const,
        key: toLegacyKeySafe(key),
        contentHash: row?.contentHash ?? null,
        registeredAt: row?.registeredAt ?? null,
      };
    });
  } catch (err) {
    console.error('[dual-read] backend resolution failed; falling back to R2:', err);
    return r2Only();
  }
}

/**
 * Attachment filename for a key: its basename, restricted to a safe charset by
 * construction (canonical keys already are; this guards header syntax anyway).
 */
function attachmentFilename(key: string): string {
  const base = key.split('/').pop() || key;
  return base.replace(/[^\w.+-]/g, '_');
}

/** Presign a GET against a resolved object's backend. When `attachmentName` is
 * set, a signed `response-content-disposition` override makes the object store
 * answer with `attachment; filename="…"` — required for browser-navigation
 * downloads, where the anchor `download` attribute is ignored cross-origin and
 * the proxy Worker's URL path would otherwise name every file "proxy". */
async function presignResolved(
  o: ResolvedObject,
  expiresIn: number,
  attachmentName?: string,
  signingDate?: Date,
): Promise<string> {
  const command = new GetObjectCommand({
    Bucket: getBucketNameForBackend(o.backend),
    Key: o.key,
    ...(attachmentName
      ? { ResponseContentDisposition: `attachment; filename="${attachmentName}"` }
      : {}),
  });
  try {
    return await getSignedUrl(getS3ClientForBackend(o.backend), command, {
      expiresIn,
      ...(signingDate ? { signingDate } : {}),
    });
  } catch (error) {
    console.error(`Failed to sign download URL for "${o.key}" (${o.backend}):`, error);
    throw new Error(`Failed to generate download URL for ${o.key}`);
  }
}

// ---------------------------------------------------------------------------
// Stable presigns (perf T2-D1, #507). A SigV4 presigned url embeds its signing
// time, so two mints seconds apart are different urls and no browser or CDN
// cache ever hits across page loads. Signing on a fixed time window instead
// (signingDate = the window start, validity = two windows) makes every mint
// within a window byte-identical, and a url minted at the very end of a
// window still lives a full window more. OSN (Ceph RGW) accepts a signing
// date that far in the past — verified 2026-09-04 with a 5 h 55 min-old
// signature against the 6 h window.
// ---------------------------------------------------------------------------

export const STABLE_PRESIGN_WINDOW_SECONDS = 6 * 3600;

/** The current stable window: its start (unix s) and the expiry a url signed
 * on it gets (two windows out). */
export function stablePresignWindow(nowMs: number = Date.now()): { start: number; exp: number } {
  const w = STABLE_PRESIGN_WINDOW_SECONDS;
  const start = Math.floor(nowMs / 1000 / w) * w;
  return { start, exp: start + 2 * w };
}

/** Presign a resolved object on the current stable window. Same inputs within
 * a window => the same url; `exp` is when it stops being valid.
 *
 * OSN only: a signing date hours in the past was verified against OSN (Ceph
 * RGW), not against R2, and the objects still homed there (the legacy
 * exposure PNGs) are the ones the exposure `<img>` sources front with no
 * per-image fallback. An R2 object is therefore signed on the current time
 * with the same lifetime — no worse than the per-mint presign it had before,
 * and the Worker still caches it per content hash; only the browser's own
 * cache stops hitting across page loads. Drop the branch once R2 is verified
 * to accept a window-dated signature. */
export async function presignResolvedStable(o: ResolvedObject): Promise<{ url: string; exp: number }> {
  const lifetime = 2 * STABLE_PRESIGN_WINDOW_SECONDS;
  if (o.backend === 'r2') {
    const url = await presignResolved(o, lifetime);
    return { url, exp: Math.floor(Date.now() / 1000) + lifetime };
  }
  const { start, exp } = stablePresignWindow();
  const url = await presignResolved(o, lifetime, undefined, new Date(start * 1000));
  return { url, exp };
}

/**
 * Generate a signed URL for downloading a file from the data store, reading from
 * whichever backend currently homes the object (R2 fallback). See
 * {@link resolveObjectBackends}.
 * @param fitsPath - Object key (e.g., "spectra/obs_name/file.fits")
 * @param expiresIn - URL expiration time in seconds (default: 1 hour)
 * @throws if signing fails — surfaced loudly so a cutover misconfig is
 *   diagnosable rather than silently masked.
 */
export async function generateDownloadUrl(
  fitsPath: string,
  expiresIn: number = 3600
): Promise<string> {
  const [resolved] = await resolveObjectBackends([fitsPath]);
  return presignResolved(resolved, expiresIn);
}

/**
 * Batched dual-read presign: resolves all backends in a single registry query,
 * then signs in parallel. Use this for routes that presign many keys at once
 * (manifest, batch download, the client's storage presign) to avoid one DB
 * round-trip per key.
 *
 * Pass `attachment: true` for URLs a browser will NAVIGATE to (per-row
 * downloads): the store then answers with `attachment; filename="<basename>"`
 * and the proxy forwards it, so the save dialog gets the real product name.
 * Leave it off for programmatic fetches (zip, JSON, <img> sources).
 */
export async function generateDownloadUrls(
  keys: string[],
  expiresIn: number = 3600,
  opts: { attachment?: boolean } = {}
): Promise<string[]> {
  const resolved = await resolveObjectBackends(keys);
  return Promise.all(
    resolved.map((o) =>
      presignResolved(o, expiresIn, opts.attachment ? attachmentFilename(o.key) : undefined),
    ),
  );
}

/**
 * Generate multiple download URLs for an object's spectra.
 * @param fitsPaths - Array of FITS file paths
 * @returns Array of signed URLs
 */
export async function generateMultipleDownloadUrls(
  fitsPaths: string[]
): Promise<string[]> {
  return generateDownloadUrls(fitsPaths);
}

/**
 * Extract observation name from target_id
 * Pattern: {observation}_{number} -> {observation}
 * Example: ember_uds_p4_1018 -> ember_uds_p4
 * @param targetId - Full target ID
 * @returns Observation name (everything before the last underscore and number)
 */
export function extractObservationName(targetId: string): string {
  // Split by underscore and find the last part
  const parts = targetId.split('_');

  // Check if the last part is a number
  const lastPart = parts[parts.length - 1];
  if (/^\d+$/.test(lastPart)) {
    // If it's a number, remove it and join the rest
    return parts.slice(0, -1).join('_');
  }

  // If not a number pattern, return the full target_id as observation name
  return targetId;
}

/**
 * Generate RGB image path in R2 for an object
 * Pattern: rgb/{observation}/{object_id}_rgb.png
 * Example: rgb/ember_uds_p4/ember_uds_p4_1018_rgb.png
 * @param targetId - Full target ID
 * @returns R2 path to RGB image
 */
export function generateRGBImagePath(targetId: string): string {
  const observation = extractObservationName(targetId);
  return storageKey('rgb', { obs: observation }, `${targetId}_rgb.png`);
}

/**
 * Generate a signed URL for an RGB image from R2
 * @param targetId - Full target ID
 * @param expiresIn - URL expiration time in seconds (default: 1 hour)
 * @returns Signed URL for the RGB image
 */
export async function generateRGBImageUrl(
  targetId: string,
  expiresIn: number = 3600
): Promise<string> {
  const rgbPath = generateRGBImagePath(targetId);
  return generateDownloadUrl(rgbPath, expiresIn);
}

/**
 * Generate SED plot path in R2 for an object
 * Pattern: sed/{observation}/{object_id}_sed.pdf
 * Example: sed/ember_uds_p4/ember_uds_p4_1018_sed.pdf
 * @param targetId - Full target ID
 * @returns R2 path to SED plot PDF
 */
export function generateSEDPlotPath(targetId: string): string {
  const observation = extractObservationName(targetId);
  return storageKey('sed', { obs: observation }, `${targetId}_sed.pdf`);
}

