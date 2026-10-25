'use client';

import React from 'react';
import Link from 'next/link';
import { Archive, Camera, Rocket, Telescope } from 'lucide-react';
import { Card } from '@/components/ui/Card';
import { ReleaseStateBadge } from '@/components/releases/ReleaseStateBadge';
import type { DrReleaseRow } from '@/lib/server/dr-exports';
import type { DashboardLease } from '@/app/api/admin/dashboard/route';
import { fmtAgo, fmtWhen } from '@/lib/admin/format';
import { releaseInProgress } from '@/lib/admin/dashboard';

// What is running right now: deploy leases (campfire deploy on some machine)
// and releases mid-lifecycle. Renders nothing when the system is quiet.
export function InProgressStrip({
  leases, releases,
}: {
  leases: DashboardLease[];
  releases: DrReleaseRow[];
}) {
  const running = releases.filter((r) => releaseInProgress(r.state));
  if (leases.length === 0 && running.length === 0) return null;

  return (
    <Card className="overflow-hidden border-l-2 border-l-warning">
      <div className="divide-y divide-border">
        {leases.map((lease) => {
          const Icon = lease.scope_type === 'field' ? Camera : Telescope;
          return (
            <div key={`${lease.scope_type}:${lease.scope_key}`} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-3 py-2 text-xs">
              <span className="inline-flex items-center gap-1.5 text-text-primary">
                <Rocket className={`w-3.5 h-3.5 ${lease.stale ? 'text-danger' : 'text-warning'}`} />
                <span className="font-medium">{lease.stale ? 'Deploy stalled' : 'Deploying'}</span>
              </span>
              <span className="inline-flex items-center gap-1.5 font-mono text-text-primary">
                <Icon className="w-3 h-3 text-text-tertiary" />
                {lease.scope_key}
              </span>
              {lease.host && <span className="text-text-secondary">{lease.host}</span>}
              {lease.actor_name && <span className="text-text-secondary">{lease.actor_name}</span>}
              <span className="ml-auto flex items-center gap-3 tabular-nums text-text-tertiary">
                <span title={fmtWhen(lease.since)}>started {fmtAgo(lease.since)} ago</span>
                <span className={lease.stale ? 'text-danger' : ''} title={fmtWhen(lease.heartbeat_at)}>
                  heartbeat {fmtAgo(lease.heartbeat_at ?? lease.since)}
                </span>
              </span>
            </div>
          );
        })}
        {running.map((r) => (
          <Link
            key={r.tag}
            href="/admin/releases"
            className="flex flex-wrap items-center gap-x-4 gap-y-1 px-3 py-2 text-xs hover:bg-card-hover"
          >
            <span className="inline-flex items-center gap-1.5 text-text-primary">
              <Archive className="w-3.5 h-3.5 text-warning" />
              <span className="font-medium">Release</span>
            </span>
            <span className="font-mono text-text-primary">{r.tag}</span>
            <ReleaseStateBadge state={r.state} />
          </Link>
        ))}
      </div>
    </Card>
  );
}
