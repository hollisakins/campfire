import 'server-only';

import { createHash } from 'crypto';
import type { AccessContext } from '@/lib/auth/access-context';

/**
 * The access-set fingerprint on /api/v1/auth/whoami (P5-3, design §4.7).
 *
 * An incremental `campfire sync` keys on `updated_at`, and a grant or a
 * revocation bumps nothing, so a client cannot see its program set move. The
 * fingerprint is a hash of what decides that set: the admin flag (admins see
 * every program) and the sorted accessible slugs. The client keeps the value
 * of its last full sync and, when a later sync answers with another one,
 * tells the user to run `campfire sync --full`. Fields follow programs
 * (NIRCam access is through the field's programs), so they add nothing here.
 */
export function accessFingerprint(access: Pick<AccessContext, 'isAdmin' | 'accessibleSlugs'>): string {
  const programs = [...new Set(access.accessibleSlugs)].sort();
  const digest = createHash('sha256')
    .update(JSON.stringify({ admin: access.isAdmin, programs }))
    .digest('hex');
  return `sha256:${digest}`;
}
