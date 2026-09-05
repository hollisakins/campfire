// Dual-read backend resolution (epic #210 / #215). Exercises resolveObjectBackends
// (the brain) and generateDownloadUrls (presign wiring) with the registry, the
// storage clients, and the presigner all mocked. Uses the REAL ./layout so the
// legacy<->canonical derivation is the production one.

import { describe, it, expect, beforeEach, vi } from 'vitest';

// --- registry (service-role client) -----------------------------------------
let dbRows: Array<{ storage_key: string; backend: string; content_hash?: string; updated_at?: string }> = [];
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
let signCalls: Array<{ expiresIn?: number; signingDate?: Date }> = [];
vi.mock('@aws-sdk/s3-request-presigner', () => ({
  getSignedUrl: (
    _client: unknown,
    command: { input: { Bucket: string; Key: string } },
    options?: { expiresIn?: number; signingDate?: Date },
  ) => {
    signCalls.push({ expiresIn: options?.expiresIn, signingDate: options?.signingDate });
    const date = options?.signingDate ? `?X-Amz-Date=${options.signingDate.toISOString()}` : '';
    return Promise.resolve(`signed://${command.input.Bucket}/${command.input.Key}${date}`);
  },
}));

import {
  resolveObjectBackends,
  generateDownloadUrls,
  presignResolvedStable,
  _resetRegistryMemo,
  stablePresignWindow,
  STABLE_PRESIGN_WINDOW_SECONDS,
} from './r2';

const LEGACY = 'spectra/ember_egs_p1/ember_egs_p1_101_spec.fits';
const CANON = 'data/products/nirspec/ember_egs_p1/ember_egs_p1_101_spec.fits';

beforeEach(() => {
  dbRows = [];
  dbError = null;
  dbCalls = 0;
  signCalls = [];
  _resetRegistryMemo();
  vi.unstubAllEnvs();
});

describe('resolveObjectBackends — kill switch', () => {
  it('OSN_READ_ENABLED off => R2 + input key, no DB query', async () => {
    vi.stubEnv('OSN_READ_ENABLED', '');
    const out = await resolveObjectBackends([LEGACY]);
    expect(out).toEqual([{ backend: 'r2', key: LEGACY, contentHash: null, registeredAt: null }]);
    expect(dbCalls).toBe(0);
  });

  it('OSN_READ_ENABLED off + canonical input => R2 under the LEGACY key (rollback)', async () => {
    vi.stubEnv('OSN_READ_ENABLED', '');
    const out = await resolveObjectBackends([CANON]);
    expect(out).toEqual([{ backend: 'r2', key: LEGACY, contentHash: null, registeredAt: null }]);
    expect(dbCalls).toBe(0);
  });
});

