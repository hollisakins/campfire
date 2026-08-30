import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

// ---------------------------------------------------------------------------
// GET /s/exit — leave a shared view. The mirror of /s/<token>: that route
// mints a link-account cookie session, this one destroys it and lands on the
// public home page. Reached from the wordmark in the stripped shared-view nav
// (docs/design-public-mirror.md §7).
//
// This is a route handler rather than a client-side signOut() because
// supabase-js only removes the local session when its /logout call succeeds:
// a transient auth-server error would leave the visitor signed in to the link
// account — on pages that all render empty for it, with no other route to a
// sign-in. Here the cookie deletion is unconditional, so the exit works even
// when GoTrue does not.
//
// A static segment beats the /s/[token] dynamic sibling in the App Router, so
// "exit" is never looked up as a token.
// ---------------------------------------------------------------------------

export const dynamic = 'force-dynamic';

export async function GET(request: NextRequest) {
  // Best-effort: revoke this browser's refresh token server-side. Scope
  // 'local' because the link account is shared by every holder of the link --
  // a global sign-out would revoke every other visitor's session too.
  try {
    const supabase = await createClient();
    await supabase.auth.signOut({ scope: 'local' });
  } catch {
    // GoTrue being unreachable must not trap the visitor in the shared view;
    // the cookie deletion below is the actual exit.
  }

  const response = NextResponse.redirect(new URL('/', request.url));
  for (const { name } of request.cookies.getAll()) {
    if (name.startsWith('sb-')) response.cookies.delete(name);
  }
  return response;
}
