// The release gate on identity (P6c-1): under a `?dr=` request, a viewer the
// pages flag does not admit gets the 404, never live data.
import { beforeEach, describe, expect, it, vi } from 'vitest';

let drTag: string | null = null;
type Mode = 'off' | 'admin' | 'all';
let mode = 'admin' as Mode;
let session: { access_token: string } | null = null;
let access = { isAdmin: false, isLinkAccount: false };

const notFound = vi.fn(() => { throw new Error('NEXT_NOT_FOUND'); });
vi.mock('next/navigation', () => ({ notFound: () => notFound(), unstable_rethrow: () => {} }));
vi.mock('next/headers', () => ({ cookies: async () => ({ getAll: () => [], set: () => {} }) }));
vi.mock('@/lib/dr/server', () => ({ requestDrTag: async () => drTag }));
vi.mock('@/lib/dr-pages', async (orig) => ({ ...(await orig<typeof import('@/lib/dr-pages')>()), drPagesMode: () => mode }));
vi.mock('@/lib/supabase/server', () => ({
  createClient: async () => ({
    auth: {
      getSession: async () => ({ data: { session } }),
      getUser: async () => ({ data: { user: null } }),
    },
  }),
}));
vi.mock('./supabase-jwt', () => ({
  verifySupabaseJwt: async () => ({ ok: true, claims: { sub: 'user-1', email: 'u@x', app_metadata: {}, user_metadata: {} } }),
}));
vi.mock('./access-context', () => ({ getAccessContext: async () => access }));

import { getRequestIdentity } from './identity';

const cases: [Mode, 'anon' | 'user' | 'admin' | 'link', 'show' | 404][] = [
  ['off', 'anon', 'show'], ['off', 'user', 'show'], ['off', 'admin', 'show'], ['off', 'link', 'show'],
  ['admin', 'anon', 404], ['admin', 'user', 404], ['admin', 'admin', 'show'], ['admin', 'link', 404],
  ['all', 'anon', 404], ['all', 'user', 'show'], ['all', 'admin', 'show'], ['all', 'link', 404],
];

describe('drGate matrix (flag × viewer) under a release', () => {
  beforeEach(() => { notFound.mockClear(); });
  for (const [m, who, want] of cases) {
    it(`flag=${m} ${who} → ${want}`, async () => {
      // With the flag off middleware never stamps the header; the gate sees no tag.
      drTag = m === 'off' ? null : 'DR-seed';
      mode = m;
      session = who === 'anon' ? null : { access_token: 't' };
      access = { isAdmin: who === 'admin', isLinkAccount: who === 'link' };
      if (want === 'show') {
        const id = await getRequestIdentity();
        expect(id.user === null).toBe(who === 'anon');
      } else {
        await expect(getRequestIdentity()).rejects.toThrow('NEXT_NOT_FOUND');
      }
      vi.resetModules();
    });
  }
});
