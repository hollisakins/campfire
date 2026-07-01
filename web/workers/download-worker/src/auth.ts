/**
 * Per-URL HMAC-SHA256 verification for the download proxy.
 *
 * The Worker no longer holds object-store credentials or an R2 binding — it
 * proxies presigned URLs that Vercel minted (dual-read: R2 or OSN) and signed
 * with the shared secret. Verifying the signature over the exact URL means the
 * proxy will only fetch URLs our own server authorized, so it can't be turned
 * into an open relay. Uses the Web Crypto API (built into Cloudflare Workers).
 */

/**
 * Verify that `signature` is HMAC-SHA256(secret, url), base64url-encoded.
 * Constant-time via crypto.subtle.verify.
 */
export async function verifyUrlSignature(
  url: string,
  signature: string,
  secret: string
): Promise<boolean> {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw',
    encoder.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['verify']
  );
  let sigBytes: ArrayBuffer;
  try {
    sigBytes = base64UrlDecode(signature);
  } catch {
    return false;
  }
  return crypto.subtle.verify('HMAC', key, sigBytes, encoder.encode(url));
}

/**
 * Decode base64url string to ArrayBuffer
 */
function base64UrlDecode(str: string): ArrayBuffer {
  // Convert base64url to base64
  const base64 = str.replace(/-/g, '+').replace(/_/g, '/');
  // Pad if necessary
  const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), '=');
  // Decode
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes.buffer;
}
