'use server';

import { createClient } from '@/lib/supabase/server';
import type {
  ObjectList,
  ObjectListMember,
  ObjectListWithMembership,
  ObjectListOverview,
  ObjectListMemberWithObject,
  ObjectListShareWithUser,
  ListShareRole,
} from '@/lib/types';

type ServerSupabase = Awaited<ReturnType<typeof createClient>>;

/**
 * Fetch the current user's share roles (issue #450), keyed by list id.
 * Used to attach `shared_role` to lists returned by the read actions.
 */
async function fetchMyShareRoles(
  supabase: ServerSupabase,
  userId: string | undefined,
): Promise<Map<number, ListShareRole>> {
  if (!userId) return new Map();
  const { data } = await supabase
    .from('object_list_shares')
    .select('list_id, role')
    .eq('user_id', userId);
  return new Map((data ?? []).map(s => [s.list_id, s.role as ListShareRole]));
}

/**
 * Whether the user may add/remove members of a list: owner, public_edit,
 * or an editor-role share. Mirrors public.member_editable_list_ids() (RLS
 * is the real enforcement; this exists for friendly error messages).
 */
async function canEditListMembers(
  supabase: ServerSupabase,
  listId: number,
  userId: string,
): Promise<{ ok: boolean; error?: string }> {
  const { data: list } = await supabase
    .from('object_lists')
    .select('created_by, visibility')
    .eq('id', listId)
    .single();
  if (!list) return { ok: false, error: 'Tag not found' };
  if (list.created_by === userId || list.visibility === 'public_edit') {
    return { ok: true };
  }
  const { data: share } = await supabase
    .from('object_list_shares')
    .select('role')
    .eq('list_id', listId)
    .eq('user_id', userId)
    .maybeSingle();
  if (share?.role === 'editor') return { ok: true };
  return { ok: false };
}

/**
 * Get all lists that an object belongs to, plus available lists for the add dropdown.
 */
export async function getListsForObject(objectId: number): Promise<{
  memberships: (ObjectListMember & { list: ObjectList })[];
  error?: string;
}> {
  const supabase = await createClient();

  const { data, error } = await supabase
    .from('object_list_members')
    .select('*, list:object_lists(*)')
    .eq('object_id', objectId);

  if (error) {
    return { memberships: [], error: error.message };
  }

  return { memberships: data ?? [] };
}

/**
 * Get all lists available to the current user (for filter dropdowns and add-to-list UI).
 * Returns system lists + user's own lists + public lists + lists shared with the user
 * (RLS does the filtering); each list carries the user's shared_role, if any.
 */
export async function getAvailableLists(): Promise<{
  lists: ObjectList[];
  error?: string;
}> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();

  const [{ data, error }, shareRoles] = await Promise.all([
    supabase
      .from('object_lists')
      .select('*')
      .order('is_system', { ascending: false })
      .order('name'),
    fetchMyShareRoles(supabase, user?.id),
  ]);

  if (error) {
    return { lists: [], error: error.message };
  }

  const lists: ObjectList[] = (data ?? []).map(list => ({
    ...list,
    shared_role: shareRoles.get(list.id) ?? null,
  }));

  return { lists };
}

/**
 * Get lists with membership status for a given object.
 */
export async function getListsWithMembership(objectId: number): Promise<{
  lists: ObjectListWithMembership[];
  error?: string;
}> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();

  const [listsResult, membersResult, shareRoles] = await Promise.all([
    supabase
      .from('object_lists')
      .select('*')
      .order('is_system', { ascending: false })
      .order('name'),
    supabase
      .from('object_list_members')
      .select('list_id')
      .eq('object_id', objectId),
    fetchMyShareRoles(supabase, user?.id),
  ]);

  if (listsResult.error) return { lists: [], error: listsResult.error.message };
  if (membersResult.error) return { lists: [], error: membersResult.error.message };

  const memberListIds = new Set((membersResult.data ?? []).map(m => m.list_id));

  const lists: ObjectListWithMembership[] = (listsResult.data ?? []).map(list => ({
    ...list,
    is_member: memberListIds.has(list.id),
    shared_role: shareRoles.get(list.id) ?? null,
  }));

  return { lists };
}

