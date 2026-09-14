// Download tokens (lib/auth/tokens.ts) and the one place that accepts them
// (api-auth.ts authenticateStorageDownloadRequest). The property that matters:
// a download token opens GET /api/v1/storage/download for its user and
// nothing else — it is never an access token, an access token is never a
// download token, and it expires.
//
// Share links are the one principal shape that reaches /api/v1 at all, and
// only here: a live link that permits downloads resolves to a principal from
// its download token (so a shared field can be bulk-downloaded), while the
// same link's access token still opens nothing, and a revoked / expired /
// downloads-off link resolves to no credential at all.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { NextRequest } from 'next/server';

vi.hoisted(() => {
  process.env.JWT_SECRET = 'test-jwt-secret-with-at-least-32-bytes!!';
});

vi.mock('server-only', () => ({}));
// tokens.ts and api-auth.ts import the service client at module load; no
// test here reaches the database (an sk_ key would).
vi.mock('@/lib/supabase/service', () => ({
  createServiceClient: () => ({ rpc: vi.fn(async () => ({ data: null, error: { message: 'no db in tests' } })) }),
}));

let isLinkAccount = false;
let linkScope: Record<string, unknown> | null = null;
vi.mock('@/lib/auth/access-context', async () => {
  // linkMayDownload is the real predicate: these tests are about which link
  // shapes it lets through, so mocking it would test nothing.
  const actual = await vi.importActual<typeof import('@/lib/auth/access-context')>(
    '@/lib/auth/access-context',
  );
  return {
    linkMayDownload: actual.linkMayDownload,
    getAccessContext: async (userId: string) => ({
      isAdmin: false,
      isLinkAccount,
      linkScope,
      accessibleSlugs: [`programs-of-${userId}`],
    }),
  };
});

/** A live share link scoped to one NIRCam field. */
function liveLink(over: Record<string, unknown> = {}) {
  return {
    active: true, observation: null, field: 'cosmos',
    allowDownload: true, includeDrafts: false, expiresAt: null,
    ...over,
  };
}

import {
  generateAccessToken,
  generateDownloadToken,
  validateAccessToken,
  validateDownloadToken,
} from './tokens';
import { authenticateApiRequest, authenticateStorageDownloadRequest } from '@/lib/api-auth';

function request(bearer: string | null) {
  return new NextRequest('http://localhost/api/v1/storage/download?key=x', {
    headers: bearer ? { authorization: `Bearer ${bearer}` } : {},
  });
}

beforeEach(() => {
  isLinkAccount = false;
  linkScope = null;
  vi.useRealTimers();
});

describe('download tokens', () => {
  it('round-trips the user id and expires in 30 days', async () => {
    const before = Date.now();
    const { token, expiresAt } = await generateDownloadToken('user-42');
    expect(await validateDownloadToken(token)).toBe('user-42');
    const days = (expiresAt.getTime() - before) / 86400e3;
    expect(days).toBeGreaterThan(29.99);
    expect(days).toBeLessThan(30.01);
  });

  it('is never an access token, and an access token is never a download token', async () => {
    const dl = (await generateDownloadToken('user-42')).token;
    const access = (await generateAccessToken('user-42')).token;
    expect(await validateAccessToken(dl)).toBeNull();
    expect(await validateDownloadToken(access)).toBeNull();
    expect(await validateDownloadToken('not-a-jwt')).toBeNull();
  });

  it('is rejected once expired', async () => {
    const { token } = await generateDownloadToken('user-42');
    vi.useFakeTimers();
    vi.setSystemTime(Date.now() + 31 * 86400e3);
    expect(await validateDownloadToken(token)).toBeNull();
  });
});

describe('authenticateStorageDownloadRequest', () => {
  it('accepts a download token as a principal with the user access context', async () => {
    const { token } = await generateDownloadToken('user-42');
    const principal = await authenticateStorageDownloadRequest(request(token));
    expect(principal).toMatchObject({
      userId: 'user-42',
      method: 'download_token',
      access: { accessibleSlugs: ['programs-of-user-42'] },
    });
  });

  it('still accepts an access token, as every /api/v1 route does', async () => {
    const { token } = await generateAccessToken('user-7');
    const principal = await authenticateStorageDownloadRequest(request(token));
    expect(principal).toMatchObject({ userId: 'user-7', method: 'access_token' });
  });

  it('the general /api/v1 authenticator does NOT accept a download token', async () => {
    const { token } = await generateDownloadToken('user-42');
    expect(await authenticateApiRequest(request(token))).toBeNull();
  });

  it('refuses a missing bearer and garbage', async () => {
    expect(await authenticateStorageDownloadRequest(request(null))).toBeNull();
    expect(await authenticateStorageDownloadRequest(request('garbage'))).toBeNull();
  });
});

describe('share links', () => {
  it('a live link that permits downloads gets a principal from its download token', async () => {
    const { token } = await generateDownloadToken('link-user');
    isLinkAccount = true;
    linkScope = liveLink();
    expect(await authenticateStorageDownloadRequest(request(token))).toMatchObject({
      userId: 'link-user',
      method: 'download_token',
      access: { isLinkAccount: true, linkScope: { field: 'cosmos' } },
    });
  });

  it('but its access token still opens nothing — including this route', async () => {
    const { token } = await generateAccessToken('link-user');
    isLinkAccount = true;
    linkScope = liveLink();
    expect(await authenticateApiRequest(request(token))).toBeNull();
    expect(await authenticateStorageDownloadRequest(request(token))).toBeNull();
  });

  it('and a download token minted for a link whose downloads are off, revoked, expired or unscoped is refused', async () => {
    const { token } = await generateDownloadToken('link-user');
    isLinkAccount = true;
    for (const scope of [
      liveLink({ allowDownload: false }),
      liveLink({ active: false }),          // revoked or past expires_at
      liveLink({ field: null }),            // no scope on either axis
      null,                                 // unreadable profile (fail-closed)
    ]) {
      linkScope = scope;
      expect(await authenticateStorageDownloadRequest(request(token))).toBeNull();
    }
  });

  it('caps the token lifetime at the link expiry, and never extends it', async () => {
    const short = new Date(Date.now() + 2 * 86400e3);
    expect((await generateDownloadToken('link-user', { notAfter: short })).expiresAt).toEqual(short);

    const far = new Date(Date.now() + 365 * 86400e3);
    const days = ((await generateDownloadToken('link-user', { notAfter: far })).expiresAt.getTime() - Date.now()) / 86400e3;
    expect(days).toBeGreaterThan(29.99);
    expect(days).toBeLessThan(30.01);
  });
});
