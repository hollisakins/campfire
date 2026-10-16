// GET /api/v1/observations/<obs>/manifest under a data release (P6c-2): the
// frozen spectrum list with every url the member's ARCHIVED copy, resolved
// with the caller's program scope; a pending member keeps its row with a
// null url; never a live key.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('server-only', () => ({}));

const USER = 'user-uuid';
vi.mock('@/lib/api-auth', () => ({
  authenticateApiRequest: async () => ({ userId: USER, method: 'api_key', access: { isAdmin: false, isLinkAccount: false, accessibleSlugs: ['pub'] } }),
}));
let gateTag: string | null = null;
vi.mock('@/lib/server/bearer-release', () => ({ assertBearerReleaseAccess: async () => ({ tag: gateTag }) }));
vi.mock('@/lib/api-helpers', () => ({ getAccessiblePrograms: async () => ['pub'], isAdminUser: async () => false }));

const A = 'spectra/obs1/t1_prism_spec.fits';
const B = 'spectra/obs1/t2_prism_spec.fits';
const manifestRows = [
  { spectra_id: 1, spectrum_id: 't1_prism', target_id: 't1', grating: 'PRISM', fits_path: A, file_hash: 'h1', file_size: 10, signal_to_noise: 5, cfpipe_version: '1' },
  { spectra_id: 2, spectrum_id: 't2_prism', target_id: 't2', grating: 'PRISM', fits_path: B, file_hash: 'h2', file_size: 20, signal_to_noise: 6, cfpipe_version: '1' },
];
const tables: string[] = [];
let clientDr: string | null | undefined;
vi.mock('@/lib/supabase/service', () => ({
  createServiceClient: (opts: { dr?: string | null } = {}) => {
    clientDr = opts.dr;
    return {
      rpc: async () => ({ data: manifestRows, error: null }),
      from: (t: string) => {
        tables.push(t);
        const chain = {
          select: () => chain, eq: () => chain, in: () => chain, limit: () => chain, insert: () => chain,
          maybeSingle: async () => ({ data: t === 'cat_targets' ? { program_slug: 'pub', field: 'f' } : { program_name: 'Pub' }, error: null }),
          then: (res: (v: unknown) => unknown) => Promise.resolve({ data: null, error: null }).then(res),
        };
        return chain;
      },
    };
  },
}));

const liveSigned = vi.fn(async (keys: string[]) => keys.map((k) => `https://live.example/${k}`));
vi.mock('@/lib/r2', () => ({ generateDownloadUrls: (keys: string[]) => liveSigned(keys) }));

let scopeSeen: unknown = null;
const ARCH_A = `archive/${'a'.repeat(64)}/products/nirspec/obs1/t1_prism_spec.fits`;
vi.mock('@/lib/server/dr-files', () => ({
  resolveReleaseMembersAs: async (_sb: unknown, _tag: string, keys: string[], scope: unknown) => {
    scopeSeen = scope;
    return new Map(keys.map((k) => [k, k === A ? { archiveKey: ARCH_A } : null]));
  },
  releaseDownloadUrls: async (byKey: Map<string, { archiveKey: string } | null>) =>
    new Map([...byKey].map(([k, m]) => [k, m ? `https://osn.example/${m.archiveKey}?sig=1` : null])),
}));

import { GET } from './route';

function get(qs = '') {
  return GET(new NextRequest(`http://x/api/v1/observations/obs1/manifest${qs}`, { headers: { authorization: 'Bearer sk_t' } }),
    { params: Promise.resolve({ obs_name: 'obs1' }) });
}

beforeEach(() => {
  gateTag = null;
  clientDr = undefined;
  scopeSeen = null;
  tables.length = 0;
  liveSigned.mockClear();
});

describe('GET /api/v1/observations/<obs>/manifest', () => {
  it('presigns live keys for the live catalog', async () => {
    const res = await get();
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.spectra.map((s: { download_url: string }) => s.download_url)).toEqual([`https://live.example/${A}`, `https://live.example/${B}`]);
    expect(body.dr).toBeUndefined();
    expect(clientDr).toBeNull();
  });

  it('under a release hands out archived copies with the caller scope, null for a pending member, never a live key', async () => {
    gateTag = 'DR-t';
    const res = await get('?dr=DR-t');
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.dr).toBe('DR-t');
    expect(clientDr).toBe('DR-t');
    expect(scopeSeen).toEqual({ accessible: ['pub'], isAdmin: false });
    expect(body.spectra).toHaveLength(2);
    expect(body.spectra[0].download_url).toBe(`https://osn.example/${ARCH_A}?sig=1`);
    expect(body.spectra[1].download_url).toBeNull();
    expect(liveSigned).not.toHaveBeenCalled();
    expect(JSON.stringify(body)).not.toContain('live.example');
    expect(tables).toContain('cat_targets');
    expect(tables).not.toContain('targets');
  });
});
