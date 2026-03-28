'use client';

import React, { useState, useEffect } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { createClient } from '@/lib/supabase/client';
import { useAuth } from '@/lib/contexts/AuthContext';
import { CommentWithUser } from '@/lib/types';
import { MessageSquare, Send } from 'lucide-react';

interface CommentsProps {
  targetId: number;
}

export const Comments: React.FC<CommentsProps> = ({ targetId }) => {
  const { user, userProfile } = useAuth();
  const supabase = createClient();

  const [comments, setComments] = useState<CommentWithUser[]>([]);
  const [newComment, setNewComment] = useState('');
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchComments();
  }, [targetId]);

  const fetchComments = async () => {
    try {
      setLoading(true);
      setError(null);

      // Check if user is authenticated
      if (!user) {
        console.log('User not authenticated, skipping comment fetch');
        setComments([]);
        setLoading(false);
        return;
      }

      console.log('Fetching comments for target_id:', targetId);

      // Fetch comments
      const { data: commentsData, error: commentsError } = await supabase
        .from('comments')
        .select('*')
        .eq('target_id', targetId)
        .eq('is_deleted', false)
        .order('created_at', { ascending: true });

      if (commentsError) {
        console.error('Supabase error details:', {
          message: commentsError.message,
          details: commentsError.details,
          hint: commentsError.hint,
          code: commentsError.code,
        });
        throw commentsError;
      }

      if (!commentsData || commentsData.length === 0) {
        console.log('No comments found');
        setComments([]);
        setLoading(false);
        return;
      }

      // Fetch user profiles for all commenters
      const userIds = [...new Set(commentsData.map(c => c.user_id))];
      const { data: profilesData, error: profilesError } = await supabase
        .from('user_profiles')
        .select('*')
        .in('user_id', userIds);

      if (profilesError) {
        console.error('Error fetching user profiles:', profilesError);
        // Continue without profiles rather than failing completely
      }

      // Combine comments with user profiles
      const commentsWithProfiles = commentsData.map(comment => ({
        ...comment,
        user_profile: profilesData?.find(p => p.user_id === comment.user_id) || null,
      }));

      console.log('Comments fetched successfully:', commentsWithProfiles.length);
      setComments(commentsWithProfiles);
    } catch (err: unknown) {
      console.error('Error fetching comments:', err);
      const error = err as { message?: string; details?: string };
      console.error('Error message:', error?.message);
      console.error('Error details:', error?.details);

      // Provide helpful error message
      if (error && 'code' in error && (error as { code?: string }).code === 'PGRST116') {
        setError('This appears to be mock data. Comments are only available for objects in the database.');
      } else {
        setError(error?.message || 'Failed to load comments. You may not have access to this object.');
      }
    } finally {
      setLoading(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!user || !userProfile) {
      setError('You must be signed in to comment');
      return;
    }

    if (!newComment.trim()) return;

    if (!userProfile.can_comment) {
      setError('You do not have permission to comment');
      return;
    }

    try {
      setSubmitting(true);
      setError(null);

      const { error } = await supabase
        .from('comments')
        .insert({
          target_id: targetId,
          user_id: user.id,
          content: newComment.trim(),
        });

      if (error) throw error;

      // Clear input and refresh comments
      setNewComment('');
      await fetchComments();
    } catch (err) {
      console.error('Error posting comment:', err);
      setError('Failed to post comment');
    } finally {
      setSubmitting(false);
    }
  };

  const formatDate = (dateString: string) => {
    const date = new Date(dateString);
    return date.toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  return (
    <Card className="p-6">
      <div className="flex items-center gap-2 mb-6">
        <MessageSquare className="w-5 h-5 text-primary" />
        <h3 className="text-lg font-semibold text-text-primary dark:text-slate-100">
          Comments ({comments.length})
        </h3>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg">
          <p className="text-sm text-red-800 dark:text-red-400">{error}</p>
        </div>
      )}

      {/* Comment Form */}
      {user && userProfile?.can_comment && (
        <form onSubmit={handleSubmit} className="mb-6">
          <textarea
            value={newComment}
            onChange={(e) => setNewComment(e.target.value)}
            placeholder="Add a comment..."
            className="w-full px-4 py-2 border border-border dark:border-slate-700 rounded-lg bg-white dark:bg-slate-700 text-text-primary dark:text-slate-100 placeholder:text-text-secondary dark:placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-primary resize-none"
            rows={3}
            disabled={submitting}
          />
          <div className="mt-2 flex justify-end">
            <Button
              type="submit"
              variant="primary"
              size="sm"
              disabled={submitting || !newComment.trim()}
              className="flex items-center gap-2"
            >
              <Send className="w-4 h-4" />
              {submitting ? 'Posting...' : 'Post Comment'}
            </Button>
          </div>
        </form>
      )}

      {!user && (
        <div className="mb-6 p-4 bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg text-center">
          <p className="text-sm text-text-secondary dark:text-slate-400">
            Sign in to add comments
          </p>
        </div>
      )}

      {/* Comments List */}
      {loading ? (
        <div className="text-center py-8 text-text-secondary dark:text-slate-400">
          Loading comments...
        </div>
      ) : comments.length === 0 ? (
        <div className="text-center py-8 text-text-secondary dark:text-slate-400">
          No comments yet. Be the first to comment!
        </div>
      ) : (
        <div className="space-y-4">
          {comments.map((comment) => (
            <div
              key={comment.id}
              className="p-4 bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg"
            >
              <div className="flex items-start justify-between mb-2">
                <div className="flex items-center gap-2">
                  <span className="font-semibold text-text-primary dark:text-slate-100 text-sm">
                    {comment.user_profile?.full_name || 'Unknown User'}
                  </span>
                  {comment.user_profile?.is_group_account && (
                    <span className="text-xs px-2 py-0.5 bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-300 rounded">
                      Group
                    </span>
                  )}
                </div>
                <span className="text-xs text-text-secondary dark:text-slate-500">
                  {formatDate(comment.created_at)}
                  {comment.edited_at && ' (edited)'}
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