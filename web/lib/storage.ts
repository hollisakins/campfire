// Per-purpose S3 storage backend resolution (web side).
//
// Centralizes S3 client construction so the endpoint, region, and addressing
// style are config concerns, not hardcoded Cloudflare R2 details. Each logical
// `purpose` ("data" or "tiles") resolves to its own backend block from
// environment variables, enabling the data->OSN / tiles->R2 split.
//
// Env vars: neutral `S3_*` names are canonical; legacy `R2_*` names are kept
// as backward-compatible aliases. For tiles, the names carry a `TILES_` infix
// (`S3_TILES_*` / `R2_TILES_*`).
//
// Resolution is lazy (on first call) so importing this module — or building
// the app without credentials — never throws.

import { S3Client } from '@aws-sdk/client-s3';

export type StoragePurpose = 'data' | 'tiles';

export interface BackendConfig {
  purpose: StoragePurpose;
  backend: 'r2' | 'osn';
  endpoint: string;
  region: string;
  bucket: string;
  accessKeyId: string;
  secretAccessKey: string;
  forcePathStyle: boolean;
  publicUrlBase?: string;
}

/** First non-empty value among the given env var names (canonical first). */
function env(...names: string[]): string | undefined {
  for (const name of names) {
    const val = process.env[name];
    if (val) return val;
  }
  return undefined;
}

function coerceBool(val: string | undefined): boolean {
  if (!val) return false;
  return ['1', 'true', 'yes', 'on'].includes(val.trim().toLowerCase());
}

function resolveEndpoint(
  purpose: StoragePurpose,
  accountId?: string,
  endpoint?: string,
): string {
  if (endpoint) return endpoint.replace(/\/+$/, '');
  if (accountId) return `https://${accountId}.r2.cloudflarestorage.com`;
  throw new Error(
    `Cannot resolve S3 endpoint for "${purpose}" storage: set either ` +
      `S3${purpose === 'tiles' ? '_TILES' : ''}_ENDPOINT or ` +
      `R2${purpose === 'tiles' ? '_TILES' : ''}_ACCOUNT_ID.`,
  );
}

/**
 * Resolve the backend configuration for a logical storage purpose.
 * Throws if credentials are not configured.
 */
export function resolveBackend(purpose: StoragePurpose = 'data'): BackendConfig {
  const p = purpose === 'tiles' ? 'TILES_' : '';

  const accountId = env(`S3_${p}ACCOUNT_ID`, `R2_${p}ACCOUNT_ID`);
  const endpointRaw = env(`S3_${p}ENDPOINT`, `R2_${p}ENDPOINT`);
  const accessKeyId = env(`S3_${p}ACCESS_KEY_ID`, `R2_${p}ACCESS_KEY_ID`);
  const secretAccessKey = env(`S3_${p}SECRET_ACCESS_KEY`, `R2_${p}SECRET_ACCESS_KEY`);
  const bucket = env(`S3_${p}BUCKET_NAME`, `R2_${p}BUCKET_NAME`);
  const region = env(`S3_${p}REGION`, `R2_${p}REGION`) || 'auto';
  const forcePathStyle = coerceBool(env(`S3_${p}FORCE_PATH_STYLE`, `R2_${p}FORCE_PATH_STYLE`));
  const publicUrlBase = env(
    `S3_${p}PUBLIC_URL_BASE`,
    `R2_${p}PUBLIC_URL_BASE`,
    `R2_${p}PUBLIC_URL`,
  );

  if (!accessKeyId || !secretAccessKey || !bucket) {
    throw new Error(`Storage "${purpose}" credentials not configured`);
  }

  const endpoint = resolveEndpoint(purpose, accountId, endpointRaw);
  const backend =
    (env(`S3_${p}BACKEND`, `R2_${p}BACKEND`) as 'r2' | 'osn' | undefined) ||
    (endpoint.includes('r2.cloudflarestorage.com') ? 'r2' : 'osn');

  return {
    purpose,
    backend,
    endpoint,
    region,
    bucket,
    accessKeyId,
    secretAccessKey,
    forcePathStyle,
    publicUrlBase,
  };
}

