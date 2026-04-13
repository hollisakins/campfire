import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * GET /api/profile/comments
 *
 * Fetch paginated comments for the current user.
 * Query params:
 *   - page: Page number (default: 1)
 *   - limit: Items per page (default: 10, max: 50)
 */
export async function GET(request: NextRequest) {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  try {
    const searchParams = request.nextUrl.searchParams;
    const page = Math.max(1, parseInt(searchParams.get('page') || '1', 10));
    const limit = Math.min(50, Math.max(1, parseInt(searchParams.get('limit') || '10', 10)));
    const offset = (page - 1) * limit;

    // Fetch comments with target/object info
    const { data: comments, count: totalCount, error } = await supabase
      .from('comments')
      .select(`
        id,
        content,
        created_at,
        edited_at,
        targets (
          id,
          target_id
        ),
        objects (
          id,
          object_id
        )
      `, { count: 'exact' })
      .eq('user_id', user.id)
      .eq('is_deleted', false)
      .order('created_at', { ascending: false })
      .range(offset, offset + limit - 1);

    if (error) {
      console.error('Error fetching comments:', error);
      return NextResponse.json({ error: 'Failed to fetch comments' }, { status: 500 });
    }

    // Transform comments to match CommentHistoryItem interface
    // Supabase types infer FKs as array, but runtime returns single object for many-to-one
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const commentHistoryItems = (comments || []).map((comment: any) => {
      const target = Array.isArray(comment.targets) ? comment.targets[0] : comment.targets;
      const obj = Array.isArray(comment.objects) ? comment.objects[0] : comment.objects;
      return {
        id: comment.id,
        content: comment.content,
        created_at: comment.created_at,
        edited_at: comment.edited_at,
        target_db_id: target?.id ?? null,
        target_display_id: target?.target_id ?? null,
        object_db_id: obj?.id ?? null,
        object_display_id: obj?.object_id ?? null,
      };
    });

    const total = totalCount || 0;
    const hasMore = offset + commentHistoryItems.length < total;

    return NextResponse.json({
      comments: commentHistoryItems,
      total_count: total,
      page,
      limit,
      has_more: hasMore,
    });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to fetch comments' }, { status: 500 });
  }
}
