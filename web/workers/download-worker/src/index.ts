/**
 * Cloudflare Worker for CAMPFIRE FITS file downloads
 * Authenticated file proxy — serves individual files from R2
 */

import { verifyToken } from './auth';

export interface Env {
  R2_BUCKET: R2Bucket;
  JWT_SECRET: string;
  ALLOWED_ORIGINS: string;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    // Handle CORS preflight
    if (request.method === 'OPTIONS') {
      return handleCORS(request, env);
    }

    // Only GET /file is supported
    if (request.method !== 'GET' || url.pathname !== '/file') {
      return new Response('Not found', { status: 404 });
    }

    try {
      // Extract parameters
      const key = url.searchParams.get('key');
      if (!key) {
        return new Response('Missing key parameter', { status: 400 });
      }

      const authHeader = request.headers.get('Authorization');
      const token = authHeader?.startsWith('Bearer ') ? authHeader.slice(7) : null;
      if (!token) {
        return new Response('Missing Authorization header', { status: 401 });
      }

      // Verify JWT
      const payload = await verifyToken(token, env.JWT_SECRET);

      if (payload.exp && payload.exp < Date.now()) {
        return new Response('Token expired', { status: 401 });
      }

      // Check that requested key is in the token's allowlist
      const allowed = payload.files?.some((f) => f.key === key);
      if (!allowed) {
        return new Response('File not authorized', { status: 403 });
      }

      // Fetch from R2
      const object = await env.R2_BUCKET.get(key);
      if (!object) {
        return new Response('File not found', { status: 404 });
      }

      return new Response(object.body, {
        headers: {
          'Content-Type': 'application/octet-stream',
          'Content-Length': object.size.toString(),
          'Access-Control-Allow-Origin': getAllowedOrigin(request, env),
          'Access-Control-Allow-Methods': 'GET, OPTIONS',
          'Access-Control-Allow-Headers': 'Authorization',
        },
      });
    } catch (error) {
      console.error('Worker error:', error);

      if (error instanceof Error) {
        if (error.message.includes('Invalid token') || error.message.includes('verification')) {
          return new Response('Invalid or expired token', { status: 401 });
        }
      }

      return new Response('Internal server error', { status: 500 });
    }
  },
};

function handleCORS(request: Request, env: Env): Response {
  return new Response(null, {
    status: 204,
    headers: {
      'Access-Control-Allow-Origin': getAllowedOrigin(request, env),
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': 'Authorization',
      'Access-Control-Max-Age': '86400',
    },
  });
}

function getAllowedOrigin(request: Request, env: Env): string {
  const origin = request.headers.get('Origin');
  const allowedOrigins = env.ALLOWED_ORIGINS.split(',');

  if (origin && allowedOrigins.includes(origin)) {
    return origin;
  }

  return allowedOrigins[0] || '*';
}
