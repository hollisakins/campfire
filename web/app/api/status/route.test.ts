// GET /api/status: the probe state and the operator message, never cached,
// 200 even while the database is down (the banner reads the body).
import { describe, it, expect, vi } from 'vitest';

vi.mock('@/lib/server/db-status', () => ({
  getDbStatus: async () => ({ state: 'down', reason: 'unreachable', checked_at: 1 }),
  getStatusMessage: async () => 'Planned maintenance at 20:00 UTC.',
}));

import { GET } from './route';

describe('GET /api/status', () => {
  it('returns the state and message', async () => {
    const res = await GET();
    expect(res.status).toBe(200);
    expect(res.headers.get('Cache-Control')).toBe('no-store');
    expect(await res.json()).toEqual({ state: 'down', message: 'Planned maintenance at 20:00 UTC.' });
  });
});
