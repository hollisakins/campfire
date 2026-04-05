'use client';

import React, { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { FilterChip, FilterOption } from '@/components/ui/FilterChip';
import {
  Loader2,
  RefreshCw,
  Download,
  FileArchive,
  FileSpreadsheet,
  FileText,
  BarChart3,
  Users,
  File,
  X,
} from 'lucide-react';
import { createClient } from '@/lib/supabase/client';

interface DownloadStats {
  total_downloads: number;
  unique_users: number;
  by_type: Record<string, number> | null;
  total_files: number;
  total_objects: number;
  recent_downloads: RecentDownload[] | null;
  most_downloaded_objects: MostDownloaded[] | null;
  downloads_by_day: DailyDownloads[] | null;
}

interface RecentDownload {
  id: string;
  download_type: string;
  target_count: number | null;
  file_count: number | null;
  requested_at: string;
  email: string | null;
  full_name: string | null;
}

interface MostDownloaded {
  target_id: string;
  download_count: number;
}

interface DailyDownloads {
  day: string;
  count: number;
}

// Time range filter options
const TIME_RANGE_OPTIONS: FilterOption[] = [
  { value: '7', label: '7 days' },
  { value: '30', label: '30 days' },
  { value: '90', label: '90 days' },
];

// Download type labels
const DOWNLOAD_TYPE_LABELS: Record<string, string> = {
  fits_single: 'Single FITS',
  fits_object: 'Object FITS',
  fits_batch: 'Batch FITS',
  fits_zip: 'ZIP Archive',
  csv: 'CSV Export',
  sed_plot: 'SED Plot',
};

// Download type icons
function DownloadTypeIcon({ type }: { type: string }) {
  switch (type) {
    case 'fits_single':
      return <File className="w-4 h-4 text-blue-600 dark:text-blue-400" />;
    case 'fits_object':
      return <Download className="w-4 h-4 text-cyan-600 dark:text-cyan-400" />;
    case 'fits_batch':
      return <Download className="w-4 h-4 text-green-600 dark:text-green-400" />;
    case 'fits_zip':
      return <FileArchive className="w-4 h-4 text-purple-600 dark:text-purple-400" />;
    case 'csv':
      return <FileSpreadsheet className="w-4 h-4 text-orange-600 dark:text-orange-400" />;
    case 'sed_plot':
      return <FileText className="w-4 h-4 text-pink-600 dark:text-pink-400" />;
    default:
      return <Download className="w-4 h-4 text-gray-600 dark:text-gray-400" />;
  }
}

export default function AdminDownloadsPage() {
  const [stats, setStats] = useState<DownloadStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState<string[]>(['30']);

  const fetchStats = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const supabase = createClient();
      const daysValue = parseInt(days[0] || '30', 10);

      const { data, error: rpcError } = await supabase.rpc('get_download_stats', {
        p_days: daysValue,
      });

      if (rpcError) {
        throw rpcError;
      }

      setStats(data as DownloadStats);
    } catch (err) {
      console.error('Error fetching download stats:', err);
      setError(err instanceof Error ? err.message : 'Failed to fetch download statistics');
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    fetchStats();
  }, [fetchStats]);

  const handleDaysChange = (selected: (string | number)[]) => {
    if (selected.length > 0) {
      setDays([selected[selected.length - 1].toString()]);
    }
  };

  const hasActiveFilters = days[0] !== '30';

  const handleClearAll = () => {
    setDays(['30']);
  };

  const formatTimestamp = (timestamp: string) => {
    // Handle Postgres timestamp format: "2026-01-16 21:59:56.727337+00"
    // Convert to ISO format: "2026-01-16T21:59:56.727337+00:00"
    let isoTimestamp = timestamp;
    if (timestamp.includes(' ') && !timestamp.includes('T')) {
      isoTimestamp = timestamp.replace(' ', 'T');
    }
    if (isoTimestamp.endsWith('+00')) {
      isoTimestamp = isoTimestamp + ':00';
    } else if (!isoTimestamp.endsWith('Z') && !isoTimestamp.includes('+')) {
      isoTimestamp = isoTimestamp + 'Z';
    }
    const date = new Date(isoTimestamp);
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
          <span className="ml-3 text-text-secondary dark:text-slate-400">Loading download statistics...</span>
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

  return (
    <div className="p-8">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold text-text-primary dark:text-slate-100">Download Analytics</h1>
          <p className="text-sm text-text-secondary dark:text-slate-400 mt-1">
            Track spectrum downloads across all users
          </p>
        </div>
        <Button variant="secondary" size="sm" onClick={fetchStats}>
          <RefreshCw className="w-4 h-4 mr-2" />
          Refresh
        </Button>
      </div>

      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-2 mb-6">
        <FilterChip
          label="Time Range"
          options={TIME_RANGE_OPTIONS}
          selected={days}
          onChange={handleDaysChange}
        />

        {hasActiveFilters && (
          <>
            <div className="h-6 w-px bg-border dark:bg-slate-700 mx-1" />
            <button
              onClick={handleClearAll}
              className="inline-flex items-center gap-1 px-3 py-1.5 text-sm text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 transition-colors"
            >
              <X className="w-3.5 h-3.5" />
              Reset
            </button>
          </>
        )}
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <Card className="p-4">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-blue-100 dark:bg-blue-950 rounded-lg">
              <Download className="w-5 h-5 text-blue-600 dark:text-blue-400" />
            </div>
            <div>
              <p className="text-sm text-text-secondary dark:text-slate-400">Total Downloads</p>
              <p className="text-2xl font-semibold text-text-primary dark:text-slate-100">
                {stats?.total_downloads?.toLocaleString() || 0}
              </p>
            </div>
          </div>
        </Card>

        <Card className="p-4">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-green-100 dark:bg-green-950 rounded-lg">
              <Users className="w-5 h-5 text-green-600 dark:text-green-400" />
            </div>
            <div>
              <p className="text-sm text-text-secondary dark:text-slate-400">Unique Users</p>
              <p className="text-2xl font-semibold text-text-primary dark:text-slate-100">
                {stats?.unique_users?.toLocaleString() || 0}
              </p>
            </div>
          </div>
        </Card>

        <Card className="p-4">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-purple-100 dark:bg-purple-950 rounded-lg">
              <File className="w-5 h-5 text-purple-600 dark:text-purple-400" />
            </div>
            <div>
              <p className="text-sm text-text-secondary dark:text-slate-400">Files Downloaded</p>
              <p className="text-2xl font-semibold text-text-primary dark:text-slate-100">
                {stats?.total_files?.toLocaleString() || 0}
              </p>
            </div>
          </div>
        </Card>

        <Card className="p-4">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-orange-100 dark:bg-orange-950 rounded-lg">
              <BarChart3 className="w-5 h-5 text-orange-600 dark:text-orange-400" />
            </div>
            <div>
              <p className="text-sm text-text-secondary dark:text-slate-400">Objects Downloaded</p>
              <p className="text-2xl font-semibold text-text-primary dark:text-slate-100">
                {stats?.total_objects?.toLocaleString() || 0}
              </p>
            </div>
          </div>
        </Card>
      </div>

      {/* Downloads by Type */}
      {stats?.by_type && Object.keys(stats.by_type).length > 0 && (
        <Card className="p-4 mb-6">
          <h2 className="text-lg font-medium text-text-primary dark:text-slate-100 mb-4">Downloads by Type</h2>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
            {Object.entries(stats.by_type).map(([type, count]) => (
              <div key={type} className="flex items-center gap-2">
                <DownloadTypeIcon type={type} />
                <div>
                  <p className="text-xs text-text-secondary dark:text-slate-400">
                    {DOWNLOAD_TYPE_LABELS[type] || type}
                  </p>
                  <p className="text-lg font-semibold text-text-primary dark:text-slate-100">
                    {count.toLocaleString()}
                  </p>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Recent Downloads */}
        <Card className="overflow-hidden">
          <div className="px-4 py-3 border-b border-border dark:border-slate-700">
            <h2 className="text-lg font-medium text-text-primary dark:text-slate-100">Recent Downloads</h2>
          </div>
          {!stats?.recent_downloads || stats.recent_downloads.length === 0 ? (
            <div className="p-8 text-center">
              <p className="text-text-secondary dark:text-slate-400">No downloads recorded yet</p>
            </div>
          ) : (
            <div className="divide-y divide-border dark:divide-slate-700 max-h-96 overflow-y-auto">
              {stats.recent_downloads.map((download) => (
                <div key={download.id} className="px-4 py-3 hover:bg-gray-50 dark:hover:bg-slate-700">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <DownloadTypeIcon type={download.download_type} />
                      <div>
                        <p className="text-sm text-text-primary dark:text-slate-100">
                          {download.full_name || download.email || 'Unknown User'}
                        </p>
                        <p className="text-xs text-text-secondary dark:text-slate-400">
                          {DOWNLOAD_TYPE_LABELS[download.download_type] || download.download_type}
                          {download.target_count && download.target_count > 1 && (
                            <span className="ml-1">({download.target_count} targets)</span>
                          )}
                        </p>
                      </div>
                    </div>
                    <span className="text-xs text-text-secondary dark:text-slate-400">
                      {formatTimestamp(download.requested_at)}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        {/* Most Downloaded Objects */}
        <Card className="overflow-hidden">
          <div className="px-4 py-3 border-b border-border dark:border-slate-700">
            <h2 className="text-lg font-medium text-text-primary dark:text-slate-100">Most Downloaded Objects</h2>
          </div>
          {!stats?.most_downloaded_objects || stats.most_downloaded_objects.length === 0 ? (
            <div className="p-8 text-center">
              <p className="text-text-secondary dark:text-slate-400">No object download data yet</p>
            </div>
          ) : (
            <div className="divide-y divide-border dark:divide-slate-700 max-h-96 overflow-y-auto">
              {stats.most_downloaded_objects.map((item, index) => (
                <div key={item.target_id} className="px-4 py-3 hover:bg-gray-50 dark:hover:bg-slate-700">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <span className="w-6 h-6 flex items-center justify-center text-xs font-medium text-text-secondary dark:text-slate-400 bg-gray-100 dark:bg-slate-800 rounded">
                        {index + 1}
                      </span>
                      <Link
                        href={`/nirspec/targets/${item.target_id}`}
                        className="text-sm font-mono text-primary hover:underline"
                      >
                        {item.target_id}
                      </Link>
                    </div>
                    <span className="text-sm text-text-secondary dark:text-slate-400">
                      {item.download_count} download{item.download_count !== 1 ? 's' : ''}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
