// HMAC signing shared with the Cloudflare Worker (workers/download-worker):
// the `/proxy` url signature (bulk ZIP, #255) and the `/o/` object token
// (delivery front, perf T2-D1 #507) are both HMAC-SHA256 over a message with
// the shared WORKER_JWT_SECRET, base64url-encoded. The Worker's auth.ts is the
// verifying mirror of this file.
import 'server-only';

export async function hmacBase64Url(message: string, secret: string): Promise<string> {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw',
    encoder.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const signature = await crypto.subtle.sign('HMAC', key, encoder.encode(message));
  return base64UrlEncode(signature);
}

export function base64UrlEncode(data: ArrayBuffer): string {
  return Buffer.from(data).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}
