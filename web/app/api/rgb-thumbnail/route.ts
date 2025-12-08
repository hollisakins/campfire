import { NextRequest } from 'next/server';
import { createClient } from '@/lib/supabase/server';
import { generateRGBImageUrl } from '@/lib/r2';

/**
 * GET /api/rgb-thumbnail?object_id=<object_id>
 *
 * Redirects to the signed R2 URL for the RGB thumbnail image.
 * This approach allows browser caching of the redirect while keeping
 * the actual image URL signed and temporary.
 */
export async function GET(request: NextRequest) {
  const supabase = await createClient();

  // Check authentication
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    // Return a transparent 1x1 pixel for unauthenticated users
    return new Response(
      Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64'),
      {
        status: 401,
        headers: {
          'Content-Type': 'image/gif',
        },
      }
    );
  }

  // Get object_id from query parameters
  const searchParams = request.nextUrl.searchParams;
  const objectId = searchParams.get('object_id');

  if (!objectId) {
    return new Response(
      Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64'),
      {
        status: 400,
        headers: {
          'Content-Type': 'image/gif',
        },
      }
    );
  }

  try {
    // Generate signed URL with 1 hour expiration
    const signedUrl = await generateRGBImageUrl(objectId, 3600);

    // Check if R2 is configured (placeholder URL indicates not configured)
    if (signedUrl.startsWith('#download-placeholder')) {
      return new Response(
        Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64'),
        {
          headers: {
            'Content-Type': 'image/gif',
            'Cache-Control': 'public, max-age=86400',
          },
        }
      );
    }

    // Redirect to the signed URL
    // Use 302 (temporary) so browsers don't cache the redirect permanently
    return Response.redirect(signedUrl, 302);
  } catch (error) {
    console.error('Error generating RGB thumbnail URL:', error);
    return new Response(
      Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64'),
      {
        status: 500,
        headers: {
          'Content-Type': 'image/gif',
        },
      }
    );
  }
}
