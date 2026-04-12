'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { createClient } from '@/lib/supabase/client';
import { useAuth } from '@/lib/contexts/AuthContext';
import type { CommentWithUser } from '@/lib/types';
import { MessageSquare, Send } from 'lucide-react';

interface ObjectCommentsProps {
  objectDbId: number;
}

export const ObjectComments: React.FC<ObjectCommentsProps> = ({ objectDbId }) => {
  const { user, userProfile } = useAuth();
  const supabase = createClient();
  const canEdit = user && userProfile?.can_comment;

  const [comments, setComments] = useState<CommentWithUser[]>([]);
  const [newComment, setNewComment] = useState('');
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchComments = useCallback(async () => {
    if (!user) {
      setComments([]);
      setLoading(false);
      return;
    }

    try {
      setLoading(true);
      const { data, error: fetchError } = await supabase
        .from('comments')
        .select('*')
        .eq('object_id', objectDbId)
        .is('target_id', null)
        .eq('is_deleted', false)
        .order('created_at', { ascending: true });

      if (fetchError) throw fetchError;

      if (!data || data.length === 0) {
        setComments([]);
        return;
      }

      const userIds = [...new Set(data.map(c => c.user_id))];
      const { data: profiles } = await supabase
        .from('user_profiles')
        .select('*')
        .in('user_id', userIds);

      setComments(data.map(c => ({
        ...c,
        user_profile: profiles?.find(p => p.user_id === c.user_id) || null,
      })));
    } catch (err) {
      console.error('Error fetching object comments:', err);
      setError('Failed to load comments');
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
    setError(null);

    try {
      const { error: insertError } = await supabase
        .from('comments')
        .insert({
          object_id: objectDbId,
          target_id: null,
          user_id: user.id,
          content: newComment.trim(),
        });

      if (insertError) throw insertError;

      setNewComment('');
      await fetchComments();
    } catch (err) {
      console.error('Error posting object comment:', err);
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
      <div className="flex items-center gap-2 mb-4">
        <MessageSquare className="w-5 h-5 text-primary" />
        <h4 className="font-medium text-text-primary dark:text-slate-100">
          Object Discussion ({comments.length})
        </h4>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg">
          <p className="text-sm text-red-800 dark:text-red-400">{error}</p>
        </div>
      )}

      {canEdit && (
        <form onSubmit={handleSubmit} className="mb-4">
          <textarea
            value={newComment}
            onChange={e => setNewComment(e.target.value)}
            placeholder="Add a comment about this object..."
            className="w-full px-4 py-2 border border-border dark:border-slate-600 rounded-lg bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary resize-none text-sm"
            rows={2}
            disabled={submitting}
          />
          <div className="mt-2 flex justify-end">
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
        <div className="mb-4 p-4 bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg text-center">
          <p className="text-sm text-text-secondary dark:text-slate-400">Sign in to add comments</p>
        </div>
      )}

      {loading ? (
        <div className="text-center py-4 text-text-secondary dark:text-slate-400 text-sm">
          Loading comments...
        </div>
      ) : comments.length === 0 ? (
        <div className="text-center py-4 text-text-secondary dark:text-slate-400 text-sm">
          No comments yet
        </div>
      ) : (
        <div className="space-y-3">
          {comments.map(comment => (
            <div
              key={comment.id}
              className="p-3 bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg"
            >
              <div className="flex items-start justify-between mb-1">
                <span className="font-medium text-text-primary dark:text-slate-100 text-sm">
                  {comment.user_profile?.full_name || 'Unknown User'}
                </span>
                <span className="text-xs text-text-secondary dark:text-slate-500">
                  {formatDate(comment.created_at)}
                </span>
              </div>
              <p className="text-sm text-text-primary dark:text-slate-100 whitespace-pre-wrap">
                {comment.content}
              </p>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
};
