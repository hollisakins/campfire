'use server';

import { createClient } from '@/lib/supabase/server';
import type {
  ObjectList,
  ObjectListMember,
  ObjectListWithMembership,
  ObjectListOverview,
  ObjectListMemberWithObject,
} from '@/lib/types';

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
 * Returns system lists + user's own lists + public_edit lists.
 */
export async function getAvailableLists(): Promise<{
  lists: ObjectList[];
  error?: string;
}> {
  const supabase = await createClient();

  const { data, error } = await supabase
    .from('object_lists')
    .select('*')
    .order('is_system', { ascending: false })
    .order('name');

  if (error) {
    return { lists: [], error: error.message };
  }

  return { lists: data ?? [] };
}

/**
 * Get lists with membership status for a given object.
 */
export async function getListsWithMembership(objectId: number): Promise<{
  lists: ObjectListWithMembership[];
  error?: string;
}> {
  const { lists: allLists, error: listsError } = await getAvailableLists();
  if (listsError) return { lists: [], error: listsError };

  const supabase = await createClient();
  const { data: members, error: membersError } = await supabase
    .from('object_list_members')
    .select('list_id')
    .eq('object_id', objectId);

  if (membersError) {
    return { lists: [], error: membersError.message };
  }

  const memberListIds = new Set((members ?? []).map(m => m.list_id));

  const lists: ObjectListWithMembership[] = allLists.map(list => ({
    ...list,
    is_member: memberListIds.has(list.id),
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
 * Create a new user list.
 */
export async function createList(
  name: string,
  description?: string,
  visibility: 'private' | 'public_read' | 'public_edit' = 'private',
): Promise<{ list?: ObjectList; error?: string }> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) {
    return { error: 'Not authenticated' };
  }

  // Validate name
  const trimmedName = name.trim();
  if (!trimmedName || trimmedName.length < 2) {
    return { error: 'List name must be at least 2 characters' };
  }
  if (trimmedName.length > 100) {
    return { error: 'List name must be 100 characters or fewer' };
  }

  // Check permissions: must be able to comment and not a group account
  const { data: profile } = await supabase
    .from('user_profiles')
    .select('can_comment, is_group_account, username')
    .eq('user_id', user.id)
    .single();

  if (!profile?.can_comment) {
    return { error: 'You do not have permission to create lists' };
  }
  if (profile.is_group_account) {
    return { error: 'Group accounts cannot create lists' };
  }

  // Generate slug: {username}/{name-slug}
  const nameSlug = trimmedName.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  const baseSlug = `${profile.username}/${nameSlug}`;

  // Check for slug collisions and append suffix if needed
  const { data: existing } = await supabase
    .from('object_lists')
    .select('slug')
    .or(`slug.eq.${baseSlug},slug.like.${baseSlug}-%`);

  let slug = baseSlug;
  if (existing && existing.length > 0) {
    const existingSlugs = new Set(existing.map(r => r.slug));
    if (existingSlugs.has(baseSlug)) {
      let suffix = 2;
      while (existingSlugs.has(`${baseSlug}-${suffix}`)) {
        suffix++;
      }
      slug = `${baseSlug}-${suffix}`;
    }
  }

  const { data, error } = await supabase
    .from('object_lists')
    .insert({
      name: trimmedName,
      slug,
      description: description ?? null,
      visibility,
      is_system: false,
      created_by: user.id,
    })
    .select()
    .single();

  if (error) {
    return { error: error.message };
  }

  return { list: data };
}

/**
 * Delete a user list (cannot delete system lists).
 */
export async function deleteList(listId: number): Promise<{ error?: string }> {
  const supabase = await createClient();

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
 * Update a user list (name, description, visibility).
 */
export async function updateList(
  listId: number,
  updates: { name?: string; description?: string; visibility?: string },
): Promise<{ error?: string }> {
  const supabase = await createClient();

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

  const { data, error } = await supabase
    .from('object_lists')
    .select('*, object_list_members(count)')
    .order('is_system', { ascending: false })
    .order('name');

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

  // Creator name
  const nameMap = listData.created_by
    ? await fetchCreatorNames(supabase, [listData.created_by])
    : new Map<string, string>();

  const { object_list_members: countArr, ...listFields } = listData;
  const totalMembers = countArr?.[0]?.count ?? 0;
  const list: ObjectListOverview = {
    ...listFields,
    member_count: totalMembers,
    creator_name: listFields.created_by ? nameMap.get(listFields.created_by) ?? null : null,
  };

  // Fetch paginated members with joined object data
  const offset = (page - 1) * pageSize;
  const { data: membersData, error: membersError } = await supabase
    .from('object_list_members')
    .select('*, object:objects(id, object_id, field, ra, dec, best_redshift, best_redshift_quality, n_spectra, max_snr)')
    .eq('list_id', list.id)
    .order('added_at', { ascending: false })
    .range(offset, offset + pageSize - 1);

  if (membersError) {
    return { list, members: [], totalMembers, error: membersError.message };
  }

  return {
    list,
    members: (membersData ?? []) as ObjectListMemberWithObject[],
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
