'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { MessageSquare, Loader2 } from 'lucide-react';
import { Card } from '@/components/ui/Card';
import { CommentHistoryItem } from '@/lib/types';

interface CommentHistoryProps {
  initialComments: CommentHistoryItem[];
  totalCount: number;
}

/**
 * Format a date for display (e.g., "Jan 15")
 */
function formatDate(dateString: string): string {
  const date = new Date(dateString);
  return date.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
  });
}

export const CommentHistory: React.FC<CommentHistoryProps> = ({
  initialComments,
  totalCount,
}) => {
  const [comments, setComments] = useState<CommentHistoryItem[]>(initialComments);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);

  // We start with 5 comments from the initial load
  const hasMore = comments.length < totalCount;

  const loadMore = async () => {
    if (loading || !hasMore) return;

    setLoading(true);
    try {
      // Calculate which page to fetch (page 1 = items 1-10, but we already have 5)
      const nextPage = page + 1;
      const response = await fetch(`/api/profile/comments?page=${nextPage}&limit=10`);

      if (!response.ok) {
        throw new Error('Failed to fetch comments');
      }

      const data = await response.json();

      // Append new comments, avoiding duplicates
      const existingIds = new Set(comments.map(c => c.id));
      const newComments = data.comments.filter(
        (c: CommentHistoryItem) => !existingIds.has(c.id)
      );

      setComments(prev => [...prev, ...newComments]);
      setPage(nextPage);
    } catch (error) {
      console.error('Error loading more comments:', error);
    } finally {
      setLoading(false);
    }
  };

  return (
    <Card className="p-6">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 bg-blue-100 dark:bg-blue-900 rounded-full flex items-center justify-center">
            <MessageSquare className="w-5 h-5 text-blue-600 dark:text-blue-400" />
          </div>
          <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100">
            Your Comments
          </h2>
        </div>
        <span className="text-sm text-text-secondary dark:text-slate-400">
          {totalCount} total
        </span>
      </div>

      {comments.length === 0 ? (
        <p className="text-text-secondary dark:text-slate-400 text-sm py-4">
          No comments yet. Comments you leave on objects will appear here.
        </p>
      ) : (
        <div className="space-y-3">
          {comments.map(comment => (
            <Link
              key={comment.id}
              href={`/nirspec/targets/${comment.target_display_id}`}
              className="block p-3 rounded-lg border border-border dark:border-slate-700
                         hover:bg-card-hover dark:hover:bg-slate-700 transition-colors"
            >
              <div className="flex items-start justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <p className="text-sm text-text-primary dark:text-slate-100 line-clamp-2">
                    {comment.content}
                  </p>
                  <p className="mt-1 text-xs text-text-secondary dark:text-slate-400">
                    on <span className="font-mono">{comment.target_display_id}</span>
                    {comment.edited_at && ' (edited)'}
                  </p>
                </div>
                <span className="text-xs text-text-secondary dark:text-slate-400 whitespace-nowrap">
                  {formatDate(comment.created_at)}
                </span>
              </div>
            </Link>
          ))}
        </div>
      )}

      {hasMore && (
        <button
          onClick={loadMore}
          disabled={loading}
          className="mt-4 w-full py-2 text-sm text-primary hover:underline
                     disabled:opacity-50 disabled:cursor-not-allowed
                     flex items-center justify-center gap-2"
        >
          {loading ? (
            <>
              <Loader2 className="w-4 h-4 animate-spin" />
              Loading...
            </>
          ) : (
            'Load more comments'
          )}
        </button>
      )}
    </Card>
  );
};
