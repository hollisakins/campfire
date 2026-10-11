// POST /api/v1/deploy/presign — the retain-on-supersede gate (P2) around the
// PUT presign: `log` mode never blocks, `enforce` answers 409 when a retained
// copy cannot be made, `off` skips the registry lookup; the writable-key
// allowlist refuses archive keys.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('server-only', () => ({}));
vi.mock('@/lib/api-auth', () => ({ validateAuth: async () => 'admin-1' }));
vi.mock('@/lib/api-helpers', () => ({ isAdminUser: async () => true }));
vi.mock('@/lib/storage', () => ({
  getS3Client: () => ({ tag: 'r2' }),
  getBucketName: () => 'r2-bucket',
  getOsnWriteClient: () => ({ tag: 'osn' }),
  getOsnWriteBucket: () => 'campfire-jwst',
}));
vi.mock('@aws-sdk/s3-request-presigner', () => ({
  getSignedUrl: async (_c: unknown, cmd: { input: { Key: string } }) => `https://signed/${cmd.input.Key}`,
}));
vi.mock('@/lib/supabase/service', () => ({ createServiceClient: () => ({ marker: 'service' }) }));

const planRetain = vi.fn();
const executeRetains = vi.fn();
let mode = 'log';
vi.mock('@/lib/server/retain', async () => {
  const actual = await vi.importActual<typeof import('@/lib/server/retain')>('@/lib/server/retain');
  return {
    ...actual,
    retainMode: () => mode,
    planRetain: (...a: unknown[]) => planRetain(...a),
    executeRetains: (...a: unknown[]) => executeRetains(...a),
  };
});

import { POST } from './route';

const SPEC = 'data/products/nirspec/ember_egs_p1/ember_egs_p1_prism_clear_15182_spec.fits';
const ARCHIVE = `archive/${'a'.repeat(64)}/products/nirspec/ember_egs_p1/ember_egs_p1_prism_clear_15182_spec.fits`;

function post(body: unknown) {
  return POST(new NextRequest('http://x/api/v1/deploy/presign', { method: 'POST', body: JSON.stringify(body), headers: { authorization: 'Bearer t' } }));
}

beforeEach(() => {
  planRetain.mockReset();
  executeRetains.mockReset();
  mode = 'log';
});

describe('allowlist', () => {
  it('refuses a PUT under archive/ (writable keys only)', async () => {
    const res = await post({ bucket: 'data', backend: 'osn', uploads: [{ key: ARCHIVE }] });
    expect(res.status).toBe(400);
    expect(planRetain).not.toHaveBeenCalled();
  });
});

describe('retain gate', () => {
  it('log mode: plans, logs, signs everything', async () => {
    planRetain.mockResolvedValue([{ key: SPEC, action: 'retain', row: { storage_key: SPEC, content_hash: 'sha256:x' }, archiveKey: ARCHIVE }]);
    const res = await post({ bucket: 'data', backend: 'osn', uploads: [{ key: SPEC, content_type: 'application/fits' }] });
    expect(res.status).toBe(200);
    expect((await res.json()).urls[SPEC]).toBe(`https://signed/${SPEC}`);
    expect(planRetain).toHaveBeenCalledTimes(1);
    expect(executeRetains).not.toHaveBeenCalled();
  });

  it('enforce mode: archives, then signs', async () => {
    mode = 'enforce';
    planRetain.mockResolvedValue([{ key: SPEC, action: 'retain', row: { storage_key: SPEC }, archiveKey: ARCHIVE }]);
    executeRetains.mockResolvedValue([{ key: SPEC, status: 'archived', archiveKey: ARCHIVE, copied: true }]);
    const res = await post({ bucket: 'data', backend: 'osn', uploads: [{ key: SPEC }] });
    expect(res.status).toBe(200);
    expect(executeRetains).toHaveBeenCalledTimes(1);
    const deps = executeRetains.mock.calls[0][1] as { bucket: string; actorId: string };
    expect(deps.bucket).toBe('campfire-jwst');
    expect(deps.actorId).toBe('admin-1');
  });

  it('enforce mode: 409 with the failed keys when a copy drifts, nothing signed', async () => {
    mode = 'enforce';
    planRetain.mockResolvedValue([
      { key: SPEC, action: 'retain', row: { storage_key: SPEC }, archiveKey: ARCHIVE },
      { key: 'data/products/nirspec/ember_egs_p1/ember_egs_p1_prism_clear_15182_zfit.json', action: 'drift', reason: 'registry moved', row: {} },
    ]);
    executeRetains.mockResolvedValue([{ key: SPEC, status: 'drift', reason: 'copy precondition failed' }]);
    const res = await post({ bucket: 'data', backend: 'osn', uploads: [{ key: SPEC }, { key: 'data/products/nirspec/ember_egs_p1/ember_egs_p1_prism_clear_15182_zfit.json' }] });
    expect(res.status).toBe(409);
    const body = await res.json();
    expect(body.error).toBe('retain_failed');
    expect(body.failed).toHaveLength(2);
    expect(body.urls).toBeUndefined();
  });

  it('off mode and legacy R2 backend skip the lookup', async () => {
    mode = 'off';
    let res = await post({ bucket: 'data', backend: 'osn', uploads: [{ key: SPEC }] });
    expect(res.status).toBe(200);
    mode = 'log';
    res = await post({ bucket: 'data', backend: 'r2', uploads: [{ key: 'spectra/ember_egs_p1/ember_egs_p1_prism_clear_15182_spec.fits' }] });
    expect(res.status).toBe(200);
    expect(planRetain).not.toHaveBeenCalled();
  });

  it('passes the client-planned hashes through to the plan', async () => {
    planRetain.mockResolvedValue([{ key: SPEC, action: 'skip-unchanged', row: {} }]);
    await post({ bucket: 'data', backend: 'osn', uploads: [{ key: SPEC, expected_old_hash: 'sha256:old', new_hash: 'sha256:new' }] });
    expect(planRetain.mock.calls[0][0]).toEqual([{ key: SPEC, expectedOldHash: 'sha256:old', newHash: 'sha256:new' }]);
  });
});
