'use server';

import { createClient } from '@/lib/supabase/server';
import type { ObjectList, ObjectListMember, ObjectListWithMembership } from '@/lib/types';

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
 * Used for the object detail page to show which lists the object is in.
 */
export async function getListsWithMembership(objectId: number): Promise<{
  lists: ObjectListWithMembership[];
  error?: string;
}> {
  const supabase = await createClient();

  // Get all visible lists
  const { data: allLists, error: listsError } = await supabase
    .from('object_lists')
    .select('*')
    .order('is_system', { ascending: false })
    .order('name');

  if (listsError) {
    return { lists: [], error: listsError.message };
  }

  // Get memberships for this object
  const { data: members, error: membersError } = await supabase
    .from('object_list_members')
    .select('list_id')
    .eq('object_id', objectId);

  if (membersError) {
    return { lists: [], error: membersError.message };
  }

  const memberListIds = new Set((members ?? []).map(m => m.list_id));

  const lists: ObjectListWithMembership[] = (allLists ?? []).map(list => ({
    ...list,
    is_member: memberListIds.has(list.id),
  }));

  return { lists };
}

/**
 * Add an object to a list. Uses the object's coordinates as the durable key.
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
    .insert({
      list_id: listId,
      object_id: objectId,
      ra,
      dec,
    });

  if (error) {
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

  const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');

  const { data, error } = await supabase
    .from('object_lists')
    .insert({
      name,
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
