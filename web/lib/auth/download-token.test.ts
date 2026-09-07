// Download tokens (lib/auth/tokens.ts) and the one place that accepts them
// (api-auth.ts authenticateStorageDownloadRequest). The property that matters:
// a download token opens GET /api/v1/storage/download for its user and
// nothing else — it is never an access token, an access token is never a
// download token, link accounts get no principal, and it expires.
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
vi.mock('@/lib/auth/access-context', () => ({
  getAccessContext: async (userId: string) => ({
    isAdmin: false,
    isLinkAccount,
    linkScope: null,
    accessibleSlugs: [`programs-of-${userId}`],
  }),
}));

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

  it('refuses link accounts, a missing bearer, and garbage', async () => {
    const { token } = await generateDownloadToken('link-user');
    isLinkAccount = true;
    expect(await authenticateStorageDownloadRequest(request(token))).toBeNull();
    isLinkAccount = false;
    expect(await authenticateStorageDownloadRequest(request(null))).toBeNull();
    expect(await authenticateStorageDownloadRequest(request('garbage'))).toBeNull();
  });
});
