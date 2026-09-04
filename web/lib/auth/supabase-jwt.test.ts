import { describe, it, expect } from 'vitest';
import { SignJWT } from 'jose';
import { verifySupabaseJwt } from './supabase-jwt';

const SECRET = 'super-secret-jwt-token-with-at-least-32-characters-long';
const key = (s: string) => new TextEncoder().encode(s);

async function mint(
  claims: Record<string, unknown>,
  opts: { secret?: string; exp?: string | number; alg?: string } = {}
): Promise<string> {
  let jwt = new SignJWT(claims)
    .setProtectedHeader({ alg: opts.alg ?? 'HS256' })
    .setIssuedAt()
    .setIssuer('https://example.supabase.co/auth/v1');
  jwt = jwt.setExpirationTime(opts.exp ?? '1h');
  return jwt.sign(key(opts.secret ?? SECRET));
}

const userClaims = {
  sub: '11111111-1111-1111-1111-111111111111',
  aud: 'authenticated',
  role: 'authenticated',
  email: 'user@campfire.dev',
  session_id: 'sess-1',
  app_metadata: { provider: 'email' },
  user_metadata: { full_name: 'Test User' },
};

describe('verifySupabaseJwt', () => {
  it('accepts a user session token and returns its claims', async () => {
    const token = await mint(userClaims);
    const res = await verifySupabaseJwt(token, { secret: SECRET });
    expect(res.ok).toBe(true);
    if (!res.ok) return;
    expect(res.claims.sub).toBe(userClaims.sub);
    expect(res.claims.email).toBe('user@campfire.dev');
    expect(res.claims.session_id).toBe('sess-1');
  });

  it('rejects a token signed with a different secret', async () => {
    const token = await mint(userClaims, { secret: 'another-secret-that-is-also-32-characters-long!!' });
    expect(await verifySupabaseJwt(token, { secret: SECRET })).toEqual({ ok: false, reason: 'invalid' });
  });

  it('reports an expired token as expired, not invalid', async () => {
    const token = await mint(userClaims, { exp: Math.floor(Date.now() / 1000) - 600 });
    expect(await verifySupabaseJwt(token, { secret: SECRET })).toEqual({ ok: false, reason: 'expired' });
  });

  it('rejects the anon and service_role keys (same secret, wrong role)', async () => {
    for (const role of ['anon', 'service_role']) {
      const token = await mint({ ...userClaims, role, aud: 'authenticated' });
      expect(await verifySupabaseJwt(token, { secret: SECRET })).toEqual({ ok: false, reason: 'invalid' });
    }
  });

  it('rejects a token with the wrong audience or no subject', async () => {
    const wrongAud = await mint({ ...userClaims, aud: 'campfire-api' });
    expect(await verifySupabaseJwt(wrongAud, { secret: SECRET })).toEqual({ ok: false, reason: 'invalid' });
    const noSub = await mint({ ...userClaims, sub: undefined });
    expect(await verifySupabaseJwt(noSub, { secret: SECRET })).toEqual({ ok: false, reason: 'invalid' });
  });

  it('rejects garbage without throwing', async () => {
    expect(await verifySupabaseJwt('not.a.jwt', { secret: SECRET })).toEqual({ ok: false, reason: 'invalid' });
    expect(await verifySupabaseJwt('', { secret: SECRET })).toEqual({ ok: false, reason: 'invalid' });
  });

  it('asks for the network fallback when no secret is configured', async () => {
    const token = await mint(userClaims);
    expect(await verifySupabaseJwt(token, { secret: '' })).toEqual({ ok: false, reason: 'no-secret' });
  });

  it('asks for the network fallback on a non-HS256 token', async () => {
    const { generateKeyPair } = await import('jose');
    const { privateKey } = await generateKeyPair('ES256');
    const token = await new SignJWT(userClaims)
      .setProtectedHeader({ alg: 'ES256', kid: 'k1' })
      .setExpirationTime('1h')
      .sign(privateKey);
    expect(await verifySupabaseJwt(token, { secret: SECRET })).toEqual({ ok: false, reason: 'unsupported-alg' });
  });
});