describe('resolveObjectBackends — flag on', () => {
  beforeEach(() => vi.stubEnv('OSN_READ_ENABLED', 'true'));

  it('migrated osn row => OSN + canonical key', async () => {
    dbRows = [{ storage_key: CANON, backend: 'osn', content_hash: 'sha256:aa' }];
    const out = await resolveObjectBackends([LEGACY]);
    expect(out).toEqual([{ backend: 'osn', key: CANON, contentHash: 'sha256:aa', registeredAt: null }]);
    expect(dbCalls).toBe(1);
  });

  it('no registry row => R2 + input (legacy) key', async () => {
    dbRows = [];
    const out = await resolveObjectBackends([LEGACY]);
    expect(out).toEqual([{ backend: 'r2', key: LEGACY, contentHash: null, registeredAt: null }]);
  });

  it('row present but backend r2 => R2 + input key (never sign R2 with a canonical key)', async () => {
    dbRows = [{ storage_key: CANON, backend: 'r2', content_hash: 'sha256:bb' }];
    const out = await resolveObjectBackends([LEGACY]);
    expect(out).toEqual([{ backend: 'r2', key: LEGACY, contentHash: 'sha256:bb', registeredAt: null }]);
  });

  it('already-canonical input + migrated row => OSN + canonical (idempotent)', async () => {
    dbRows = [{ storage_key: CANON, backend: 'osn', content_hash: 'sha256:aa' }];
    const out = await resolveObjectBackends([CANON]);
    expect(out).toEqual([{ backend: 'osn', key: CANON, contentHash: 'sha256:aa', registeredAt: null }]);
  });

  it('DB error => fail open to R2 for all keys', async () => {
    dbError = new Error('supabase down');
    const out = await resolveObjectBackends([LEGACY]);
    expect(out).toEqual([{ backend: 'r2', key: LEGACY, contentHash: null, registeredAt: null }]);
  });

  it('canonical input + DB error => fail open to R2 under the LEGACY key', async () => {
    // The client mirror sends canonical keys post-migration; R2 only has them
    // under the legacy key, so fail-open must re-key canonical -> legacy.
    dbError = new Error('supabase down');
    const out = await resolveObjectBackends([CANON]);
    expect(out).toEqual([{ backend: 'r2', key: LEGACY, contentHash: null, registeredAt: null }]);
  });

  it('chunks the registry lookup for large key sets and still diverts', async () => {
    const N = 250; // > LOOKUP_CHUNK (100) => multiple queries
    const keys = Array.from(
      { length: N },
      (_, i) => `spectra/ember_egs_p1/ember_egs_p1_${i}_spec.fits`
    );
    const migratedCanon = 'data/products/nirspec/ember_egs_p1/ember_egs_p1_123_spec.fits';
    dbRows = [{ storage_key: migratedCanon, backend: 'osn', content_hash: 'sha256:cc' }];

    const out = await resolveObjectBackends(keys);

    expect(out[123]).toEqual({ backend: 'osn', key: migratedCanon, contentHash: 'sha256:cc', registeredAt: null });
    expect(out[0]).toEqual({ backend: 'r2', key: keys[0], contentHash: null, registeredAt: null });
    expect(dbCalls).toBeGreaterThan(1); // not one URL-overflowing .in()
  });

  it('unparseable key => R2 fallback, still resolves the others', async () => {
    dbRows = [{ storage_key: CANON, backend: 'osn', content_hash: 'sha256:aa' }];
    const out = await resolveObjectBackends(['garbage/not/a/key', LEGACY]);
    expect(out[0]).toEqual({ backend: 'r2', key: 'garbage/not/a/key', contentHash: null, registeredAt: null });
    expect(out[1]).toEqual({ backend: 'osn', key: CANON, contentHash: 'sha256:aa', registeredAt: null });
  });
});

describe('generateDownloadUrls — presign wiring', () => {
  beforeEach(() => vi.stubEnv('OSN_READ_ENABLED', 'true'));

  it('signs OSN bucket for a migrated object, R2 for an unmigrated one', async () => {
    const OTHER = 'spectra/ember_egs_p1/ember_egs_p1_999_spec.fits';
    dbRows = [{ storage_key: CANON, backend: 'osn', content_hash: 'sha256:aa' }]; // only LEGACY migrated
    const urls = await generateDownloadUrls([LEGACY, OTHER]);
    expect(urls[0]).toBe(`signed://campfire-jwst/${CANON}`);
    expect(urls[1]).toBe(`signed://campfire/${OTHER}`);
  });
});

