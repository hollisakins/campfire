'use client';

import React from 'react';
import Link from 'next/link';
import { Users } from 'lucide-react';
import type { DashboardSummary } from '@/lib/actions/admin-dashboard';
import { Panel, PanelError, SkeletonRows, StatCell, ViewAllLink } from './primitives';
import { fmtAgo, fmtWhen, fmtCount } from '@/lib/admin/format';

// ---------------------------------------------------------------------------
// People & access. User counts exclude link accounts (synthetic share-link
// principals) server-side. Recent signups carry provisioning state inline —
// an amber "no programs" chip is the signed-up-but-not-provisioned signal.
// The access line mirrors the attention rules so the two surfaces agree.
// ---------------------------------------------------------------------------

export function PeoplePanel({
  summary, loading, error, onRetry,
}: {
  summary: DashboardSummary | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  const u = summary?.users;
  const a = summary?.access;
  return (
    <Panel icon={Users} title="People & access" right={<ViewAllLink href="/admin/users" label="Users" />}>
      {loading ? (
        <SkeletonRows n={7} />
      ) : error || !u || !a ? (
        <PanelError message={error} onRetry={onRetry} />
      ) : (
        <>
          <div className="grid grid-cols-4 gap-2 px-3 py-2 border-b border-border">
            <StatCell value={fmtCount(u.total)} label="Users" href="/admin/users" />
            <StatCell value={fmtCount(u.inspectors)} label="Inspect" href="/admin/users" />
            <StatCell value={fmtCount(u.admins)} label="Admins" href="/admin/users" />
            <StatCell value={fmtCount(u.group_accounts)} label="Groups" href="/admin/users" />
          </div>

          {u.recent_signups.length === 0 ? (
            <p className="px-3 py-2 text-xs text-text-tertiary border-b border-border">
              No signups yet.
            </p>
          ) : (
            <div className="divide-y divide-border border-b border-border">
              {u.recent_signups.map((s) => (
                <Link
                  key={s.user_id}
                  href="/admin/users"
                  className="flex items-center gap-2 px-3 h-8 hover:bg-card-hover"
                >
                  <span className="text-xs text-text-primary truncate">
                    {s.full_name || s.username}
                  </span>
                  {s.is_admin && (
                    <span className="text-[9px] uppercase px-1 rounded bg-primary-soft text-primary-text shrink-0">admin</span>
                  )}
                  {s.is_group_account && (
                    <span className="text-[9px] uppercase px-1 rounded bg-surface-2 text-text-secondary shrink-0">group</span>
                  )}
                  {s.can_inspect && (
                    <span className="text-[9px] uppercase px-1 rounded bg-surface-2 text-text-secondary shrink-0">inspect</span>
                  )}
                  <span className="ml-auto flex items-center gap-2 shrink-0">
                    <span className={`text-[11px] tabular-nums ${s.n_programs === 0 && !s.is_admin ? 'text-warning' : 'text-text-tertiary'}`}>
                      {s.n_programs === 0 && !s.is_admin ? 'no programs' : `${s.n_programs} prog`}
                    </span>
                    <span className="text-[11px] text-text-tertiary tabular-nums" title={fmtWhen(s.created_at)}>
                      {fmtAgo(s.created_at)}
                    </span>
                  </span>
                </Link>
              ))}
            </div>
          )}

          <div className="px-3 py-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px]">
            <Link href="/admin/inspection-requests" className={`hover:underline ${a.pending_requests > 0 ? 'text-warning' : 'text-text-secondary'}`}>
              {fmtCount(a.pending_requests)} access requests
            </Link>
            <Link href="/admin/users" className={`hover:underline ${a.stale_invites > 0 ? 'text-warning' : 'text-text-secondary'}`}>
              {fmtCount(a.open_invites)} invites
              {a.stale_invites > 0 ? ` (${a.stale_invites} stale)` : ''}
            </Link>
            <Link href="/admin/share-links" className={`hover:underline ${a.links_exposing_drafts > 0 ? 'text-danger' : 'text-text-secondary'}`}>
              {fmtCount(a.active_share_links)} share links
              {a.links_exposing_drafts > 0 ? ` (${a.links_exposing_drafts} expose drafts)` : ''}
            </Link>
            <Link href="/admin/codes" className={`hover:underline ${a.codes_all_programs > 0 ? 'text-warning' : 'text-text-secondary'}`}>
              {fmtCount(a.active_codes)} codes
              {a.codes_all_programs > 0 ? ` (${a.codes_all_programs} grant all)` : ''}
            </Link>
          </div>
        </>
      )}
    </Panel>
  );
}
