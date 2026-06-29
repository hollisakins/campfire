// Real-execution coverage for the dual-read storage resolution (epic #210 / #215)
// — exercises the actual S3_OSN_* env resolution, client cache, and r2-vs-osn
// bucket selection in storage.ts (the TS twin of test_storage_backend.py's OSN
// cases), which r2.dualread.test.ts mocks out. Modules are reset per test so the
// internal client caches don't leak across cases.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

function setOsnEnv() {
  vi.stubEnv('S3_OSN_ACCESS_KEY_ID', 'oak');
  vi.stubEnv('S3_OSN_SECRET_ACCESS_KEY', 'osk');
  vi.stubEnv('S3_OSN_BUCKET_NAME', 'campfire-jwst');
  vi.stubEnv('S3_OSN_ENDPOINT', 'https://uaz1.osn.mghpcc.org/');
  vi.stubEnv('S3_OSN_REGION', 'us-east-1');
  vi.stubEnv('S3_OSN_FORCE_PATH_STYLE', 'true');
}

function setR2Env() {
  vi.stubEnv('S3_ACCESS_KEY_ID', 'ak');
  vi.stubEnv('S3_SECRET_ACCESS_KEY', 'sk');
  vi.stubEnv('S3_BUCKET_NAME', 'campfire');
  vi.stubEnv('S3_ENDPOINT', 'https://acct.r2.cloudflarestorage.com');
}

beforeEach(() => vi.resetModules());
afterEach(() => vi.unstubAllEnvs());

describe('dual-read storage resolution (storage.ts)', () => {
  it('resolves the OSN bucket from S3_OSN_*', async () => {
    setOsnEnv();
    const { getBucketNameForBackend } = await import('./storage');
    expect(getBucketNameForBackend('osn')).toBe('campfire-jwst');
  });

  it('throws when OSN creds are incomplete', async () => {
    vi.stubEnv('S3_OSN_ACCESS_KEY_ID', 'oak'); // missing secret / bucket / endpoint
    const { getBucketNameForBackend } = await import('./storage');
    expect(() => getBucketNameForBackend('osn')).toThrow(/osn/i);
  });

  it('caches the OSN client and keeps it distinct from the R2 client', async () => {
    setOsnEnv();
    setR2Env();
    const { getS3ClientForBackend } = await import('./storage');
    const osn1 = getS3ClientForBackend('osn');
    const osn2 = getS3ClientForBackend('osn');
    expect(osn1).toBe(osn2); // cached
    expect(getS3ClientForBackend('r2')).not.toBe(osn1);
  });

  it("'r2' backend resolves the existing data bucket", async () => {
    setR2Env();
    const { getBucketNameForBackend } = await import('./storage');
    expect(getBucketNameForBackend('r2')).toBe('campfire');
  });
});
