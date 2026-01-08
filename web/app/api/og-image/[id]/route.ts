import { NextRequest } from 'next/server';
import { r2Client, generateRGBImagePath } from '@/lib/r2';
import { GetObjectCommand } from '@aws-sdk/client-s3';

const R2_BUCKET_NAME = process.env.R2_BUCKET_NAME || 'campfire-fits';

/**
 * GET /api/og-image/[id]
 *
 * Serves RGB images publicly for social media crawlers (Slack, Twitter, etc.)
 * - No authentication required (unlike /api/rgb-thumbnail)
 * - Returns image bytes directly (not a redirect)
 * - Aggressive caching (1 week) since images rarely change
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const objectId = decodeURIComponent(id);

  try {
    const rgbPath = generateRGBImagePath(objectId);

    const command = new GetObjectCommand({
      Bucket: R2_BUCKET_NAME,
      Key: rgbPath,
    });

    const response = await r2Client.send(command);

    if (!response.Body) {
      return new Response('Image not found', { status: 404 });
    }

    // Convert the readable stream to a buffer
    const chunks: Uint8Array[] = [];
    const reader = response.Body.transformToWebStream().getReader();

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
    }

    const buffer = Buffer.concat(chunks);

    return new Response(buffer, {
      status: 200,
      headers: {
        'Content-Type': 'image/png',
        'Cache-Control': 'public, max-age=604800', // 1 week
      },
    });
  } catch (error) {
    console.error('Error fetching OG image:', error);

    // Return a 404 for any error (image not found, R2 error, etc.)
    return new Response('Image not found', { status: 404 });
  }
}
