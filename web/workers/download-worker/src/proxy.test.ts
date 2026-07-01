import { describe, it, expect } from 'vitest';
import { isAllowedFetchUrl } from './index';
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
