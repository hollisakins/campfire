// GET /api/health: 200 while the shared database probe is ok, 503 (still JSON)
// when the database is stuck or unreachable, never cached.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import type { DbStatus } from '@/lib/server/db-status';

let status: DbStatus = { state: 'ok', reason: null, checked_at: 1 };
vi.mock('@/lib/server/db-status', () => ({ getDbStatus: async () => status }));

import { GET } from './route';

describe('GET /api/health', () => {
  beforeEach(() => {
    status = { state: 'ok', reason: null, checked_at: 1 };
    process.env.GIT_SHA = 'abc1234';
  });

  it('reports ok with the deployed sha', async () => {
    const res = await GET();
    expect(res.status).toBe(200);
    expect(res.headers.get('Cache-Control')).toBe('no-store');
    const body = await res.json();
    expect(body).toMatchObject({ ok: true, db: 'ok', reason: null, sha: 'abc1234' });
    expect(typeof body.uptime_s).toBe('number');
  });

  it('answers 503 while commits are stuck on the disk', async () => {
    status = { state: 'degraded', reason: 'wal', checked_at: 1 };
    const res = await GET();
    expect(res.status).toBe(503);
    expect(await res.json()).toMatchObject({ ok: false, db: 'degraded', reason: 'wal' });
  });

  it('answers 503 when the database is unreachable', async () => {
    status = { state: 'down', reason: 'unreachable', checked_at: 1 };
    const res = await GET();
    expect(res.status).toBe(503);
    expect(await res.json()).toMatchObject({ ok: false, db: 'error', reason: 'unreachable' });
  });
});
