'use client';

import React, { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { FilterChip, FilterOption } from '@/components/ui/FilterChip';
import { Loader2, RefreshCw, MessageSquare, Edit3, ChevronLeft, ChevronRight, X } from 'lucide-react';
import type { ActivityFeedResponse, ActivityUser } from '@/lib/types';
import { formatActivityField, formatFieldName } from '@/lib/types';

// Activity type filter options
const TYPE_OPTIONS: FilterOption[] = [
  { value: 'comment', label: 'Comment' },
  { value: 'inspection', label: 'Inspection' },
];

// Inspection field type filter options
const FIELD_OPTIONS: FilterOption[] = [
  { value: 'redshift_quality', label: 'Redshift Quality' },
  { value: 'redshift_inspected', label: 'Redshift (Manual)' },
  { value: 'spectral_features', label: 'Spectral Features' },
  { value: 'object_flags', label: 'Object Flags' },
  { value: 'dq_flags', label: 'DQ Flags' },
];

export default function AdminActivityPage() {
  const [data, setData] = useState<ActivityFeedResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);

  // Filter state
  const [typeFilter, setTypeFilter] = useState<string[]>([]);
  const [userFilter, setUserFilter] = useState<string[]>([]);
  const [fieldFilter, setFieldFilter] = useState<string[]>([]);

  // Build user filter options from API response
  const userOptions: FilterOption[] = (data?.available_users || []).map((user: ActivityUser) => ({
    value: user.user_id,
    label: user.full_name,
  }));

  const fetchActivity = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      // Build query params with filters
      const params = new URLSearchParams({
        page: page.toString(),
        page_size: '50',
      });

      if (typeFilter.length > 0) {
        params.set('type', typeFilter.join(','));
      }
      if (userFilter.length > 0) {
        params.set('user_id', userFilter.join(','));
      }
      if (fieldFilter.length > 0) {
        params.set('field_name', fieldFilter.join(','));
      }

      const response = await fetch(`/api/admin/activity?${params.toString()}`);
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
  }, [page, typeFilter, userFilter, fieldFilter]);

  useEffect(() => {
    fetchActivity();
  }, [fetchActivity]);

  // Reset to page 1 when filters change
  const handleTypeFilterChange = (selected: (string | number)[]) => {
    setTypeFilter(selected as string[]);
    setPage(1);
  };

  const handleUserFilterChange = (selected: (string | number)[]) => {
    setUserFilter(selected as string[]);
    setPage(1);
  };

  const handleFieldFilterChange = (selected: (string | number)[]) => {
    setFieldFilter(selected as string[]);
    setPage(1);
  };

  // Check if any filters are active
  const hasActiveFilters = typeFilter.length > 0 || userFilter.length > 0 || fieldFilter.length > 0;

  const handleClearAll = () => {
    setTypeFilter([]);
    setUserFilter([]);
    setFieldFilter([]);
    setPage(1);
  };

  // Field filter is disabled when only comments are selected
  const isFieldFilterDisabled = typeFilter.length === 1 && typeFilter[0] === 'comment';

  const formatTimestamp = (timestamp: string) => {
    // Database timestamps are UTC without timezone indicator
    // Append 'Z' to ensure proper UTC parsing
    const date = new Date(timestamp.endsWith('Z') ? timestamp : `${timestamp}Z`);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMins / 60);
    const diffDays = Math.floor(diffHours / 24);

    if (diffMins < 1) {
      return 'just now';
    } else if (diffMins < 60) {
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
          <span className="ml-3 text-text-secondary dark:text-slate-400">Loading activity...</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-8">
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4">
          <p className="text-red-800 dark:text-red-400">{error}</p>
        </div>
      </div>
    );
  }

  const activities = data?.activities || [];

  return (
    <div className="p-8">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold text-text-primary dark:text-slate-100">User Activity</h1>
          <p className="text-sm text-text-secondary dark:text-slate-400 mt-1">
            Recent comments and inspection changes from users
          </p>
        </div>
        <Button variant="secondary" size="sm" onClick={fetchActivity}>
          <RefreshCw className="w-4 h-4 mr-2" />
          Refresh
        </Button>
      </div>

      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <FilterChip
          label="Type"
          options={TYPE_OPTIONS}
          selected={typeFilter}
          onChange={handleTypeFilterChange}
        />

        <FilterChip
          label="User"
          options={userOptions}
          selected={userFilter}
          onChange={handleUserFilterChange}
          disabled={userOptions.length === 0}
        />

        <FilterChip
          label="Field"
          options={FIELD_OPTIONS}
          selected={fieldFilter}
          onChange={handleFieldFilterChange}
          disabled={isFieldFilterDisabled}
        />

        {/* Clear all button */}
        {hasActiveFilters && (
          <>
            <div className="h-6 w-px bg-border dark:bg-slate-700 mx-1" />
            <button
              onClick={handleClearAll}
              className="inline-flex items-center gap-1 px-3 py-1.5 text-sm text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 transition-colors"
            >
              <X className="w-3.5 h-3.5" />
              Clear all
            </button>
          </>
        )}
      </div>

      {activities.length === 0 ? (
        <Card className="p-8 text-center">
          <p className="text-text-secondary dark:text-slate-400">No activity yet</p>
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <table className="w-full">
            <thead className="bg-card dark:bg-slate-800 border-b border-border dark:border-slate-700">
              <tr>
                <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider">
                  Type
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider">
                  User
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider">
                  Object
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider">
                  Details
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider">
                  Time
                </th>
              </tr>
            </thead>
            <tbody className="bg-white dark:bg-slate-800 divide-y divide-border dark:divide-slate-700">
              {activities.map((activity) => (
                <tr key={activity.id} className="hover:bg-gray-50 dark:hover:bg-slate-700">
                  <td className="px-6 py-4 whitespace-nowrap">
                    <div className="flex items-center gap-2">
                      {activity.type === 'comment' ? (
                        <>
                          <MessageSquare className="w-4 h-4 text-blue-600 dark:text-blue-400" />
                          <span className="text-sm text-text-primary dark:text-slate-100">Comment</span>
                        </>
                      ) : (
                        <>
                          <Edit3 className="w-4 h-4 text-green-600 dark:text-green-400" />
                          <span className="text-sm text-text-primary dark:text-slate-100">Inspection</span>
                        </>
                      )}
                    </div>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap">
                    <div className="text-sm text-text-primary dark:text-slate-100">
                      {activity.user_profile?.full_name || 'Unknown User'}
                    </div>
                    {activity.user_profile?.is_group_account && (
                      <span className="text-xs px-1.5 py-0.5 bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-300 rounded">
                        Group
                      </span>
                    )}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap">
                    <Link
                      href={`/nirspec/targets/${activity.target_display_id}`}
                      className="text-sm font-mono text-primary hover:underline"
                    >
                      {activity.target_display_id}
                    </Link>
                  </td>
                  <td className="px-6 py-4">
                    {activity.type === 'comment' ? (
                      <div className="text-sm text-text-secondary dark:text-slate-400 max-w-md">
                        <span className="line-clamp-2">{activity.content}</span>
                        {activity.edited_at && (
                          <span className="text-xs italic ml-1">(edited)</span>
                        )}
                      </div>
                    ) : (
                      <div className="text-sm">
                        <span className="text-text-secondary dark:text-slate-400">
                          {formatFieldName(activity.field_name)}:
                        </span>
                        <span className="ml-2 text-text-primary dark:text-slate-100">
                          {formatActivityField(activity.field_name, activity.old_value)}
                          {' → '}
                          {formatActivityField(activity.field_name, activity.new_value)}
                        </span>
                      </div>
                    )}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm text-text-secondary dark:text-slate-400">
                    {formatTimestamp(activity.timestamp)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {/* Pagination */}
          {data && data.total_count > data.page_size && (
            <div className="px-6 py-4 border-t border-border dark:border-slate-700 flex items-center justify-between">
              <div className="text-sm text-text-secondary dark:text-slate-400">
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