/**
 * Add an object to a list. Uses upsert with (list_id, ra, dec) as the conflict key
 * to handle the case where a coordinate-only entry already exists (e.g., from migration).
 */
export async function addObjectToList(
  listId: number,
  objectId: number,
  ra: number,
  dec: number,
): Promise<{ error?: string }> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return { error: 'Not authenticated' };

  const { data: profile } = await supabase
    .from('user_profiles')
    .select('can_comment')
    .eq('user_id', user.id)
    .single();
  if (!profile?.can_comment) return { error: 'You do not have permission to edit tags' };

  // Verify user can edit this list (owner, public_edit, or editor share)
  const { ok, error: checkError } = await canEditListMembers(supabase, listId, user.id);
  if (!ok) {
    return { error: checkError ?? 'You do not have permission to add objects to this tag' };
  }

  const { error } = await supabase
    .from('object_list_members')
    .upsert(
      { list_id: listId, object_id: objectId, ra, dec },
      { onConflict: 'list_id,ra,dec' }
    );

  if (error) {
    console.error('addObjectToList error:', error);
    return { error: error.message };
  }

  return {};
}

/**
 * Remove an object from a list.
 */
export async function removeObjectFromList(
  listId: number,
  objectId: number,
): Promise<{ error?: string }> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return { error: 'Not authenticated' };

  const { data: profile } = await supabase
    .from('user_profiles')
    .select('can_comment')
    .eq('user_id', user.id)
    .single();
  if (!profile?.can_comment) return { error: 'You do not have permission to edit tags' };

  // Verify user can edit this list (owner, public_edit, or editor share)
  const { ok, error: checkError } = await canEditListMembers(supabase, listId, user.id);
  if (!ok) {
    return { error: checkError ?? 'You do not have permission to remove objects from this tag' };
  }

  const { error } = await supabase
    .from('object_list_members')
    .delete()
    .eq('list_id', listId)
    .eq('object_id', objectId);

  if (error) {
    return { error: error.message };
  }

  return {};
}

/**
 * Validate and normalize a user-chosen slug (shortname).
 * Allowed: lowercase alphanumeric, hyphens, and a single forward slash.
 */
