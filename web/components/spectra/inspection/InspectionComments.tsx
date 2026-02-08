'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { MessageSquare, Send, ChevronDown, ChevronUp } from 'lucide-react';
import { createClient } from '@/lib/supabase/client';
import { useAuth } from '@/lib/contexts/AuthContext';
import type { CommentWithUser } from '@/lib/types';

interface InspectionCommentsProps {
  objectDbId: number;
}

export const InspectionComments: React.FC<InspectionCommentsProps> = ({ objectDbId }) => {
  const { user, userProfile } = useAuth();
  const supabase = createClient();
  const canEdit = user && userProfile?.can_comment;

  const [expanded, setExpanded] = useState(false);
  const [comments, setComments] = useState<CommentWithUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [newComment, setNewComment] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const fetchComments = useCallback(async () => {
    if (!user) {
      setComments([]);
      setLoading(false);
      return;
    }
    try {
      setLoading(true);
      const { data: commentsData, error } = await supabase
        .from('comments')
        .select('*')
        .eq('object_id', objectDbId)
        .eq('is_deleted', false)
        .order('created_at', { ascending: true });

      if (error) throw error;
      if (!commentsData || commentsData.length === 0) {
        setComments([]);
        return;
      }

      const userIds = [...new Set(commentsData.map((c) => c.user_id))];
      const { data: profilesData } = await supabase
        .from('user_profiles')
        .select('*')
        .in('user_id', userIds);

      setComments(
        commentsData.map((comment) => ({
          ...comment,
          user_profile: profilesData?.find((p) => p.user_id === comment.user_id) || null,
        }))
      );
    } catch {
      // silently fail
    } finally {
      setLoading(false);
    }
  }, [objectDbId, user, supabase]);

  useEffect(() => {
    fetchComments();
  }, [fetchComments]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!user || !newComment.trim()) return;
    setSubmitting(true);
    try {
      const { error } = await supabase.from('comments').insert({
        object_id: objectDbId,
        user_id: user.id,
        content: newComment.trim(),
      });
      if (error) throw error;
      setNewComment('');
      await fetchComments();
    } catch {
      // silently fail
    } finally {
      setSubmitting(false);
    }
  };

  const formatDate = (dateString: string) =>
    new Date(dateString).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });

  return (
    <div className="border-t border-border dark:border-slate-700 flex-shrink-0 bg-background dark:bg-slate-900">
      {/* Collapsed bar */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-4 py-2 hover:bg-card dark:hover:bg-slate-800 transition-colors"
      >
        <div className="flex items-center gap-2 text-sm text-text-secondary dark:text-slate-400">
          <MessageSquare className="w-4 h-4" />
          <span>{loading ? 'Loading...' : `${comments.length} comment${comments.length !== 1 ? 's' : ''}`}</span>
        </div>
        {expanded ? <ChevronDown className="w-4 h-4 text-text-secondary dark:text-slate-400" /> : <ChevronUp className="w-4 h-4 text-text-secondary dark:text-slate-400" />}
      </button>

      {/* Expanded content */}
      {expanded && (
        <div className="px-4 pb-3 max-h-48 overflow-y-auto">
          {/* Comment form */}
          {canEdit && (
            <form onSubmit={handleSubmit} className="flex gap-2 mb-2">
              <input
                type="text"
                value={newComment}
                onChange={(e) => setNewComment(e.target.value)}
                placeholder="Add a comment..."
                className="flex-1 px-3 py-1.5 text-sm border border-border dark:border-slate-600 rounded bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-1 focus:ring-primary"
                disabled={submitting}
              />
              <button
                type="submit"
                disabled={submitting || !newComment.trim()}
                className="px-2.5 py-1.5 rounded bg-primary hover:bg-primary-hover text-white text-sm disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                <Send className="w-3.5 h-3.5" />
              </button>
            </form>
          )}

          {/* Comments list */}
          {comments.length === 0 ? (
            <p className="text-xs text-text-secondary dark:text-slate-400 text-center py-2">No comments yet</p>
          ) : (
            <div className="space-y-1.5">
              {comments.map((comment) => (
                <div key={comment.id} className="text-xs">
                  <span className="font-medium text-text-primary dark:text-slate-100">
                    {comment.user_profile?.full_name || 'Unknown'}
                  </span>
                  <span className="text-text-secondary dark:text-slate-500 mx-1">{formatDate(comment.created_at)}</span>
                  <p className="text-text-primary dark:text-slate-100 whitespace-pre-wrap">{comment.content}</p>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};
