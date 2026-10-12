// POST /api/v1/dr/<tag>/export -- admin gate, tag / state validation, budget
// clamp, and the run result passed through.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('server-only', () => ({}));
let user: string | null = 'admin-1';
let admin = true;
vi.mock('@/lib/api-auth', () => ({ validateAuth: async () => user }));
vi.mock('@/lib/api-helpers', () => ({ isAdminUser: async () => admin }));
vi.mock('@/lib/storage', () => ({
  getOsnWriteClient: () => ({ tag: 'osn' }),
  getOsnWriteBucket: () => 'campfire-jwst',
}));
vi.mock('@/lib/supabase/service', () => ({ createServiceClient: () => ({ marker: 'service' }) }));

let release: { state: string } | null = { state: 'frozen' };
const run = vi.fn();
vi.mock('@/lib/server/dr-exports', () => ({
  fetchRelease: async () => release,
  runExportBudget: (...a: unknown[]) => run(...a),
}));

import { POST } from './route';

function post(tag: string, body?: unknown) {
  return POST(
    new NextRequest(`http://x/api/v1/dr/${tag}/export`, {
      method: 'POST',
      body: body === undefined ? undefined : JSON.stringify(body),
      headers: { authorization: 'Bearer t' },
    }),
    { params: Promise.resolve({ tag }) },
  );
}

beforeEach(() => {
  user = 'admin-1';
  admin = true;
  release = { state: 'frozen' };
  run.mockReset();
  run.mockResolvedValue({ tag: 'DR-t', state: 'exported', built: [], failed: [], remaining: 0, incomplete: false, duration_ms: 5 });
});

describe('gates', () => {
  it('401 without auth, 403 for non-admins', async () => {
    user = null;
    expect((await post('DR-t')).status).toBe(401);
    user = 'u';
    admin = false;
    expect((await post('DR-t')).status).toBe(403);
    expect(run).not.toHaveBeenCalled();
  });

  it('400 on a bad tag, 404 on an unknown release, 409 on a published one', async () => {
    expect((await post('bad tag')).status).toBe(400);
    release = null;
    expect((await post('DR-t')).status).toBe(404);
    release = { state: 'published' };
    expect((await post('DR-t')).status).toBe(409);
    expect(run).not.toHaveBeenCalled();
  });
});

describe('run', () => {
  it('runs with the default budget and returns the result', async () => {
    const res = await post('DR-t');
    expect(res.status).toBe(200);
    expect((await res.json()).state).toBe('exported');
    expect(run).toHaveBeenCalledTimes(1);
    expect(run.mock.calls[0][3]).toBe('DR-t');
    expect(run.mock.calls[0][4]).toBe(240_000);
    expect(run.mock.calls[0][2]).toBe('campfire-jwst');
  });

  it('clamps the budget to the function ceiling', async () => {
    await post('DR-t', { budget_seconds: 9999 });
    expect(run.mock.calls[0][4]).toBe(280_000);
    await post('DR-t', { budget_seconds: 0 });
    expect(run.mock.calls[1][4]).toBe(1_000);
  });

  it('500 with the message when the run throws', async () => {
    run.mockRejectedValue(new Error('begin failed'));
    const res = await post('DR-t');
    expect(res.status).toBe(500);
    expect((await res.json()).error_description).toBe('begin failed');
  });
});