function validateSlug(slug: string): { valid: boolean; error?: string } {
  if (!slug || slug.length < 2) {
    return { valid: false, error: 'Shortname must be at least 2 characters' };
  }
  if (slug.length > 60) {
    return { valid: false, error: 'Shortname must be 60 characters or fewer' };
  }
  if (!/^[a-z0-9]+(?:[-/][a-z0-9]+)*$/.test(slug)) {
    return { valid: false, error: 'Shortname can only contain lowercase letters, numbers, hyphens, and one slash' };
  }
  if ((slug.match(/\//g) || []).length > 1) {
    return { valid: false, error: 'Shortname can contain at most one slash' };
  }
  return { valid: true };
}

/**
 * Check whether a slug is available (not already taken).
 */
export async function checkSlugAvailability(
  slug: string,
  excludeListId?: number,
): Promise<{ available: boolean; error?: string }> {
  const validation = validateSlug(slug);
  if (!validation.valid) {
    return { available: false, error: validation.error };
  }

  const supabase = await createClient();
  let query = supabase
    .from('object_lists')
    .select('id')
    .eq('slug', slug);

  if (excludeListId) {
    query = query.neq('id', excludeListId);
  }

  const { data } = await query;
  return { available: !data || data.length === 0 };
}

/**
 * Create a new user tag.
 */
export async function createList(
  name: string,
  description?: string,
  visibility: 'private' | 'public_read' | 'public_edit' = 'private',
  icon?: string | null,
  color?: string | null,
  slug?: string,
): Promise<{ list?: ObjectList; error?: string }> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) {
    return { error: 'Not authenticated' };
  }

  // Validate name
  const trimmedName = name.trim();
  if (!trimmedName || trimmedName.length < 2) {
    return { error: 'Tag name must be at least 2 characters' };
  }
  if (trimmedName.length > 100) {
    return { error: 'Tag name must be 100 characters or fewer' };
  }

  // Validate color format if provided
  if (color && !/^#[0-9a-f]{6}$/i.test(color)) {
    return { error: 'Invalid color format' };
  }

  // Check permissions: must be able to comment and not a group account
  const { data: profile } = await supabase
    .from('user_profiles')
    .select('can_comment, is_group_account, username')
    .eq('user_id', user.id)
    .single();

  if (!profile?.can_comment) {
    return { error: 'You do not have permission to create tags' };
  }
  if (profile.is_group_account) {
    return { error: 'Group accounts cannot create tags' };
  }

  // Use user-provided slug or generate a default
  let finalSlug: string;
  if (slug) {
    const validation = validateSlug(slug);
    if (!validation.valid) {
      return { error: validation.error };
    }
    // Check availability
    const { available } = await checkSlugAvailability(slug);
    if (!available) {
      return { error: `Shortname "${slug}" is already taken` };
    }
    finalSlug = slug;
  } else {
    // Auto-generate: {username}/{name-slug}
    const nameSlug = trimmedName.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    finalSlug = `${profile.username}/${nameSlug}`;
    // Append suffix if collision (capped to avoid unbounded DB round-trips)
    const { available } = await checkSlugAvailability(finalSlug);
    if (!available) {
      const MAX_SUFFIX = 20;
      let suffix = 2;
      while (suffix <= MAX_SUFFIX && !(await checkSlugAvailability(`${finalSlug}-${suffix}`)).available) {
        suffix++;
      }
      if (suffix > MAX_SUFFIX) {
        return { error: 'Could not auto-generate a unique shortname. Please set one manually.' };
      }
      finalSlug = `${finalSlug}-${suffix}`;
    }
  }

  const { data, error } = await supabase
    .from('object_lists')
    .insert({
      name: trimmedName,
      slug: finalSlug,
      description: description ?? null,
      visibility,
      is_system: false,
      icon: icon ?? null,
      color: color ?? null,
      created_by: user.id,
    })
    .select()
    .single();

  if (error) {
    // Handle race condition: slug was available at check time but taken by insert time
    if (error.code === '23505') {
      return { error: `Shortname "${finalSlug}" is already taken` };
    }
    return { error: error.message };
  }

  return { list: data };
}

/**
 * Delete a user list (cannot delete system lists).
 */
export async function deleteList(listId: number): Promise<{ error?: string }> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return { error: 'Not authenticated' };

  const { data: list } = await supabase
    .from('object_lists')
    .select('created_by, is_system')
    .eq('id', listId)
    .single();
  if (!list) return { error: 'Tag not found' };
  if (list.is_system) return { error: 'Cannot delete system tags' };
  if (list.created_by !== user.id) return { error: 'Permission denied' };

  const { error } = await supabase
    .from('object_lists')
    .delete()
    .eq('id', listId);

  if (error) {
    return { error: error.message };
  }

  return {};
}

/**
 * Update a user tag (name, slug, description, visibility, icon, color).
 */
export async function updateList(
  listId: number,
  updates: { name?: string; slug?: string; description?: string; visibility?: 'private' | 'public_read' | 'public_edit'; icon?: string | null; color?: string | null },
): Promise<{ error?: string }> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return { error: 'Not authenticated' };

  const { data: list } = await supabase
    .from('object_lists')
    .select('created_by, is_system')
    .eq('id', listId)
    .single();
  if (!list) return { error: 'Tag not found' };
  if (list.is_system) return { error: 'Cannot modify system tags' };
  if (list.created_by !== user.id) return { error: 'Permission denied' };

  // Validate slug if changing
  if (updates.slug) {
    const validation = validateSlug(updates.slug);
    if (!validation.valid) {
      return { error: validation.error };
    }
    const { available } = await checkSlugAvailability(updates.slug, listId);
    if (!available) {
      return { error: `Shortname "${updates.slug}" is already taken` };
    }
  }

  // Validate color format if provided
  if (updates.color && !/^#[0-9a-f]{6}$/i.test(updates.color)) {
    return { error: 'Invalid color format' };
  }

  const { error } = await supabase
    .from('object_lists')
    .update({ ...updates, updated_at: new Date().toISOString() })
    .eq('id', listId);

  if (error) {
    return { error: error.message };
  }

  return {};
}

