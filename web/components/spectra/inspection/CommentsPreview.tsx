'use client';

import React, { useEffect, useState } from 'react';
import { MessageSquare } from 'lucide-react';
import { createClient } from '@/lib/supabase/client';
import { useAuth } from '@/lib/contexts/AuthContext';
import type { CommentWithUser } from '@/lib/types';

interface CommentsPreviewProps {
  targetDbId: number;
  commentCount: number;
}

export const CommentsPreview: React.FC<CommentsPreviewProps> = ({ targetDbId, commentCount }) => {
  const { user } = useAuth();
  const supabase = createClient();
  const [comments, setComments] = useState<CommentWithUser[]>([]);

  // Fetch latest 2 comments only
  useEffect(() => {
    if (commentCount === 0 || !user) {
      setComments([]);
      return;
    }

    async function fetchLatestComments() {
      try {
        const { data: commentsData, error } = await supabase
          .from('comments')
          .select('*')
          .eq('target_id', targetDbId)
          .eq('is_deleted', false)
          .order('created_at', { ascending: false })
          .limit(2);

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
      } catch (error) {
        console.warn('[CommentsPreview] Failed to fetch comments:', error);
        setComments([]);
      }
    }

    fetchLatestComments();
  }, [targetDbId, commentCount, user, supabase]);

  // Format relative time
  const formatDistanceToNow = (date: Date) => {
    const seconds = Math.floor((new Date().getTime() - date.getTime()) / 1000);
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h`;
    const days = Math.floor(hours / 24);
    return `${days}d`;
  };

  if (commentCount === 0) {
    return (
      <div className="p-4 border-b border-border dark:border-slate-700">
        <h3 className="text-xs font-semibold text-text-secondary dark:text-slate-400 uppercase flex items-center gap-1">
          <MessageSquare className="w-3 h-3" />
          Comments (0)
        </h3>
        <p className="text-xs text-text-secondary dark:text-slate-500 mt-2">
          No comments yet
        </p>
      </div>
    );
  }

  return (
    <div className="p-4 border-b border-border dark:border-slate-700">
      <h3 className="text-xs font-semibold text-text-secondary dark:text-slate-400 uppercase mb-2 flex items-center gap-1">
        <MessageSquare className="w-3 h-3" />
        Comments ({commentCount})
      </h3>
      <div className="space-y-2 max-h-32 overflow-y-auto">
        {comments.slice(0, 2).reverse().map((comment) => (
          <div key={comment.id} className="text-xs">
            <div className="flex items-center gap-1 text-text-secondary dark:text-slate-400">
              <span className="font-medium">{comment.user_profile?.full_name || 'Anonymous'}</span>
              <span>·</span>
              <span>{formatDistanceToNow(new Date(comment.created_at))} ago</span>
            </div>
            <p className="text-text-primary dark:text-slate-100 mt-0.5 line-clamp-2">
              {comment.content}
            </p>
          </div>
        ))}
      </div>
      {commentCount > 2 && (
        <p className="text-xs text-primary mt-2">
          + {commentCount - 2} more comment{commentCount - 2 !== 1 ? 's' : ''}
        </p>
      )}
    </div>
  );
};
