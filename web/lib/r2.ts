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

// Max keys per storage_objects lookup. Chunked so a large observation manifest
// (hundreds–thousands of spectra) can't overflow the PostgREST/Kong URL length
// (the `.in()` list rides in the query string) — mirrors the chunked lookup in
// the Python registry.find_migration_conflicts.
const LOOKUP_CHUNK = 100;

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

/** Where a requested key should be read from: a backend label + the key to sign. */
interface ResolvedObject {
  backend: DataBackend;
  key: string;
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
    keys.map((key) => ({ backend: 'r2' as const, key: toLegacyKeySafe(key) }));

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

    const backendByCanonical = new Map<string, DataBackend>();
    if (canonicalForms.length > 0) {
      const supabase = createServiceClient();
      // Chunked so the `.in()` list never overflows the request URL.
      for (let i = 0; i < canonicalForms.length; i += LOOKUP_CHUNK) {
        const chunk = canonicalForms.slice(i, i + LOOKUP_CHUNK);
        const { data, error } = await supabase
          .from('storage_objects')
          .select('storage_key, backend')
          .in('storage_key', chunk)
          .eq('status', 'active');
        if (error) throw error;
        for (const row of data ?? []) {
          backendByCanonical.set(row.storage_key as string, row.backend as DataBackend);
        }
      }
    }

    return keys.map((key) => {
      const canonical = canonicalByInput.get(key);
      // Only divert to OSN when the registry explicitly homes the canonical key
      // there; every other case reads R2 under the legacy key.
      if (canonical && backendByCanonical.get(canonical) === 'osn') {
        return { backend: 'osn' as const, key: canonical };
      }
      return { backend: 'r2' as const, key: toLegacyKeySafe(key) };
    });
  } catch (err) {
    console.error('[dual-read] backend resolution failed; falling back to R2:', err);
    return r2Only();
  }
}

/** Presign a GET against a resolved object's backend. */
async function presignResolved(o: ResolvedObject, expiresIn: number): Promise<string> {
  const command = new GetObjectCommand({
    Bucket: getBucketNameForBackend(o.backend),
    Key: o.key,
  });
  try {
    return await getSignedUrl(getS3ClientForBackend(o.backend), command, { expiresIn });
  } catch (error) {
    console.error(`Failed to sign download URL for "${o.key}" (${o.backend}):`, error);
    throw new Error(`Failed to generate download URL for ${o.key}`);
  }
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
 */
export async function generateDownloadUrls(
  keys: string[],
  expiresIn: number = 3600
): Promise<string[]> {
  const resolved = await resolveObjectBackends(keys);
  return Promise.all(resolved.map((o) => presignResolved(o, expiresIn)));
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