/**
 * Helper: batch-fetch creator names from user_profiles for a set of user IDs.
 */
async function fetchCreatorNames(
  supabase: Awaited<ReturnType<typeof createClient>>,
  userIds: string[],
): Promise<Map<string, string>> {
  if (userIds.length === 0) return new Map();

  const { data } = await supabase
    .from('user_profiles')
    .select('user_id, full_name')
    .in('user_id', userIds);

  const map = new Map<string, string>();
  for (const row of data ?? []) {
    map.set(row.user_id, row.full_name);
  }
  return map;
}

/**
 * Get all visible lists with member counts (for /lists browse page).
 * Returns system lists + public lists + user's own private lists.
 */
export async function getListsOverview(): Promise<{
  lists: ObjectListOverview[];
  error?: string;
}> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();

  const [{ data, error }, shareRoles] = await Promise.all([
    supabase
      .from('object_lists')
      .select('*, object_list_members(count)')
      .order('is_system', { ascending: false })
      .order('name'),
    fetchMyShareRoles(supabase, user?.id),
  ]);

  if (error) {
    return { lists: [], error: error.message };
  }

  const creatorIds = [...new Set((data ?? []).map(l => l.created_by).filter(Boolean))] as string[];
  const nameMap = await fetchCreatorNames(supabase, creatorIds);

  const lists: ObjectListOverview[] = (data ?? []).map(row => {
    const { object_list_members, ...list } = row;
    return {
      ...list,
      member_count: object_list_members?.[0]?.count ?? 0,
      creator_name: list.created_by ? nameMap.get(list.created_by) ?? null : null,
      shared_role: shareRoles.get(list.id) ?? null,
    };
  });

  return { lists };
}

/**
 * Get a single list by slug with paginated members (for /lists/[slug] detail page).
 */
