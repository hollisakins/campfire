'use client';

import React from 'react';
import { BarChart3 } from 'lucide-react';
import { Card } from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import { ProfileStats as ProfileStatsType } from '@/lib/types';

interface ProfileStatsProps {
  stats: ProfileStatsType;
}

/**
 * Format a timestamp as relative time (e.g., "2 days ago", "Just now")
 */
function formatRelativeTime(timestamp: string | null): string {
  if (!timestamp) return 'Never';

  const now = new Date();
  const date = new Date(timestamp);
  const diffMs = now.getTime() - date.getTime();
  const diffSeconds = Math.floor(diffMs / 1000);
  const diffMinutes = Math.floor(diffSeconds / 60);
  const diffHours = Math.floor(diffMinutes / 60);
  const diffDays = Math.floor(diffHours / 24);
  const diffWeeks = Math.floor(diffDays / 7);
  const diffMonths = Math.floor(diffDays / 30);

  if (diffSeconds < 60) return 'Just now';
  if (diffMinutes < 60) return `${diffMinutes}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  if (diffWeeks < 4) return `${diffWeeks}w ago`;
  return `${diffMonths}mo ago`;
}

export const ProfileStats: React.FC<ProfileStatsProps> = ({ stats }) => {
  return (
    <Card className="p-6">
      <div className="flex items-center gap-3 mb-4">
        <div className="w-10 h-10 bg-primary/10 rounded-full flex items-center justify-center">
          <BarChart3 className="w-5 h-5 text-primary" />
        </div>
        <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100">
          Your Activity
        </h2>
      </div>

      <div className="flex gap-3">
        <Badge value={stats.objects_inspected} label="INSPECTED" compact />
        <Badge value={stats.comments_posted} label="COMMENTS" compact />
        <Badge value={formatRelativeTime(stats.last_activity)} label="LAST ACTIVE" compact />
      </div>
    </Card>
  );
};
