'use client';

import React, { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Loader2, RefreshCw, MessageSquare, Edit3, ChevronLeft, ChevronRight } from 'lucide-react';
import type { ActivityFeedResponse } from '@/lib/types';
import { formatActivityField, formatFieldName } from '@/lib/types';

export default function AdminActivityPage() {
  const [data, setData] = useState<ActivityFeedResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);

  const fetchActivity = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const response = await fetch(`/api/admin/activity?page=${page}&page_size=50`);
      const result = await response.json();

      if (!response.ok) {
        throw new Error(result.error || 'Failed to fetch activity');
      }

      setData(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch activity');
    } finally {
      setLoading(false);
    }
  }, [page]);

  useEffect(() => {
    fetchActivity();
  }, [fetchActivity]);

  const formatTimestamp = (timestamp: string) => {
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMins / 60);
    const diffDays = Math.floor(diffHours / 24);

    if (diffMins < 60) {
      return `${diffMins} minute${diffMins !== 1 ? 's' : ''} ago`;
    } else if (diffHours < 24) {
      return `${diffHours} hour${diffHours !== 1 ? 's' : ''} ago`;
    } else if (diffDays < 7) {
      return `${diffDays} day${diffDays !== 1 ? 's' : ''} ago`;
    } else {
      return date.toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
        year: date.getFullYear() !== now.getFullYear() ? 'numeric' : undefined,
      });
    }
  };

  if (loading) {
    return (
      <div className="p-8">
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
          <span className="ml-3 text-text-secondary">Loading activity...</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-8">
        <div className="bg-red-50 border border-red-200 rounded-lg p-4">
          <p className="text-red-800">{error}</p>
        </div>
      </div>
    );
  }

  const activities = data?.activities || [];

  return (
    <div className="p-8">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold text-text-primary">User Activity</h1>
          <p className="text-sm text-text-secondary mt-1">
            Recent comments and inspection changes from users
          </p>
        </div>
        <Button variant="secondary" size="sm" onClick={fetchActivity}>
          <RefreshCw className="w-4 h-4 mr-2" />
          Refresh
        </Button>
      </div>

      {activities.length === 0 ? (
        <Card className="p-8 text-center">
          <p className="text-text-secondary">No activity yet</p>
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <table className="w-full">
            <thead className="bg-card border-b border-border">
              <tr>
                <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider">
                  Type
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider">
                  User
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider">
                  Object
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider">
                  Details
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider">
                  Time
                </th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-border">
              {activities.map((activity) => (
                <tr key={activity.id} className="hover:bg-gray-50">
                  <td className="px-6 py-4 whitespace-nowrap">
                    <div className="flex items-center gap-2">
                      {activity.type === 'comment' ? (
                        <>
                          <MessageSquare className="w-4 h-4 text-blue-600" />
                          <span className="text-sm text-text-primary">Comment</span>
                        </>
                      ) : (
                        <>
                          <Edit3 className="w-4 h-4 text-green-600" />
                          <span className="text-sm text-text-primary">Inspection</span>
                        </>
                      )}
                    </div>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap">
                    <div className="text-sm text-text-primary">
                      {activity.user_profile?.full_name || 'Unknown User'}
                    </div>
                    {activity.user_profile?.is_group_account && (
                      <span className="text-xs px-1.5 py-0.5 bg-blue-100 text-blue-800 rounded">
                        Group
                      </span>
                    )}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap">
                    <Link
                      href={`/spectra/${activity.object_display_id}`}
                      className="text-sm font-mono text-primary hover:underline"
                    >
                      {activity.object_display_id}
                    </Link>
                  </td>
                  <td className="px-6 py-4">
                    {activity.type === 'comment' ? (
                      <div className="text-sm text-text-secondary max-w-md">
                        <span className="line-clamp-2">{activity.content}</span>
                        {activity.edited_at && (
                          <span className="text-xs italic ml-1">(edited)</span>
                        )}
                      </div>
                    ) : (
                      <div className="text-sm">
                        <span className="text-text-secondary">
                          {formatFieldName(activity.field_name)}:
                        </span>
                        <span className="ml-2 text-text-primary">
                          {formatActivityField(activity.field_name, activity.old_value)}
                          {' → '}
                          {formatActivityField(activity.field_name, activity.new_value)}
                        </span>
                      </div>
                    )}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm text-text-secondary">
                    {formatTimestamp(activity.timestamp)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {/* Pagination */}
          {data && data.total_count > data.page_size && (
            <div className="px-6 py-4 border-t border-border flex items-center justify-between">
              <div className="text-sm text-text-secondary">
                Showing {(page - 1) * data.page_size + 1} to{' '}
                {Math.min(page * data.page_size, data.total_count)} of {data.total_count} activities
              </div>
              <div className="flex gap-2">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => setPage(p => p - 1)}
                  disabled={page === 1}
                >
                  <ChevronLeft className="w-4 h-4 mr-1" />
                  Previous
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => setPage(p => p + 1)}
                  disabled={!data.has_next_page}
                >
                  Next
                  <ChevronRight className="w-4 h-4 ml-1" />
                </Button>
              </div>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
