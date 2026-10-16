// GET/POST /api/download under a data release (P6c-2): the archived copy
// of a member, a 404 for a member the release has not archived, and never
// the live key.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('server-only', () => ({}));

let drTag: string | null = null;
vi.mock('@/lib/dr/server', () => ({ requestDrTag: async () => drTag }));

const rows: Record<string, { id: number; target_id: string; fits_path: string }> = {
  'spectra/obs/t1_prism_spec.fits': { id: 1, target_id: 't1', fits_path: 'spectra/obs/t1_prism_spec.fits' },
  'spectra/obs/t2_prism_spec.fits': { id: 2, target_id: 't2', fits_path: 'spectra/obs/t2_prism_spec.fits' },
};
const supabase = {
  from: (table: string) => {
    if (table !== 'cat_spectra') throw new Error(`unexpected table ${table}`);
    let keys: string[] = [];
    const chain = {
      select: () => chain,
      eq: (_c: string, v: string) => { keys = [v]; return chain; },
      in: (_c: string, vs: string[]) => { keys = vs; return chain; },
      single: async () => (rows[keys[0]] ? { data: rows[keys[0]], error: null } : { data: null, error: { message: 'none' } }),
      then: (res: (v: unknown) => unknown) => Promise.resolve({ data: keys.filter((k) => rows[k]).map((k) => rows[k]), error: null }).then(res),
    };
    return chain;
  },
};
vi.mock('@/lib/auth/identity', () => ({
  getRequestIdentity: async () => ({ user: { id: 'u1' }, supabase }),
  getRequestPrincipal: async () => ({ user: { id: 'u1' }, supabase, access: { isLinkAccount: false } }),
}));

const liveSigned = vi.fn(async (key: string) => `https://live.example/${key}`);
vi.mock('@/lib/r2', () => ({
  generateDownloadUrl: (key: string) => liveSigned(key),
  generateDownloadUrls: async (keys: string[]) => Promise.all(keys.map((k) => liveSigned(k))),
}));

let archived: Record<string, string> = {};
vi.mock('@/lib/server/dr-files', () => ({
  resolveReleaseMembers: async (_sb: unknown, _tag: string, keys: string[]) =>
    new Map(keys.map((k) => [k, archived[k] ? { archiveKey: archived[k] } : null])),
  releaseDownloadUrls: async (byKey: Map<string, { archiveKey: string } | null>) =>
    new Map([...byKey].map(([k, m]) => [k, m ? `https://osn.example/${m.archiveKey}?sig=1` : null])),
}));

const tracked: unknown[] = [];
vi.mock('@/lib/actions/download-tracking', () => ({
  trackDownload: async (p: unknown) => { tracked.push(p); },
  extractTargetIdFromFitsPath: async (p: string) => p.split('/').pop()?.split('_')[0] ?? null,
}));

import { GET, POST } from './route';

const A = 'spectra/obs/t1_prism_spec.fits';
const B = 'spectra/obs/t2_prism_spec.fits';
const ARCH_A = `archive/${'a'.repeat(64)}/products/nirspec/obs/t1_prism_spec.fits`;

beforeEach(() => {
  drTag = null;
  archived = {};
  liveSigned.mockClear();
  tracked.length = 0;
});

describe('GET /api/download', () => {
  it('presigns the live key for the live catalog', async () => {
    const res = await GET(new NextRequest(`http://x/api/download?path=${encodeURIComponent(A)}`));
    expect(res.status).toBe(200);
    expect((await res.json()).url).toBe(`https://live.example/${A}`);
  });

  it('hands out the archived copy under a release and never the live key', async () => {
    drTag = 'DR-t';
    archived[A] = ARCH_A;
    const res = await GET(new NextRequest(`http://x/api/download?path=${encodeURIComponent(A)}`));
    expect(res.status).toBe(200);
    expect((await res.json()).url).toBe(`https://osn.example/${ARCH_A}?sig=1`);
    expect(liveSigned).not.toHaveBeenCalled();
  });

  it('answers 404 for a member the release has not archived', async () => {
    drTag = 'DR-t';
    const res = await GET(new NextRequest(`http://x/api/download?path=${encodeURIComponent(A)}`));
    expect(res.status).toBe(404);
    expect(liveSigned).not.toHaveBeenCalled();
    expect(tracked).toHaveLength(0);
  });
});

describe('POST /api/download', () => {
  const post = (paths: string[]) =>
    POST(new NextRequest('http://x/api/download', { method: 'POST', body: JSON.stringify({ paths }), headers: { 'content-type': 'application/json' } }));

  it('returns only the archived members under a release', async () => {
    drTag = 'DR-t';
    archived[A] = ARCH_A;
    const res = await post([A, B]);
    expect(res.status).toBe(200);
    const { urls } = await res.json();
    expect(Object.keys(urls)).toEqual([A]);
    expect(urls[A]).toContain(ARCH_A);
    expect(liveSigned).not.toHaveBeenCalled();
    expect(tracked[0]).toMatchObject({ fileCount: 1 });
  });

  it('is a 404 when the release holds none of the requested members archived', async () => {
    drTag = 'DR-t';
    const res = await post([A, B]);
    expect(res.status).toBe(404);
  });
});
