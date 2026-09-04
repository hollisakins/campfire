import { NextRequest } from 'next/server';
import { createServiceClient } from '@/lib/supabase/server';

/**
 * GET /nirspec/targets/[id] → 307 to the parent object's page.
 *
 * Target URLs are the pre-unified-object-page address (design-unified-
 * object-page.md) and survive in bookmarks, comments and the CLI. This used
 * to be a page whose `redirect()` streamed behind `loading.tsx`, so the
 * browser received a 36 kB skeleton document plus a `meta refresh` and paid
 * a second navigation; it also looked the row up twice (`generateMetadata`
 * + page). One service-role lookup and a real HTTP redirect instead (#497).
 * Social crawlers follow redirects, so the object page's OpenGraph metadata
 * still serves shared target links.
 *
 * 307 rather than 308: a target's parent object can change across
 * re-deployments (objects are re-matched), and browsers cache permanent
 * redirects with no invalidation path.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const targetId = decodeURIComponent(id);

  // Service-role read with no auth. Gate on has_published_spectrum so a target
  // with no published spectrum is treated as nonexistent rather than leaking
  // its target_id → object_id mapping. No-op in B1.
  const supabase = createServiceClient();
  const { data } = await supabase
    .from('targets')
    .select('objects!inner(object_id)')
    .eq('target_id', targetId)
    .eq('has_published_spectrum', true)
    .maybeSingle();

  // targets.object_id is the FK (integer); objects.object_id is the human-readable ID
  const joined = data?.objects as { object_id: string } | { object_id: string }[] | null | undefined;
  const objectId = Array.isArray(joined) ? joined[0]?.object_id : joined?.object_id;
  if (!objectId) {
    return notFoundResponse(targetId);
  }

  // Object detail page has no tabs or per-grating sub-tabs; drop those params
  // and forward only filter/sort so bookmarked nav state still resolves.
  const forwarded = new URLSearchParams();
  request.nextUrl.searchParams.forEach((value, key) => {
    if (value && key !== 'grating' && key !== 'tab') forwarded.set(key, value);
  });
  const qs = forwarded.toString();

  return new Response(null, {
    status: 307,
    headers: {
      // Relative Location is valid (RFC 9110 §10.2.2) and sidesteps the
      // proxy-rewritten origin a Vercel function sees on request.url.
      Location: `/nirspec/objects/${encodeURIComponent(objectId)}${qs ? `?${qs}` : ''}`,
      'Cache-Control': 'private, no-store',
    },
  });
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function notFoundResponse(targetId: string): Response {
  const safeId = escapeHtml(targetId);
  const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>Target not found - CAMPFIRE</title>
<style>body{font-family:system-ui,sans-serif;margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center;background:#0f1115;color:#e6e6e6}main{padding:2rem}h1{font-size:1.5rem;margin:0 0 .5rem}code{font-family:ui-monospace,monospace}a{color:#f0a05a}</style>
</head><body><main><h1>Target not found</h1><p><code>${safeId}</code> has no published spectra.</p><p><a href="/nirspec">Back to the NIRSpec archive</a></p></main></body></html>`;
  return new Response(html, {
    status: 404,
    headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'private, no-store' },
  });
}
