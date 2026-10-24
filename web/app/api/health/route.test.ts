// GET /api/health: 200 while the database answers, 503 (still JSON) when the
// probe query errors or throws, never cached.
import { describe, it, expect, beforeEach, vi } from 'vitest';

let mode: 'ok' | 'error' | 'throw' = 'ok';
vi.mock('@/lib/supabase/service', () => ({
  createServiceClient: () => {
    if (mode === 'throw') throw new Error('SUPABASE_SERVICE_ROLE_KEY is not configured');
    return {
      from: () => ({
        select: () => ({
          limit: () => ({
            abortSignal: async () =>
              mode === 'error' ? { data: null, error: { message: 'down' } } : { data: [], error: null },
          }),
        }),
      }),
    };
  },
}));

import { GET } from './route';

describe('GET /api/health', () => {
  beforeEach(() => {
    mode = 'ok';
    process.env.GIT_SHA = 'abc1234';
  });

  it('reports ok with the deployed sha', async () => {
    const res = await GET();
    expect(res.status).toBe(200);
    expect(res.headers.get('Cache-Control')).toBe('no-store');
    const body = await res.json();
    expect(body).toMatchObject({ ok: true, db: 'ok', sha: 'abc1234' });
    expect(typeof body.uptime_s).toBe('number');
  });

  it('answers 503 when the probe query errors', async () => {
    mode = 'error';
    const res = await GET();
    expect(res.status).toBe(503);
    expect(await res.json()).toMatchObject({ ok: false, db: 'error' });
  });

  it('answers 503 when the client cannot be built', async () => {
    mode = 'throw';
    const res = await GET();
    expect(res.status).toBe(503);
    expect(await res.json()).toMatchObject({ ok: false, db: 'error' });
  });
});
