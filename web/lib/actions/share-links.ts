'use server';

import { createHash, randomBytes } from 'crypto';
import { createClient, createServiceClient } from '@/lib/supabase/server';

// ---------------------------------------------------------------------------
// Share links (docs/design-public-mirror.md).
//
// Admin-minted URLs scoped to one NIRCam field or one NIRSpec observation, for
// collaborators with no CAMPFIRE account. Each link is backed by a synthetic
// `auth.users` principal -- a "link account" -- so the visitor authenticates
// like any other user and every existing reader in the portal works unchanged.
// What a link account may actually SEE is decided entirely by RLS
// (supabase/schemas/policies.sql), not here.
//
// A link is scoped to a field/observation and never to a deployment: a scope is
// deployed many times and any one deployment may be narrower than the scope, so
// the link shows the scope's current state rather than a snapshot.
// ---------------------------------------------------------------------------

/** 32 url-safe chars, ~190 bits. Unguessable is the security model. */
function generateToken(): string {
  return randomBytes(24).toString('base64url').slice(0, 32);
}

/** Never leaves the server; stored so /s/<token> can mint a cookie session. */
function generateLinkPassword(): string {
  return randomBytes(32).toString('base64url');
}

/**
 * Username for a link account.
 *
 * Derived as a hex digest of the token rather than a slice of it: the token is
 * base64url, so a slice can end in `-` or `_`, and user_profiles_username_check
 * requires the first and last characters to be alphanumeric. Hex is always
 * [0-9a-f], so this satisfies the constraint by construction instead of by
 * luck. Deterministic per token, and unique because the token is.
 */
function linkUsername(token: string): string {
  return `link-${createHash('sha256').update(token).digest('hex').slice(0, 16)}`;
}

async function requireAdmin() {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) throw new Error('Not authenticated');

  const { data: profile } = await supabase
    .from('user_profiles')
    .select('is_admin')
    .eq('user_id', user.id)
    .single();

  if (!profile?.is_admin) throw new Error('Admin access required');
  return { supabase, userId: user.id };
}

export interface ShareLinkRow {
  token: string;
  label: string;
  observation: string | null;
  field: string | null;
  include_drafts: boolean;
  allow_download: boolean;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  last_seen_at: string | null;
  view_count: number;
}

export interface ShareLinksResult {
  links: ShareLinkRow[];
  error?: string;
}

// Columns are listed explicitly and never with `*`: link_password must not be
// selected. The column-level REVOKE in tables.sql already blocks it for
// `authenticated`, but a `select('*')` would turn that into a hard error rather
// than a silent omission, so name the columns.
const LINK_COLUMNS =
  'token, label, observation, field, include_drafts, allow_download, ' +
  'created_at, expires_at, revoked_at, last_seen_at, view_count';

export async function getShareLinks(): Promise<ShareLinksResult> {
  try {
    const { supabase } = await requireAdmin();

    const { data, error } = await supabase
      .from('share_links')
      .select(LINK_COLUMNS)
      .order('created_at', { ascending: false });

    if (error) return { links: [], error: error.message };
    return { links: (data ?? []) as unknown as ShareLinkRow[] };
  } catch (err) {
    return { links: [], error: err instanceof Error ? err.message : 'Failed to load share links' };
  }
}

export interface MintShareLinkParams {
  label: string;
  observation?: string | null;
  field?: string | null;
  includeDrafts?: boolean;
  allowDownload?: boolean;
  /** ISO timestamp. Omit for a link that never expires (the default). */
  expiresAt?: string | null;
}

export interface MintShareLinkResult {
  token?: string;
  error?: string;
}

/**
 * Create a share link and the link account behind it.
 *
 * Ordering matters: the auth user is created first, then the profile, then the
 * share_links row. If a later step fails we delete the auth user, which cascades
 * the rest away -- otherwise a failed mint would leave an orphan principal with
 * a profile and no scope.
 */
