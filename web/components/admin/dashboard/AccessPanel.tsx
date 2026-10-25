'use client';

import React from 'react';
import Link from 'next/link';
import { KeyRound, Link2, UserCheck, UserPlus } from 'lucide-react';
import type { AdminDashboardSummary } from '@/app/api/admin/dashboard/route';
import { ageDays, fmtCount } from '@/lib/admin/format';
import { Panel, PanelError, SkeletonRows, ViewAllLink } from './primitives';

const QUICK_ACTIONS = [
  { href: '/admin/users?new=invite', label: 'Invite user', icon: UserPlus },
  { href: '/admin/codes?new=1', label: 'Access code', icon: KeyRound },
  { href: '/admin/share-links?new=1', label: 'Share link', icon: Link2 },
];

export function AccessPanel({
  access, loading, error, onRetry,
}: {
  access: AdminDashboardSummary['access'] | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  return (
    <Panel icon={UserCheck} title="Access" right={<ViewAllLink href="/admin/inspection-requests" />}>
      {loading ? (
        <SkeletonRows n={3} />
      ) : error || !access ? (
        <PanelError message={error} onRetry={onRetry} />
      ) : (
        <>
          <Link
            href="/admin/inspection-requests"
            className="flex items-center justify-between gap-3 px-3 py-2.5 hover:bg-card-hover"
          >
            <span className="text-sm text-text-primary">Inspection requests</span>
            <span className="flex items-baseline gap-2 tabular-nums">
              {access.pending_requests > 0 && access.oldest_request_at && (
                <span className="text-[11px] text-text-tertiary">oldest {ageDays(access.oldest_request_at)}d</span>
              )}
              <span className={`text-lg font-semibold ${access.pending_requests > 0 ? 'text-warning' : 'text-text-tertiary'}`}>
                {fmtCount(access.pending_requests)}
              </span>
            </span>
          </Link>
          <div className="border-t border-border px-3 py-2 flex flex-wrap gap-1.5">
            {QUICK_ACTIONS.map(({ href, label, icon: Icon }) => (
              <Link
                key={href}
                href={href}
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-[11px] text-text-secondary hover:bg-card-hover hover:text-text-primary"
              >
                <Icon className="w-3 h-3" />
                {label}
              </Link>
            ))}
          </div>
        </>
      )}
    </Panel>
  );
}
