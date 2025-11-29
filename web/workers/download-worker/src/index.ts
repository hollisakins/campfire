/**
 * Cloudflare Worker for CAMPFIRE FITS file downloads
 * Streams multiple FITS files from R2 as a ZIP archive
 */

import { verifyToken, type DownloadPayload } from './auth';
import { streamZip } from './zip';

export interface Env {
  R2_BUCKET: R2Bucket;
  JWT_SECRET: string;
  ALLOWED_ORIGINS: string;
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    // Handle CORS preflight
    if (request.method === 'OPTIONS') {
      return handleCORS(request, env);
    }

    // Only allow GET requests
    if (request.method !== 'GET') {
      return new Response('Method not allowed', { status: 405 });
    }

    try {
      // Extract and verify token
      const token = url.searchParams.get('token');

      if (!token) {
        return new Response('Missing token parameter', { status: 400 });
      }

      // Verify JWT and extract payload
      const payload = await verifyToken(token, env.JWT_SECRET);

      // Check expiration
      if (payload.exp && payload.exp < Date.now()) {
        return new Response('Token expired', { status: 401 });
      }

      // Validate payload
      if (!payload.files || !Array.isArray(payload.files) || payload.files.length === 0) {
        return new Response('Invalid token payload', { status: 400 });
      }

      // Get zip filename from payload (includes timestamp)
      const zipFilename = payload.zipFilename || 'campfire_download.zip';

      // Stream ZIP response
      const { readable, writable } = new TransformStream();

      // Start ZIP generation in background
      ctx.waitUntil(
        streamZip(payload.files, writable, env.R2_BUCKET).catch((err) => {
          console.error('ZIP generation error:', err);
        })
      );

      // Return streaming response
      return new Response(readable, {
        headers: {
          'Content-Type': 'application/zip',
          'Content-Disposition': `attachment; filename="${zipFilename}"`,
          'Access-Control-Allow-Origin': getAllowedOrigin(request, env),
          'Access-Control-Allow-Methods': 'GET, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type',
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

/**
 * Handle CORS preflight requests
 */
function handleCORS(request: Request, env: Env): Response {
  return new Response(null, {
    status: 204,
    headers: {
      'Access-Control-Allow-Origin': getAllowedOrigin(request, env),
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      'Access-Control-Max-Age': '86400',
    },
  });
}

/**
 * Get allowed origin for CORS
 */
function getAllowedOrigin(request: Request, env: Env): string {
  const origin = request.headers.get('Origin');
  const allowedOrigins = env.ALLOWED_ORIGINS.split(',');

  if (origin && allowedOrigins.includes(origin)) {
    return origin;
  }

  // Default to first allowed origin
  return allowedOrigins[0] || '*';
}
