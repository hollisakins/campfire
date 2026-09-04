// Read side of object lists shared by the list actions (lib/actions/lists.ts)
// and the GET routes (perf T2-C, #506). Plain server module: the object page
// reads list membership on every mount, and as a server action that read
// queued behind the page's other actions.
import 'server-only';

import type { SupabaseClient } from '@supabase/supabase-js';
import type { ListShareRole, ObjectListWithMembership } from '@/lib/types';

/**
 * Fetch the current user's share roles (issue #450), keyed by list id.
 * Used to attach `shared_role` to lists returned by the read paths.
 */
export async function fetchMyShareRoles(
  supabase: SupabaseClient,
  userId: string | undefined,
): Promise<Map<number, ListShareRole>> {
  if (!userId) return new Map();
  const { data } = await supabase
    .from('object_list_shares')
    .select('list_id, role')
    .eq('user_id', userId);
  return new Map((data ?? []).map((s: { list_id: number; role: string }) => [s.list_id, s.role as ListShareRole]));
}

/**
 * Every list the viewer can see, each flagged with whether `objectId` (the
 * objects.id primary key) is a member and with the viewer's share role.
 * RLS on object_lists / object_list_members scopes the rows.
 */
export async function listsWithMembership(
  supabase: SupabaseClient,
  userId: string | undefined,
  objectId: number,
): Promise<{ lists: ObjectListWithMembership[]; error?: string }> {
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
    fetchMyShareRoles(supabase, userId),
  ]);

  if (listsResult.error) return { lists: [], error: listsResult.error.message };
  if (membersResult.error) return { lists: [], error: membersResult.error.message };

  const memberListIds = new Set((membersResult.data ?? []).map((m: { list_id: number }) => m.list_id));

  const lists: ObjectListWithMembership[] = (listsResult.data ?? []).map((list: ObjectListWithMembership) => ({
    ...list,
    is_member: memberListIds.has(list.id),
    shared_role: shareRoles.get(list.id) ?? null,
  }));

  return { lists };
}
