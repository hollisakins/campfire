'use client';

import React from 'react';
import Link from 'next/link';
import { Archive } from 'lucide-react';
import { ReleaseStateBadge } from '@/components/releases/ReleaseStateBadge';
import type { DrReleaseRow } from '@/lib/server/dr-exports';
import { releaseCount, releaseNextStep } from '@/lib/dr-releases';
import { fmtAgo, fmtCount, fmtWhen } from '@/lib/admin/format';
import { Panel, PanelEmpty, PanelError, SkeletonRows, ViewAllLink } from './primitives';

const SHOWN = 4;

export function ReleasesPanel({
  releases, loading, error, onRetry,
}: {
  releases: DrReleaseRow[] | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  return (
    <Panel icon={Archive} title="Data releases" right={<ViewAllLink href="/admin/releases" />}>
      {loading ? (
        <SkeletonRows n={3} />
      ) : error || !releases ? (
        <PanelError message={error} onRetry={onRetry} />
      ) : releases.length === 0 ? (
        <PanelEmpty text="No releases" />
      ) : (
        <div className="divide-y divide-border">
          {releases.slice(0, SHOWN).map((r) => {
            const next = releaseNextStep(r.state, r.tag);
            const when = r.published_at ?? r.frozen_at;
            const spectra = releaseCount(r.counts, 'spectra');
            return (
              <Link
                key={r.tag}
                href="/admin/releases"
                className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-xs hover:bg-card-hover"
              >
                <span className="font-mono text-text-primary">{r.tag}</span>
                <ReleaseStateBadge state={r.state} />
                {spectra != null && (
                  <span className="text-text-secondary tabular-nums">{fmtCount(spectra)} spectra</span>
                )}
                {next && <code className="font-mono text-[10px] text-text-tertiary truncate">{next}</code>}
                <span className="ml-auto text-text-tertiary tabular-nums whitespace-nowrap" title={fmtWhen(when)}>
                  {r.published_at ? 'published' : r.frozen_at ? 'frozen' : ''} {fmtAgo(when)}
                </span>
              </Link>
            );
          })}
          {releases.length > SHOWN && (
            <Link href="/admin/releases" className="block px-3 py-2 text-xs text-text-secondary hover:bg-card-hover">
              +{releases.length - SHOWN} more
            </Link>
          )}
        </div>
      )}
    </Panel>
  );
}
