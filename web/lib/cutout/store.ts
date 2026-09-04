// Content-addressed cutout store (perf T2-D3, #509; decision D-D).
//
// A cutout is a pure function of (field, imaging version, size, fov, ra, dec),
// yet the three render routes re-rendered ~1,078 times a day at 0.8–2 s each.
// Rendered PNGs now land on the tiles bucket (R2, CDN-fronted, immutable per
// key) under a deterministic key, and a route that finds one there answers
// with a 302 to the CDN url (or streams it) instead of rendering:
//
//   cutouts/<field>/v<version>/<size>/<fov>/<ra>_<dec>.png
//
// `version` is the field's imaging asset version (lib/asset-version.ts): a
// hash over every map_layers.tile_version and the FitsGL dataset's backing
// mosaic hashes, so a field re-deploy moves every cutout to a new prefix and
// the old one is orphaned for the bucket's lifecycle rule to expire (30 days
// on `cutouts/`, set on the bucket — see the PR / DEPLOYMENT notes).
//
// A route stores only what it would serve again under the same key: a FitsGL
// field whose FitsGL render failed and fell back to the legacy composite does
// not store that fallback (it would pin the lower-quality image).
//
// Program-scoped access is the route's job BEFORE it consults the store; the
// stored object is keyed by its inputs, not by a viewer. Only renders from
// public imagery are stored (a draft-backed dataset an admin's RLS can see
// stays a private, uncached render). The tiles bucket itself is public, so a
// stored cutout exposes nothing beyond the tiles it was rendered from — plus
// the coordinates already in its url.
import 'server-only';

import { GetObjectCommand, HeadObjectCommand, PutObjectCommand } from '@aws-sdk/client-s3';
import { after } from 'next/server';
import { getS3Client, getBucketName, getPublicUrlBase } from '@/lib/storage';

/** Sizes the browser routes render at. A request is rounded UP to the next
 * rung (the `<img>` scales down), so the list's 48 px, the pinned bucket's
 * 40 / 20 px and the object page's 600 px collapse onto two stored renders
 * per object instead of four. Above the ladder (the v1 API allows 2048) the
 * requested size is kept exactly. */
export const CUTOUT_SIZE_LADDER = [64, 300, 600] as const;

export function storeSizeFor(requested: number): number {
  for (const s of CUTOUT_SIZE_LADDER) if (requested <= s) return s;
  return Math.round(requested);
}

export interface CutoutStoreInput {
  field: string;
  /** Per-field imaging asset version token (assetVersionFor). */
  version: string;
  size: number;
  fov: number;
  ra: number;
  dec: number;
}

function fovSegment(fov: number): string {
  // "5", "3.2", "12.5": a short canonical decimal (no float noise).
  return String(Number(fov.toFixed(3)));
}

export function cutoutStoreKey(i: CutoutStoreInput): string {
  const field = i.field.toLowerCase().replace(/[^a-z0-9_-]/g, '_');
  const version = i.version.replace(/[^A-Za-z0-9_-]/g, '');
  // 1e-7 deg = 0.36 mas: below the finest pixel any route renders (a
  // 2048 px, 1" cutout is ~0.5 mas/px), so two catalog rows only share a key
  // when they are the same patch of sky to sub-pixel precision.
  const ra = i.ra.toFixed(7);
  const dec = `${i.dec >= 0 ? '+' : ''}${i.dec.toFixed(7)}`;
  return `cutouts/${field}/v${version}/${i.size}/${fovSegment(i.fov)}/${ra}_${dec}.png`;
}

export interface CutoutStoreEntry {
  key: string;
  /** Public CDN url of the stored object. */
  url: string;
}

let availability: { ok: boolean; base?: string } | null = null;

/** The store is usable when the tiles backend and its public url base are
 * configured; otherwise every route renders as before. Resolved once. */
function storeBase(): string | null {
  if (availability === null) {
    try {
      getBucketName('tiles');
      const base = getPublicUrlBase('tiles');
      availability = base ? { ok: true, base: base.replace(/\/+$/, '') } : { ok: false };
      if (!base) console.warn('cutout store off: S3_TILES_PUBLIC_URL_BASE is not set');
    } catch (err) {
      console.warn('cutout store off:', err instanceof Error ? err.message : err);
      availability = { ok: false };
    }
  }
  return availability.ok ? (availability.base as string) : null;
}

/** Test hook. */
export function _resetCutoutStore(): void {
  availability = null;
  known.clear();
}

/** The store entry for an input, or null when the store is not configured. */
export function cutoutStoreFor(i: CutoutStoreInput): CutoutStoreEntry | null {
  const base = storeBase();
  if (!base) return null;
  const key = cutoutStoreKey(i);
  return { key, url: `${base}/${key}` };
}

/** Keys known to exist (rendered or HEAD-verified by this instance), so a hot
 * object costs no storage round trip. Bounded; insertion-order eviction. */
const known = new Set<string>();
const KNOWN_MAX = 20_000;
function remember(key: string): void {
  if (known.size >= KNOWN_MAX) {
    const oldest = known.values().next().value;
    if (oldest !== undefined) known.delete(oldest);
  }
  known.add(key);
}

/** Whether the object is in the store (memo, else one HEAD). Never throws. */
export async function cutoutStoreHas(key: string): Promise<boolean> {
  if (known.has(key)) return true;
  try {
    await getS3Client('tiles').send(new HeadObjectCommand({ Bucket: getBucketName('tiles'), Key: key }));
    remember(key);
    return true;
  } catch {
    return false;
  }
}

/** The stored bytes as a web stream (for routes that must answer with the
 * body itself, e.g. og-image for crawlers), or null when absent. */
export async function cutoutStoreRead(key: string): Promise<ReadableStream | null> {
  try {
    const obj = await getS3Client('tiles').send(new GetObjectCommand({ Bucket: getBucketName('tiles'), Key: key }));
    if (!obj.Body) return null;
    remember(key);
    return obj.Body.transformToWebStream();
  } catch {
    return null;
  }
}

const STORED_CACHE_CONTROL = 'public, max-age=31536000, immutable';

/** Write a freshly rendered cutout after the response is sent (Next `after`);
 * outside a request scope (tests) the put simply runs detached. Failures are
 * logged, never surfaced — the caller already has its bytes. */
export function storeCutoutInBackground(key: string, png: Buffer): void {
  const put = () =>
    getS3Client('tiles')
      .send(new PutObjectCommand({
        Bucket: getBucketName('tiles'),
        Key: key,
        Body: png,
        ContentType: 'image/png',
        CacheControl: STORED_CACHE_CONTROL,
      }))
      .then(() => remember(key))
      .catch((err) => console.error(`cutout store: put failed for ${key}:`, err));
  try {
    after(put);
  } catch {
    void put();
  }
}