const _clients = new Map<StoragePurpose, S3Client>();

/** Lazily-built, cached S3 client for a logical storage purpose. */
export function getS3Client(purpose: StoragePurpose = 'data'): S3Client {
  const cached = _clients.get(purpose);
  if (cached) return cached;

  const b = resolveBackend(purpose);
  const client = new S3Client({
    region: b.region,
    endpoint: b.endpoint,
    forcePathStyle: b.forcePathStyle,
    credentials: {
      accessKeyId: b.accessKeyId,
      secretAccessKey: b.secretAccessKey,
    },
  });
  _clients.set(purpose, client);
  return client;
}

/** Bucket name for a logical storage purpose. */
export function getBucketName(purpose: StoragePurpose = 'data'): string {
  return resolveBackend(purpose).bucket;
}

/** Public URL base for a logical storage purpose, if configured. */
export function getPublicUrlBase(purpose: StoragePurpose = 'data'): string | undefined {
  return resolveBackend(purpose).publicUrlBase;
}

// ---------------------------------------------------------------------------
// By-backend resolution for the data bucket (dual-read, epic #210 / #215).
//
// During the R2->OSN migration both backends are live: each object's home is
// recorded in storage_objects.backend, and the web reads from whichever the
// registry says (R2 fallback). `'r2'` reuses the existing `data` purpose (R2
// today); `'osn'` reads the parallel S3_OSN_* env block (region us-east-1,
// path-style). Inert until S3_OSN_* is set + OSN_READ_ENABLED is flipped on.
// ---------------------------------------------------------------------------

export type DataBackend = 'r2' | 'osn';

/** Resolve the OSN data backend from S3_OSN_* env (no R2 alias; this is new). */
function resolveOsnBackend(): BackendConfig {
  const accessKeyId = env('S3_OSN_ACCESS_KEY_ID');
  const secretAccessKey = env('S3_OSN_SECRET_ACCESS_KEY');
  const bucket = env('S3_OSN_BUCKET_NAME');
  const endpointRaw = env('S3_OSN_ENDPOINT');
  const region = env('S3_OSN_REGION') || 'auto';
  const forcePathStyle = coerceBool(env('S3_OSN_FORCE_PATH_STYLE'));

  if (!accessKeyId || !secretAccessKey || !bucket || !endpointRaw) {
    throw new Error('Storage "osn" credentials not configured (set S3_OSN_*)');
  }

  const endpoint = endpointRaw.replace(/\/+$/, '');
  const backend =
    (env('S3_OSN_BACKEND') as 'r2' | 'osn' | undefined) ||
    (endpoint.includes('r2.cloudflarestorage.com') ? 'r2' : 'osn');

  return {
    purpose: 'data',
    backend,
    endpoint,
    region,
    bucket,
    accessKeyId,
    secretAccessKey,
    forcePathStyle,
  };
}

const _osnClient = new Map<'osn', S3Client>();

/** S3 client for a data-bucket backend label ('r2' => the `data` purpose). */
export function getS3ClientForBackend(b: DataBackend): S3Client {
  if (b === 'r2') return getS3Client('data');
  const cached = _osnClient.get('osn');
  if (cached) return cached;
  const cfg = resolveOsnBackend();
  const client = new S3Client({
    region: cfg.region,
    endpoint: cfg.endpoint,
    forcePathStyle: cfg.forcePathStyle,
    credentials: { accessKeyId: cfg.accessKeyId, secretAccessKey: cfg.secretAccessKey },
  });
  _osnClient.set('osn', client);
  return client;
}

/** Bucket name for a data-bucket backend label. */
export function getBucketNameForBackend(b: DataBackend): string {
  return b === 'r2' ? getBucketName('data') : resolveOsnBackend().bucket;
}
