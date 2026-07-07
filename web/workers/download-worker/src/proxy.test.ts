import { describe, it, expect, vi, afterEach } from 'vitest';
import worker, { isAllowedFetchUrl } from './index';
import { verifyUrlSignature } from './auth';

const ALLOWED = 'uaz1.osn.mghpcc.org,abc123.r2.cloudflarestorage.com';

describe('isAllowedFetchUrl', () => {
  it('allows the exact OSN host over https', () => {
    expect(isAllowedFetchUrl('https://uaz1.osn.mghpcc.org/campfire-jwst/data/x_spec.fits?X-Amz-Signature=a', ALLOWED).ok).toBe(true);
  });

  it('allows a subdomain of an allowlisted host (R2 virtual-hosted style)', () => {
    expect(isAllowedFetchUrl('https://bucket.abc123.r2.cloudflarestorage.com/key?sig=1', ALLOWED).ok).toBe(true);
  });

  it('rejects a non-https scheme', () => {
    expect(isAllowedFetchUrl('http://uaz1.osn.mghpcc.org/x', ALLOWED)).toMatchObject({ ok: false });
  });

  it('rejects embedded credentials in the authority', () => {
    // classic allowlist bypass: user@evil, or the allowed host as userinfo
    expect(isAllowedFetchUrl('https://uaz1.osn.mghpcc.org@evil.com/x', ALLOWED)).toMatchObject({ ok: false });
  });

  it('rejects a host that merely contains an allowlisted host as a substring', () => {
    expect(isAllowedFetchUrl('https://uaz1.osn.mghpcc.org.evil.com/x', ALLOWED)).toMatchObject({ ok: false });
  });

  it('rejects a host not in the allowlist', () => {
    expect(isAllowedFetchUrl('https://example.com/x', ALLOWED)).toMatchObject({ ok: false });
  });

  it('rejects an unparseable url', () => {
    expect(isAllowedFetchUrl('not a url', ALLOWED)).toMatchObject({ ok: false });
  });
});

// Mirror of the server's signUrlSignature (download.ts) — proves sign/verify agree.
async function sign(url: string, secret: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey('raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const sig = await crypto.subtle.sign('HMAC', key, enc.encode(url));
  const b64 = btoa(String.fromCharCode(...new Uint8Array(sig)));
  return b64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}

describe('verifyUrlSignature', () => {
  const SECRET = 'test-shared-secret';
  const URL_ = 'https://uaz1.osn.mghpcc.org/campfire-jwst/data/products/nirspec/obs/x_spec.fits?X-Amz-Signature=deadbeef';

  it('accepts a signature the server would produce', async () => {
    const sig = await sign(URL_, SECRET);
    expect(await verifyUrlSignature(URL_, sig, SECRET)).toBe(true);
  });

  it('rejects a tampered url (key substitution)', async () => {
    const sig = await sign(URL_, SECRET);
    const tampered = URL_.replace('x_spec.fits', 'other_spec.fits');
    expect(await verifyUrlSignature(tampered, sig, SECRET)).toBe(false);
  });

  it('rejects a signature made with a different secret', async () => {
    const sig = await sign(URL_, 'wrong-secret');
    expect(await verifyUrlSignature(URL_, sig, SECRET)).toBe(false);
  });

  it('rejects a malformed signature', async () => {
    expect(await verifyUrlSignature(URL_, 'not-base64!!', SECRET)).toBe(false);
  });
});

describe('proxy handler upstream fetch', () => {
  const SECRET = 'test-shared-secret';
  const ORIGIN = 'https://campfire.hollisakins.com';
  const TARGET =
    'https://uaz1.osn.mghpcc.org/campfire-jwst/data/products/nirspec/obs/x_spec.fits?X-Amz-Signature=deadbeef';

  const ENV = {
    JWT_SECRET: SECRET,
    ALLOWED_ORIGINS: ORIGIN,
    ALLOWED_FETCH_HOSTS: 'uaz1.osn.mghpcc.org',
  };

  async function proxyRequest(): Promise<Request> {
    const sig = await sign(TARGET, SECRET);
    const u = `https://worker.example/proxy?url=${encodeURIComponent(TARGET)}&sig=${sig}`;
    return new Request(u, { headers: { Origin: ORIGIN } });
  }

  afterEach(() => vi.unstubAllGlobals());

  it('streams a 200 upstream and never requests redirect:"error" (Workers has no such mode)', async () => {
    const seen: (RequestInit | undefined)[] = [];
    vi.stubGlobal('fetch', async (_input: unknown, init?: RequestInit) => {
      seen.push(init);
      return new Response('FITSDATA', { status: 200, headers: { 'Content-Type': 'application/octet-stream' } });
    });

    const res = await worker.fetch(await proxyRequest(), ENV);

    expect(res.status).toBe(200);
    // The regression: redirect:'error' throws a TypeError at the edge → 500 on every file.
    expect(seen[0]?.redirect).not.toBe('error');
    expect(seen[0]?.redirect).toBe('manual');
    expect(res.headers.get('Access-Control-Allow-Origin')).toBe(ORIGIN);
  });

  it('refuses an upstream redirect instead of following it (allowlist-escape guard)', async () => {
    vi.stubGlobal('fetch', async () =>
      new Response(null, { status: 302, headers: { Location: 'https://evil.example/x' } }));

    const res = await worker.fetch(await proxyRequest(), ENV);

    expect(res.status).toBe(502);
  });
});