export async function getListBySlug(
  slug: string,
  page: number = 1,
  pageSize: number = 50,
): Promise<{
  list: ObjectListOverview | null;
  members: ObjectListMemberWithObject[];
  totalMembers: number;
  error?: string;
}> {
  const supabase = await createClient();

  // Fetch the list
  const { data: listData, error: listError } = await supabase
    .from('object_lists')
    .select('*, object_list_members(count)')
    .eq('slug', slug)
    .single();

  if (listError) {
    return { list: null, members: [], totalMembers: 0, error: listError.message };
  }

  // Creator name + current user's share role (issue #450)
  const { data: { user } } = await supabase.auth.getUser();
  const [nameMap, shareResult] = await Promise.all([
    listData.created_by
      ? fetchCreatorNames(supabase, [listData.created_by])
      : Promise.resolve(new Map<string, string>()),
    user
      ? supabase
          .from('object_list_shares')
          .select('role')
          .eq('list_id', listData.id)
          .eq('user_id', user.id)
          .maybeSingle()
      : Promise.resolve({ data: null }),
  ]);

  const { object_list_members: countArr, ...listFields } = listData;
  const totalMembers = countArr?.[0]?.count ?? 0;
  const list: ObjectListOverview = {
    ...listFields,
    member_count: totalMembers,
    creator_name: listFields.created_by ? nameMap.get(listFields.created_by) ?? null : null,
    shared_role: (shareResult.data?.role as ListShareRole | undefined) ?? null,
  };

  // Fetch paginated members with joined object data. NOTE: n_spectra / max_snr
  // are NOT read off the objects row — those stored aggregates are computed
  // across ALL member programs at deploy time, so reading them directly would
  // leak proprietary member metadata on mixed-program objects (same class of
  // leak fixed on the catalog/detail/CSV/map paths). redshift / redshift_quality
  // stay visible (object science, per the access policy).
  const offset = (page - 1) * pageSize;
  const { data: membersData, error: membersError } = await supabase
    .from('object_list_members')
    .select('*, object:objects(id, object_id, field, ra, dec, redshift, redshift_quality)')
    .eq('list_id', list.id)
    .order('added_at', { ascending: false })
    .range(offset, offset + pageSize - 1);

  if (membersError) {
    return { list, members: [], totalMembers, error: membersError.message };
  }

  // Scope n_spectra / max_snr to the viewer's accessible programs by recomputing
  // from member targets + spectra, which RLS filters to accessible programs
  // (mirrors object_scoped_aggregates()). One extra query for the page.
  const memberObjectIds = (membersData ?? [])
    .map(m => (m.object as { id: number } | null)?.id)
    .filter((id): id is number => id != null);

  const scopedAgg = new Map<number, { n_spectra: number; max_snr: number | null }>();
  if (memberObjectIds.length > 0) {
    const { data: scopeRows } = await supabase
      .from('targets')
      .select('object_id, spectra(signal_to_noise)')
      .in('object_id', memberObjectIds);
    for (const t of (scopeRows ?? []) as { object_id: number | null; spectra: { signal_to_noise: number | null }[] | null }[]) {
      if (t.object_id == null) continue;
      const entry = scopedAgg.get(t.object_id) ?? { n_spectra: 0, max_snr: null };
      for (const s of t.spectra ?? []) {
        entry.n_spectra += 1;
        if (s.signal_to_noise != null) {
          entry.max_snr = entry.max_snr == null ? s.signal_to_noise : Math.max(entry.max_snr, s.signal_to_noise);
        }
      }
      scopedAgg.set(t.object_id, entry);
    }
  }

  const members: ObjectListMemberWithObject[] = (membersData ?? []).map(m => {
    const obj = m.object as { id: number } | null;
    if (!obj) return { ...m, object: null } as ObjectListMemberWithObject;
    const agg = scopedAgg.get(obj.id) ?? { n_spectra: 0, max_snr: null };
    return { ...m, object: { ...obj, n_spectra: agg.n_spectra, max_snr: agg.max_snr } } as ObjectListMemberWithObject;
  });

  return {
    list,
    members,
    totalMembers,
  };
}

/**
 * Get lists created by the current user with member counts (for /profile/lists).
 */
export async function getMyLists(): Promise<{
  lists: ObjectListOverview[];
  error?: string;
}> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) {
    return { lists: [], error: 'Not authenticated' };
  }

  const { data, error } = await supabase
    .from('object_lists')
    .select('*, object_list_members(count)')
    .eq('created_by', user.id)
    .eq('is_system', false)
    .order('created_at', { ascending: false });

  if (error) {
    return { lists: [], error: error.message };
  }

  const lists: ObjectListOverview[] = (data ?? []).map(row => {
    const { object_list_members, ...list } = row;
    return {
      ...list,
      member_count: object_list_members?.[0]?.count ?? 0,
      creator_name: null, // Own lists, no need to show creator name
    };
  });

  return { lists };
}

// =============================================================================
// Tag sharing (issue #450)
// =============================================================================
// RLS on object_list_shares enforces owner-only grant/role-change/revoke (a
// grantee may only delete their own share); the checks here exist for
// friendly error messages.

/**
 * Get the shares on a list, with grantee names (owner-facing management UI).
 */
export async function getListShares(listId: number): Promise<{
  shares: ObjectListShareWithUser[];
  error?: string;
}> {
  const supabase = await createClient();

  const { data: shares, error } = await supabase
    .from('object_list_shares')
    .select('*')
    .eq('list_id', listId)
    .order('granted_at', { ascending: true });

  if (error) {
    return { shares: [], error: error.message };
  }
  if (!shares || shares.length === 0) {
    return { shares: [] };
  }

  // No PostgREST-resolvable FK from shares.user_id to user_profiles (it
  // references auth.users), so join manually.
  const userIds = [...new Set(shares.map(s => s.user_id))];
  const { data: profiles } = await supabase
    .from('user_profiles')
    .select('user_id, username, full_name')
    .in('user_id', userIds);
  const profileMap = new Map((profiles ?? []).map(p => [p.user_id, p]));

  return {
    shares: shares.map(s => ({
      ...s,
      username: profileMap.get(s.user_id)?.username ?? null,
      full_name: profileMap.get(s.user_id)?.full_name ?? null,
    })),
  };
}