describe('resolveObjectBackends — registry memo (perf T2-D1)', () => {
  beforeEach(() => vi.stubEnv('OSN_READ_ENABLED', 'true'));

  it('a second resolution of a registered key within the TTL makes no DB query', async () => {
    dbRows = [{ storage_key: CANON, backend: 'osn', content_hash: 'sha256:aa' }];
    await resolveObjectBackends([LEGACY]);
    expect(dbCalls).toBe(1);
    const out = await resolveObjectBackends([CANON]);
    expect(out).toEqual([{ backend: 'osn', key: CANON, contentHash: 'sha256:aa', registeredAt: null }]);
    expect(dbCalls).toBe(1);
  });

  it('a miss (no row) is not memoized — a deploy in flight resolves live next time', async () => {
    dbRows = [];
    await resolveObjectBackends([LEGACY]);
    dbRows = [{ storage_key: CANON, backend: 'osn', content_hash: 'sha256:aa' }];
    const out = await resolveObjectBackends([LEGACY]);
    expect(out).toEqual([{ backend: 'osn', key: CANON, contentHash: 'sha256:aa', registeredAt: null }]);
    expect(dbCalls).toBe(2);
  });

  it('memo expires after the TTL', async () => {
    vi.useFakeTimers();
    try {
      dbRows = [{ storage_key: CANON, backend: 'osn', content_hash: 'sha256:aa' }];
      await resolveObjectBackends([LEGACY]);
      vi.advanceTimersByTime(61_000);
      await resolveObjectBackends([LEGACY]);
      expect(dbCalls).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('resolveObjectBackends — registration time', () => {
  it('carries storage_objects.updated_at as unix seconds, null without a row', async () => {
    vi.stubEnv('OSN_READ_ENABLED', 'true');
    dbRows = [{ storage_key: CANON, backend: 'osn', content_hash: 'sha256:aa', updated_at: '2026-09-04T12:00:00+00:00' }];
    const [withRow, withoutRow] = await resolveObjectBackends([CANON, 'data/products/nirspec/other/x_spec.fits']);
    expect(withRow.registeredAt).toBe(Date.parse('2026-09-04T12:00:00Z') / 1000);
    expect(withoutRow.registeredAt).toBeNull();
  });
});

describe('stablePresignWindow', () => {
  it('aligns to the window and expires two windows out', () => {
    const w = STABLE_PRESIGN_WINDOW_SECONDS;
    const t = 1_757_000_123_000; // arbitrary ms
    const { start, exp } = stablePresignWindow(t);
    expect(start % w).toBe(0);
    expect(start).toBeLessThanOrEqual(t / 1000);
    expect(t / 1000 - start).toBeLessThan(w);
    expect(exp - start).toBe(2 * w);
  });

  it('two instants in the same window share a start', () => {
    const w = STABLE_PRESIGN_WINDOW_SECONDS * 1000;
    const base = Math.floor(1_757_000_123_000 / w) * w;
    expect(stablePresignWindow(base + 1000).start).toBe(stablePresignWindow(base + w - 1000).start);
    expect(stablePresignWindow(base + w).start).not.toBe(stablePresignWindow(base).start);
  });
});

describe('presignResolvedStable', () => {
  it('signs on the window start with a two-window validity, so mints within a window are byte-identical', async () => {
    const w = STABLE_PRESIGN_WINDOW_SECONDS * 1000;
    const base = Math.floor(1_757_000_123_000 / w) * w;
    vi.useFakeTimers();
    try {
      vi.setSystemTime(base + 1000);
      const a = await presignResolvedStable({ backend: 'osn', key: CANON, contentHash: 'sha256:aa', registeredAt: null });
      vi.setSystemTime(base + w - 1000);
      const b = await presignResolvedStable({ backend: 'osn', key: CANON, contentHash: 'sha256:aa', registeredAt: null });
      expect(a.url).toBe(b.url);
      expect(a.exp).toBe(b.exp);
      expect(a.exp).toBe(base / 1000 + 2 * STABLE_PRESIGN_WINDOW_SECONDS);
      expect(signCalls).toHaveLength(2);
      for (const call of signCalls) {
        expect(call.signingDate?.getTime()).toBe(base);
        expect(call.expiresIn).toBe(2 * STABLE_PRESIGN_WINDOW_SECONDS);
      }

      // The next window is a different url: the signing date moved.
      vi.setSystemTime(base + w + 1000);
      const c = await presignResolvedStable({ backend: 'osn', key: CANON, contentHash: 'sha256:aa', registeredAt: null });
      expect(c.url).not.toBe(a.url);
    } finally {
      vi.useRealTimers();
    }
  });

  it('signs an R2-homed object on the current time (window-dated signatures are only verified on OSN)', async () => {
    const w = STABLE_PRESIGN_WINDOW_SECONDS * 1000;
    const base = Math.floor(1_757_000_123_000 / w) * w;
    vi.useFakeTimers();
    try {
      vi.setSystemTime(base + 5000);
      const r = await presignResolvedStable({ backend: 'r2', key: LEGACY, contentHash: 'sha256:aa', registeredAt: null });
      expect(signCalls).toHaveLength(1);
      expect(signCalls[0].signingDate).toBeUndefined();
      expect(signCalls[0].expiresIn).toBe(2 * STABLE_PRESIGN_WINDOW_SECONDS);
      expect(r.exp).toBe((base + 5000) / 1000 + 2 * STABLE_PRESIGN_WINDOW_SECONDS);
      expect(r.url).toBe(`signed://campfire/${LEGACY}`);
    } finally {
      vi.useRealTimers();
    }
  });
});
