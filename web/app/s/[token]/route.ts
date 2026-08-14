import { NextRequest, NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';

// ---------------------------------------------------------------------------
// GET /s/<token> — exchange a share-link token for a session, then redirect to
// the shared scope. See docs/design-public-mirror.md §4.3.
//
// The token is a PATH SEGMENT that immediately redirects, not a query
// parameter: it must not linger in the address bar of the pages the visitor
// then browses, and must not ride along in Referer headers to third parties.
//
// Signing in with the link account's stored password is deliberately boring --
// it reuses the same @supabase/ssr cookie plumbing a normal login uses, so
// there is no second session format to keep in sync with Supabase.
// ---------------------------------------------------------------------------

export const dynamic = 'force-dynamic';

/** Where a link lands. Observation links open the spectra browser filtered to
 *  the scope; field links open the NIRCam field page. */
function scopeDestination(link: { observation: string | null; field: string | null }): string {
  if (link.field) return `/nircam/${encodeURIComponent(link.field)}`;
  return `/nirspec?observations=${encodeURIComponent(link.observation ?? '')}`;
}

function deadLink(request: NextRequest, reason: 'unknown' | 'revoked' | 'expired') {
  const url = new URL('/s/inactive', request.url);
  url.searchParams.set('reason', reason);
  return NextResponse.redirect(url);
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ token: string }> }
) {
  const { token } = await params;

  // Service client: share_links is admin-only under RLS and the visitor is
  // anonymous at this point.
  const service = createServiceClient();

  const { data: link, error } = await service
    .from('share_links')
    .select('token, label, observation, field, link_user_id, link_password, expires_at, revoked_at, view_count')
    .eq('token', token)
    .maybeSingle();

  if (error || !link) return deadLink(request, 'unknown');
  if (link.revoked_at) return deadLink(request, 'revoked');
  if (link.expires_at && new Date(link.expires_at) <= new Date()) {
    return deadLink(request, 'expired');
  }

  // Cookie client: signInWithPassword writes the session cookies through the
  // same path a normal login uses.
  const supabase = await createClient();
  const { error: signInError } = await supabase.auth.signInWithPassword({
    email: `link+${link.token}@shared.invalid`,
    password: link.link_password,
  });

  if (signInError) {
    console.error('Share link sign-in failed:', signInError.message);
    return deadLink(request, 'unknown');
  }

  // Usage counters are the signal an admin uses to spot a link they have
  // forgotten about -- links never expire by default, so this is the only thing
  // that makes a stale link visible. Best-effort: never fail a visit over it.
  await service
    .from('share_links')
    .update({ last_seen_at: new Date().toISOString(), view_count: (link.view_count ?? 0) + 1 })
    .eq('token', link.token);

  return NextResponse.redirect(new URL(scopeDestination(link), request.url));
}