/**
 * Search users to share a tag with (autocomplete). Matches username or full
 * name; excludes the current user.
 */
export async function searchUsersForSharing(query: string): Promise<{
  users: { user_id: string; username: string; full_name: string }[];
  error?: string;
}> {
  const trimmed = query.trim();
  if (trimmed.length < 2) return { users: [] };

  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return { users: [], error: 'Not authenticated' };

  // Escape ILIKE wildcards, then double-quote the value for the or() filter —
  // PostgREST's or() grammar splits on bare commas/parens, so an unquoted
  // "Doe, John" would be parsed as two malformed conditions (HTTP 400).
  const escaped = trimmed.replace(/[%_\\]/g, ch => `\\${ch}`).replace(/"/g, '\\"');

  const { data, error } = await supabase
    .from('user_profiles')
    .select('user_id, username, full_name')
    .neq('user_id', user.id)
    .or(`username.ilike."%${escaped}%",full_name.ilike."%${escaped}%"`)
    .order('username')
    .limit(8);

  if (error) {
    return { users: [], error: error.message };
  }

  return { users: data ?? [] };
}

/**
 * Share a tag with another user (owner only, non-system tags).
 */
export async function addListShare(
  listId: number,
  granteeUserId: string,
  role: ListShareRole,
): Promise<{ error?: string }> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return { error: 'Not authenticated' };

  const { data: list } = await supabase
    .from('object_lists')
    .select('created_by, is_system')
    .eq('id', listId)
    .single();
  if (!list) return { error: 'Tag not found' };
  if (list.is_system) return { error: 'System tags cannot be shared' };
  if (list.created_by !== user.id) return { error: 'Only the tag owner can manage sharing' };
  if (granteeUserId === user.id) return { error: 'You cannot share a tag with yourself' };

  const { error } = await supabase
    .from('object_list_shares')
    .insert({
      list_id: listId,
      user_id: granteeUserId,
      role,
      granted_by: user.id,
    });

  if (error) {
    if (error.code === '23505') {
      return { error: 'This tag is already shared with that user' };
    }
    return { error: error.message };
  }

  return {};
}

/**
 * Change a share's role (owner only).
 */
export async function updateListShare(
  shareId: number,
  role: ListShareRole,
): Promise<{ error?: string }> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return { error: 'Not authenticated' };

  const { data, error } = await supabase
    .from('object_list_shares')
    .update({ role })
    .eq('id', shareId)
    .select('id');

  if (error) {
    return { error: error.message };
  }
  // RLS silently filters rows the caller may not update
  if (!data || data.length === 0) {
    return { error: 'Share not found or permission denied' };
  }

  return {};
}

/**
 * Leave a tag that was shared with the current user (delete own share).
 */
export async function leaveSharedList(listId: number): Promise<{ error?: string }> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return { error: 'Not authenticated' };

  const { data, error } = await supabase
    .from('object_list_shares')
    .delete()
    .eq('list_id', listId)
    .eq('user_id', user.id)
    .select('id');

  if (error) {
    return { error: error.message };
  }
  if (!data || data.length === 0) {
    return { error: 'This tag is not shared with you' };
  }

  return {};
}

/**
 * Revoke a share. The owner can revoke any share on their tag; a grantee can
 * remove their own share (leave the tag).
 */
export async function removeListShare(shareId: number): Promise<{ error?: string }> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return { error: 'Not authenticated' };

  const { data, error } = await supabase
    .from('object_list_shares')
    .delete()
    .eq('id', shareId)
    .select('id');

  if (error) {
    return { error: error.message };
  }
  if (!data || data.length === 0) {
    return { error: 'Share not found or permission denied' };
  }

  return {};
}
