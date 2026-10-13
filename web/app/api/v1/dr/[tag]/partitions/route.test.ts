// GET /api/v1/dr/<tag>/partitions -- the five principals: no auth, a user
// without the private program, a grantee, an admin on an unpublished release,
// and the link account (refused by authenticateApiRequest, so it never gets a
// principal here).
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('server-only', () => ({}));

type Principal = { userId: string; access: { isAdmin: boolean; accessibleSlugs: string[] } } | null;
let principal: Principal = null;
vi.mock('@/lib/api-auth', () => ({ authenticateApiRequest: async () => principal }));
vi.mock('@/lib/storage', () => ({
  getS3ClientForBackend: () => ({ tag: 'osn' }),
  getBucketNameForBackend: () => 'campfire-jwst',
}));
vi.mock('@aws-sdk/s3-request-presigner', () => ({
  getSignedUrl: async (_c: unknown, cmd: { input: { Key: string } }) => `https://signed/${cmd.input.Key}`,
}));
vi.mock('@/lib/supabase/service', () => ({ createServiceClient: () => ({ marker: 'service' }) }));

let release: Record<string, unknown> | null = null;
let ledger: Record<string, unknown>[] = [];
vi.mock('@/lib/server/dr-exports', async () => {
  const actual = await vi.importActual<typeof import('@/lib/server/dr-exports')>('@/lib/server/dr-exports');
  return {
    ...actual,
    fetchRelease: async () => release,
    fetchLedger: async () => ledger,
  };
});

import { GET } from './route';

function get(tag = 'DR-t') {
  return GET(new NextRequest(`http://x/api/v1/dr/${tag}/partitions`, { headers: { authorization: 'Bearer t' } }), {
    params: Promise.resolve({ tag }),
  });
}

const entry = (stream: string, partition: string, state = 'ready') => ({
  tag: 'DR-t', stream, partition, state, storage_key: `dr/DR-t/exports/${stream}/${partition.replace(/[^A-Za-z0-9._-]/g, '_')}.jsonl.gz`,
  sha256: 'sha256:x', size_bytes: 10, rows: 1, built_at: null, error: null,
});

beforeEach(() => {
  release = {
    tag: 'DR-t', state: 'published', components: ['nirspec', 'nircam/f', 'photometry/f/CAT v1'], dr_format_version: 1,
    counts: { spectra: 2 }, frozen_at: 't', published_at: 't', withdrawn_at: null, doi: null, notes: null, known_issues: null,
  };
  ledger = [
    entry('objects', 'pub'), entry('objects', 'priv'), entry('spectra', 'pub'), entry('spectra', 'priv'),
    entry('members', 'pub'), entry('members', 'nircam/f'), entry('members', 'photometry/f/CAT v1'),
    entry('line_fits', 'priv', 'pending'),
  ];
});

describe('principals', () => {
  it('401 without a principal (link accounts land here too)', async () => {
    principal = null;
    expect((await get()).status).toBe(401);
  });

  it('a user without the private program gets the public and field-level partitions only', async () => {
    principal = { userId: 'u', access: { isAdmin: false, accessibleSlugs: ['pub'] } };
    const res = await get();
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.tag).toBe('DR-t');
    expect(body.format_version).toBe(1);
    expect(body.complete).toBe(false); // one ledger row still pending
    expect(body.partitions.map((p: { stream: string; partition: string }) => `${p.stream}/${p.partition}`)).toEqual([
      'objects/pub', 'spectra/pub', 'members/pub', 'members/nircam/f', 'members/photometry/f/CAT v1',
    ]);
    const pz = body.partitions.find((p: { partition: string }) => p.partition === 'photometry/f/CAT v1');
    expect(pz.url).toBe('https://signed/dr/DR-t/exports/members/photometry_f_CAT_v1.jsonl.gz');
    expect(pz.sha256).toBe('sha256:x');
  });

  it('a component-shaped program slug is not open (Codex on #600)', async () => {
    // a private program named like a component: its program streams, and any
    // component the release did not freeze, stay gated
    ledger.push(entry('spectra', 'nircam/f'), entry('objects', 'nircam/f'), entry('members', 'nircam/g'));
    principal = { userId: 'u', access: { isAdmin: false, accessibleSlugs: ['pub'] } };
    const names = (await (await get()).json()).partitions.map(
      (p: { stream: string; partition: string }) => `${p.stream}/${p.partition}`,
    );
    expect(names).not.toContain('spectra/nircam/f');
    expect(names).not.toContain('objects/nircam/f');
    expect(names).not.toContain('members/nircam/g');
    expect(names).toContain('members/nircam/f');
  });

  it('a grantee gets both programs; pending files are never handed out', async () => {
    principal = { userId: 'g', access: { isAdmin: false, accessibleSlugs: ['pub', 'priv'] } };
    const body = await (await get()).json();
    const names = body.partitions.map((p: { stream: string; partition: string }) => `${p.stream}/${p.partition}`);
    expect(names).toContain('objects/priv');
    expect(names).toContain('spectra/priv');
    expect(names).not.toContain('line_fits/priv');
  });

  it('an unpublished release is 404 to readers and visible to admins', async () => {
    release = { ...release!, state: 'frozen', published_at: null };
    principal = { userId: 'g', access: { isAdmin: false, accessibleSlugs: ['pub', 'priv'] } };
    expect((await get()).status).toBe(404);
    principal = { userId: 'a', access: { isAdmin: true, accessibleSlugs: ['pub', 'priv'] } };
    const res = await get();
    expect(res.status).toBe(200);
    expect((await res.json()).state).toBe('frozen');
  });

  it('unknown tag and bad tag', async () => {
    principal = { userId: 'a', access: { isAdmin: true, accessibleSlugs: [] } };
    release = null;
    expect((await get()).status).toBe(404);
    expect((await get('no spaces')).status).toBe(400);
  });
});
