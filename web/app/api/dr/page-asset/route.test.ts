// /api/dr/page-asset (P6c-2): a figure of the release page document,
// authorized through the viewer's manifest rows, served as a redirect to
// the archived object; anything else is a 404.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('server-only', () => ({}));
vi.mock('@/lib/dr-pages', () => ({ drPagesMode: () => 'all', drPagesAccess: (_m: string, v: { signedIn: boolean }) => (v.signedIn ? 'show' : 'not-found') }));

let signedIn = true;
vi.mock('@/lib/auth/identity', () => ({
  getRequestPrincipal: async () => (signedIn ? { user: { id: 'u1' }, supabase: {}, access: { isAdmin: false, isLinkAccount: false } } : null),
}));

let members: { storageKey: string; archiveKey: string; backend: 'osn'; bucket: string }[] = [];
vi.mock('@/lib/server/dr-files', () => ({
  listReleaseMembers: async (_sb: unknown, _tag: string, opts: { component: string }) => (opts.component === 'page' ? members : []),
}));
vi.mock('@/lib/storage', () => ({ getS3ClientForBackend: () => ({}), getBucketNameForBackend: () => 'campfire-data' }));
vi.mock('@aws-sdk/s3-request-presigner', () => ({
  getSignedUrl: async (_c: unknown, cmd: { input: { Key: string } }) => `https://osn.example/campfire-data/${cmd.input.Key}?sig=1`,
}));
vi.mock('@aws-sdk/client-s3', () => ({ GetObjectCommand: class { input: unknown; constructor(input: unknown) { this.input = input; } } }));

import { GET } from './route';

const KEY = 'dr/DR-t/page/0123456789abcdef/nz.png';

beforeEach(() => {
  signedIn = true;
  members = [{ storageKey: KEY, archiveKey: KEY, backend: 'osn', bucket: 'data' }];
});

describe('GET /api/dr/page-asset', () => {
  it('redirects to the archived object of a listed asset', async () => {
    const res = await GET(new NextRequest('http://x/api/dr/page-asset?tag=DR-t&name=nz.png'));
    expect(res.status).toBe(302);
    expect(res.headers.get('location')).toBe(`https://osn.example/campfire-data/${KEY}?sig=1`);
    expect(res.headers.get('cache-control')).toContain('private');
  });
  it('is a 404 for a name the manifest does not list, a bad name, a bad tag, or no session', async () => {
    expect((await GET(new NextRequest('http://x/api/dr/page-asset?tag=DR-t&name=other.png'))).status).toBe(404);
    expect((await GET(new NextRequest('http://x/api/dr/page-asset?tag=DR-t&name=..%2Fnz.png'))).status).toBe(404);
    expect((await GET(new NextRequest('http://x/api/dr/page-asset?tag=..&name=nz.png'))).status).toBe(404);
    signedIn = false;
    expect((await GET(new NextRequest('http://x/api/dr/page-asset?tag=DR-t&name=nz.png'))).status).toBe(404);
  });
});
