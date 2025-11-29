/**
 * JWT verification for download tokens
 * Uses Web Crypto API (built into Cloudflare Workers)
 */

export interface DownloadFile {
  key: string; // R2 object key
  filename: string; // Original filename
}

export interface DownloadPayload {
  files: DownloadFile[];
  exp: number; // Expiration timestamp (milliseconds)
  zipFilename: string; // Name for the ZIP file
}

/**
 * Verify JWT token using HMAC SHA-256
 */
export async function verifyToken(token: string, secret: string): Promise<DownloadPayload> {
  const parts = token.split('.');

  if (parts.length !== 3) {
    throw new Error('Invalid token format');
  }

  const [headerB64, payloadB64, signatureB64] = parts;

  // Verify signature
  const encoder = new TextEncoder();
  const data = encoder.encode(`${headerB64}.${payloadB64}`);
  const secretKey = await crypto.subtle.importKey(
    'raw',
    encoder.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['verify']
  );

  const signature = base64UrlDecode(signatureB64);
  const isValid = await crypto.subtle.verify('HMAC', secretKey, signature, data);

  if (!isValid) {
    throw new Error('Invalid token signature');
  }

  // Decode payload
  const payloadJson = atob(payloadB64.replace(/-/g, '+').replace(/_/g, '/'));
  const payload = JSON.parse(payloadJson);

  return payload as DownloadPayload;
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
