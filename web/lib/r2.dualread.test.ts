// Dual-read backend resolution (epic #210 / #215). Exercises resolveObjectBackends
// (the brain) and generateDownloadUrls (presign wiring) with the registry, the
// storage clients, and the presigner all mocked. Uses the REAL ./layout so the
// legacy<->canonical derivation is the production one.

import { describe, it, expect, beforeEach, vi } from 'vitest';

// --- registry (service-role client) -----------------------------------------
let dbRows: Array<{ storage_key: string; backend: string }> = [];
let dbError: unknown = null;
let dbCalls = 0;

vi.mock('./supabase/server', () => ({
  createServiceClient: () => {
    const b: Record<string, unknown> = {
      from: () => {
        dbCalls += 1;
        return b;
      },
      select: () => b,
      in: () => b,
      eq: () => b,
      then: (resolve: (v: { data: unknown; error: unknown }) => unknown) =>
        resolve({ data: dbRows, error: dbError }),
    };
    return b;
  },
}));

// --- storage clients ---------------------------------------------------------
vi.mock('./storage', () => ({
  getS3Client: () => ({}),
  getBucketName: () => 'campfire',
  getS3ClientForBackend: (b: string) => ({ backend: b }),
  getBucketNameForBackend: (b: string) => (b === 'osn' ? 'campfire-jwst' : 'campfire'),
}));

// --- AWS SDK -----------------------------------------------------------------
vi.mock('@aws-sdk/client-s3', () => ({
  GetObjectCommand: class {
    input: { Bucket: string; Key: string };
    constructor(input: { Bucket: string; Key: string }) {
      this.input = input;
    }
  },
}));
vi.mock('@aws-sdk/s3-request-presigner', () => ({
  getSignedUrl: (_client: unknown, command: { input: { Bucket: string; Key: string } }) =>
    Promise.resolve(`signed://${command.input.Bucket}/${command.input.Key}`),
}));

import { resolveObjectBackends, generateDownloadUrls } from './r2';

const LEGACY = 'spectra/ember_egs_p1/ember_egs_p1_101_spec.fits';
const CANON = 'data/products/nirspec/ember_egs_p1/ember_egs_p1_101_spec.fits';

beforeEach(() => {
  dbRows = [];
  dbError = null;
  dbCalls = 0;
  vi.unstubAllEnvs();
});

describe('resolveObjectBackends — kill switch', () => {
  it('OSN_READ_ENABLED off => R2 + input key, no DB query', async () => {
    vi.stubEnv('OSN_READ_ENABLED', '');
    const out = await resolveObjectBackends([LEGACY]);
    expect(out).toEqual([{ backend: 'r2', key: LEGACY }]);
    expect(dbCalls).toBe(0);
  });

  it('OSN_READ_ENABLED off + canonical input => R2 under the LEGACY key (rollback)', async () => {
    vi.stubEnv('OSN_READ_ENABLED', '');
    const out = await resolveObjectBackends([CANON]);
    expect(out).toEqual([{ backend: 'r2', key: LEGACY }]);
    expect(dbCalls).toBe(0);
  });
});

describe('resolveObjectBackends — flag on', () => {
  beforeEach(() => vi.stubEnv('OSN_READ_ENABLED', 'true'));

  it('migrated osn row => OSN + canonical key', async () => {
    dbRows = [{ storage_key: CANON, backend: 'osn' }];
    const out = await resolveObjectBackends([LEGACY]);
    expect(out).toEqual([{ backend: 'osn', key: CANON }]);
    expect(dbCalls).toBe(1);
  });

  it('no registry row => R2 + input (legacy) key', async () => {
    dbRows = [];
    const out = await resolveObjectBackends([LEGACY]);
    expect(out).toEqual([{ backend: 'r2', key: LEGACY }]);
  });

  it('row present but backend r2 => R2 + input key (never sign R2 with a canonical key)', async () => {
    dbRows = [{ storage_key: CANON, backend: 'r2' }];
    const out = await resolveObjectBackends([LEGACY]);
    expect(out).toEqual([{ backend: 'r2', key: LEGACY }]);
  });

  it('already-canonical input + migrated row => OSN + canonical (idempotent)', async () => {
    dbRows = [{ storage_key: CANON, backend: 'osn' }];
    const out = await resolveObjectBackends([CANON]);
    expect(out).toEqual([{ backend: 'osn', key: CANON }]);
  });

  it('DB error => fail open to R2 for all keys', async () => {
    dbError = new Error('supabase down');
    const out = await resolveObjectBackends([LEGACY]);
    expect(out).toEqual([{ backend: 'r2', key: LEGACY }]);
  });

  it('canonical input + DB error => fail open to R2 under the LEGACY key', async () => {
    // The client mirror sends canonical keys post-migration; R2 only has them
    // under the legacy key, so fail-open must re-key canonical -> legacy.
    dbError = new Error('supabase down');
    const out = await resolveObjectBackends([CANON]);
    expect(out).toEqual([{ backend: 'r2', key: LEGACY }]);
  });

  it('chunks the registry lookup for large key sets and still diverts', async () => {
    const N = 250; // > LOOKUP_CHUNK (100) => multiple queries
    const keys = Array.from(
      { length: N },
      (_, i) => `spectra/ember_egs_p1/ember_egs_p1_${i}_spec.fits`
    );
    const migratedCanon = 'data/products/nirspec/ember_egs_p1/ember_egs_p1_123_spec.fits';
    dbRows = [{ storage_key: migratedCanon, backend: 'osn' }];

    const out = await resolveObjectBackends(keys);

    expect(out[123]).toEqual({ backend: 'osn', key: migratedCanon });
    expect(out[0]).toEqual({ backend: 'r2', key: keys[0] });
    expect(dbCalls).toBeGreaterThan(1); // not one URL-overflowing .in()
  });

  it('unparseable key => R2 fallback, still resolves the others', async () => {
    dbRows = [{ storage_key: CANON, backend: 'osn' }];
    const out = await resolveObjectBackends(['garbage/not/a/key', LEGACY]);
    expect(out[0]).toEqual({ backend: 'r2', key: 'garbage/not/a/key' });
    expect(out[1]).toEqual({ backend: 'osn', key: CANON });
  });
});

describe('generateDownloadUrls — presign wiring', () => {
  beforeEach(() => vi.stubEnv('OSN_READ_ENABLED', 'true'));

  it('signs OSN bucket for a migrated object, R2 for an unmigrated one', async () => {
    const OTHER = 'spectra/ember_egs_p1/ember_egs_p1_999_spec.fits';
    dbRows = [{ storage_key: CANON, backend: 'osn' }]; // only LEGACY migrated
    const urls = await generateDownloadUrls([LEGACY, OTHER]);
    expect(urls[0]).toBe(`signed://campfire-jwst/${CANON}`);
    expect(urls[1]).toBe(`signed://campfire/${OTHER}`);
  });
});