export async function mintShareLink(params: MintShareLinkParams): Promise<MintShareLinkResult> {
  try {
    const { userId } = await requireAdmin();

    const observation = params.observation?.trim() || null;
    const field = params.field?.trim() || null;
    // Mirrors share_links_scope_check; checked here so the UI gets a clear
    // message instead of a constraint violation.
    if ((observation === null) === (field === null)) {
      return { error: 'A share link must be scoped to exactly one observation or one field.' };
    }
    if (!params.label?.trim()) {
      return { error: 'A label is required (it is how you will recognise this link later).' };
    }

    const service = createServiceClient();
    const token = generateToken();
    const password = generateLinkPassword();

    const { data: created, error: userError } = await service.auth.admin.createUser({
      // Non-routable by design: nothing should ever mail a link account.
      email: `link+${token}@shared.invalid`,
      password,
      email_confirm: true,
      // No self_signup flag, so handle_new_user() does not auto-provision a
      // profile -- we create it below with the read-only flags set.
      user_metadata: { share_link: true },
    });

    if (userError || !created?.user) {
      return { error: `Failed to create link account: ${userError?.message ?? 'unknown error'}` };
    }
    const linkUserId = created.user.id;

    const cleanup = async (message: string): Promise<MintShareLinkResult> => {
      await service.auth.admin.deleteUser(linkUserId);
      return { error: message };
    };

    // can_comment/can_inspect false is also enforced by the
    // user_profiles_link_account_readonly CHECK -- passed explicitly here so the
    // intent is legible at the call site, not only in the constraint.
    const { error: profileError } = await service.from('user_profiles').insert({
      user_id: linkUserId,
      username: linkUsername(token),
      full_name: params.label.trim(),
      is_admin: false,
      can_comment: false,
      can_inspect: false,
      is_group_account: false,
      is_link_account: true,
    });
    if (profileError) return cleanup(`Failed to create link profile: ${profileError.message}`);

    const { error: linkError } = await service.from('share_links').insert({
      token,
      label: params.label.trim(),
      observation,
      field,
      link_user_id: linkUserId,
      link_password: password,
      include_drafts: params.includeDrafts ?? false,
      allow_download: params.allowDownload ?? true,
      created_by: userId,
      expires_at: params.expiresAt ?? null,
    });
    if (linkError) return cleanup(`Failed to create share link: ${linkError.message}`);

    return { token };
  } catch (err) {
    return { error: err instanceof Error ? err.message : 'Failed to mint share link' };
  }
}

/**
 * Revoke a link.
 *
 * Stamps revoked_at AND deletes the link account. The stamp is what the RLS
 * helpers read, so revocation bites on the visitor's very next query; deleting
 * the account additionally kills any live cookie session at its next token
 * refresh, rather than letting it run for the rest of the hour.
 *
 * The delete cascades the share_links row away (share_links_link_user_id_fkey
 * ON DELETE CASCADE), so the stamp is deliberately written first: if the delete
 * fails, the link is already dead rather than merely scheduled to die.
 */
export async function revokeShareLink(token: string): Promise<{ error?: string }> {
  try {
    await requireAdmin();
    const service = createServiceClient();

    const { data: link, error: readError } = await service
      .from('share_links')
      .select('link_user_id')
      .eq('token', token)
      .single();

    if (readError || !link) return { error: 'Share link not found' };

    const { error: stampError } = await service
      .from('share_links')
      .update({ revoked_at: new Date().toISOString() })
      .eq('token', token);
    if (stampError) return { error: `Failed to revoke: ${stampError.message}` };

    const { error: deleteError } = await service.auth.admin.deleteUser(link.link_user_id);
    if (deleteError) {
      // The link is already inert (revoked_at is set and the helpers read it),
      // so surface this without pretending the revoke failed.
      return { error: `Link revoked, but its account could not be deleted: ${deleteError.message}` };
    }

    return {};
  } catch (err) {
    return { error: err instanceof Error ? err.message : 'Failed to revoke share link' };
  }
}

export interface ScopeOptions {
  observations: string[];
  fields: string[];
  error?: string;
}

/** Scope pickers for the mint form. Deployed scopes only — you cannot share
 *  something that has never been deployed. */
export async function getShareableScopes(): Promise<ScopeOptions> {
  try {
    const { supabase } = await requireAdmin();

    const [obsResult, fieldResult] = await Promise.all([
      supabase.from('observations').select('name').order('name'),
      supabase.from('fields').select('name').order('name'),
    ]);

    return {
      observations: (obsResult.data ?? []).map((o: { name: string }) => o.name),
      fields: (fieldResult.data ?? []).map((f: { name: string }) => f.name),
    };
  } catch (err) {
    return {
      observations: [],
      fields: [],
      error: err instanceof Error ? err.message : 'Failed to load scopes',
    };
  }
}
