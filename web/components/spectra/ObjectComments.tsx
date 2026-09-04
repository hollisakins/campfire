'use client';

import React, { useState, useMemo, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { createClient } from '@/lib/supabase/client';
import { useAuth } from '@/lib/contexts/AuthContext';
import { useInView } from '@/lib/hooks/useInView';
import type { CommentWithUser } from '@/lib/types';
import { MessageSquare, Send } from 'lucide-react';

interface MemberTarget {
  id: number;
  target_id: string;
}

interface AggregatedComment extends CommentWithUser {
  /** Display ID of the target this comment belongs to, or null for object-level */
  source_target_display_id: string | null;
}

interface ObjectCommentsProps {
  objectDbId: number;
  memberTargets: MemberTarget[];
}

export const ObjectComments: React.FC<ObjectCommentsProps> = ({ objectDbId, memberTargets }) => {
  const { user, userProfile } = useAuth();
  const supabase = createClient();
  const queryClient = useQueryClient();
  const canEdit = user && userProfile?.can_comment;

  const [newComment, setNewComment] = useState('');
  const [selectedTargetId, setSelectedTargetId] = useState<string>(
    memberTargets.length === 1 ? String(memberTargets[0].id) : ''
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isSingleton = memberTargets.length === 1;

  // Lookup map: target DB id -> display ID
  const targetDisplayMap = useMemo(() => {
    const map: Record<number, string> = {};
    for (const m of memberTargets) {
      map[m.id] = m.target_id;
    }
    return map;
  }, [memberTargets]);

  const memberDbIds = useMemo(() => memberTargets.map(m => m.id), [memberTargets]);

  // The comment read was prod's single most expensive query shape: 346
  // comments exist across 42 k objects, yet every object view ran the
  // target-scoped read (230 ms under RLS) ~3× — an unconditional mount
  // effect re-firing as `user` churned during auth boot. Now (#499):
  //   * one query (object-level OR member-target-level) instead of two;
  //   * fetched only once the Discussion card is about to scroll into view;
  //   * held in the TanStack cache keyed on the object id, so boot-time
  //     identity changes and back-navigation don't re-issue it.
  const containerRef = useRef<HTMLDivElement>(null);
  const inView = useInView(containerRef);

  const commentsQuery = useQuery<AggregatedComment[]>({
    queryKey: ['object-comments', objectDbId, memberDbIds],
    enabled: !!user && inView,
    queryFn: async () => {
      const scope = memberDbIds.length > 0
        ? `and(object_id.eq.${objectDbId},target_id.is.null),target_id.in.(${memberDbIds.join(',')})`
        : `and(object_id.eq.${objectDbId},target_id.is.null)`;
      const { data: rows, error: readError } = await supabase
        .from('comments')
        .select('*')
        .or(scope)
        .eq('is_deleted', false)
        .order('created_at', { ascending: true });
      if (readError) throw readError;
      const allRaw = rows ?? [];
      if (allRaw.length === 0) return [];

      // Fetch user profiles
      const userIds = [...new Set(allRaw.map(c => c.user_id))];
      const { data: profiles } = await supabase
        .from('user_profiles')
        .select('*')
        .in('user_id', userIds);

      // Annotate with source target display ID (rows are already chronological)
      return allRaw.map(c => ({
        ...c,
        user_profile: profiles?.find(p => p.user_id === c.user_id) || null,
        source_target_display_id: c.target_id ? (targetDisplayMap[c.target_id] || null) : null,
      }));
    },
  });

  const comments = commentsQuery.data ?? [];
  // "Loading" only while a fetch is genuinely pending for a signed-in viewer;
  // before the card scrolls into view it shows the (accurate) empty state.
  const loading = !!user && inView && commentsQuery.isPending;
  const loadError = commentsQuery.isError ? 'Failed to load comments' : null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!user || !newComment.trim()) return;

    setSubmitting(true);
    setError(null);

    try {
      const targetId = selectedTargetId ? Number(selectedTargetId) : null;

      const { error: insertError } = await supabase
        .from('comments')
        .insert({
          target_id: targetId,
          object_id: targetId ? null : objectDbId,
          user_id: user.id,
          content: newComment.trim(),
        });

      if (insertError) throw insertError;

      setNewComment('');
      await queryClient.invalidateQueries({ queryKey: ['object-comments', objectDbId] });
    } catch (err) {
      console.error('Error posting comment:', err);
      setError('Failed to post comment');
    } finally {
      setSubmitting(false);
    }
  };

  const formatDate = (dateString: string) =>
    new Date(dateString).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });

  return (
    <Card className="p-6">
      <div ref={containerRef} className="flex items-center gap-2 mb-4">
        <MessageSquare className="w-5 h-5 text-primary" />
        <h4 className="font-medium text-text-primary">
          Discussion ({comments.length})
        </h4>
      </div>

      {(error || loadError) && (
        <div className="mb-4 p-3 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg">
          <p className="text-sm text-red-800 dark:text-red-400">{error || loadError}</p>
        </div>
      )}

      {canEdit && (
        <form onSubmit={handleSubmit} className="mb-4">
          <textarea
            value={newComment}
            onChange={e => setNewComment(e.target.value)}
            placeholder="Add a comment..."
            className="w-full px-4 py-2 border border-border dark:border-border-strong rounded-lg bg-background dark:bg-card-hover text-text-primary placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary resize-none text-sm"
            rows={2}
            disabled={submitting}
          />
          <div className="mt-2 flex items-center justify-end gap-2">
            {!isSingleton && (
              <select
                value={selectedTargetId}
                onChange={e => setSelectedTargetId(e.target.value)}
                className="px-3 py-1.5 border border-border dark:border-border-strong rounded-lg bg-background dark:bg-card-hover text-text-primary text-xs font-mono focus:outline-none focus:ring-2 focus:ring-primary"
                disabled={submitting}
              >
                <option value="">General</option>
                {memberTargets.map(m => (
                  <option key={m.id} value={String(m.id)}>
                    {m.target_id}
                  </option>
                ))}
              </select>
            )}
            <Button
              type="submit"
              variant="secondary"
              size="sm"
              disabled={submitting || !newComment.trim()}
            >
              <Send className="w-4 h-4 mr-1" />
              {submitting ? 'Posting...' : 'Post'}
            </Button>
          </div>
        </form>
      )}

      {!user && (
        <div className="mb-4 p-4 bg-card border border-border rounded-lg text-center">
          <p className="text-sm text-text-secondary">Sign in to add comments</p>
        </div>
      )}

      {loading ? (
        <div className="text-center py-4 text-text-secondary text-sm">
          Loading comments...
        </div>
      ) : comments.length === 0 ? (
        <div className="text-center py-4 text-text-secondary text-sm">
          No comments yet
        </div>
      ) : (
        <div className="space-y-3">
          {comments.map(comment => (
            <div
              key={comment.id}
              className="p-3 bg-card border border-border rounded-lg"
            >
              <div className="flex items-start justify-between mb-1">
                <div className="flex items-baseline gap-2">
                  <span className="font-medium text-text-primary text-sm">
                    {comment.user_profile?.full_name || 'Unknown User'}
                  </span>
                  {comment.source_target_display_id && (
                    <span className="text-xs font-mono text-text-secondary">
                      ({comment.source_target_display_id})
                    </span>
                  )}
                </div>
                <span className="text-xs text-text-secondary dark:text-text-tertiary">
                  {formatDate(comment.created_at)}
                </span>
              </div>
              <p className="text-sm text-text-primary whitespace-pre-wrap">
                {comment.content}
              </p>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
};
