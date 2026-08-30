'use client';

import React from 'react';
import Link from 'next/link';
import { Telescope } from 'lucide-react';
import type { DashboardSummary } from '@/lib/actions/admin-dashboard';
import { Panel, PanelEmpty, PanelError, SkeletonRows, ViewAllLink } from './primitives';
import { fmtAgo, fmtWhen } from '@/lib/admin/format';

// ---------------------------------------------------------------------------
// Scopes: the newest observations and fields, with deployment and
// config-plane state — the closest thing the panel has to a config-plane
// read-out (campfire config push/pull/diff is CLI-only). A hollow amber dot
// marks a scope defined but never pushed through the config plane; "never
// deployed" is the earliest signal that data entered the pipeline and never
// reached the catalog.
// ---------------------------------------------------------------------------

export function ScopesPanel({
  summary, loading, error, onRetry,
}: {
  summary: DashboardSummary | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  const scopes = summary?.scopes;
  return (
    <Panel
      id="scopes-panel"
      icon={Telescope}
      title="New observations & fields"
      right={<ViewAllLink href="/admin/programs" label="Programs" />}
    >
      {loading ? (
        <SkeletonRows n={6} />
      ) : error || !scopes ? (
        <PanelError message={error} onRetry={onRetry} />
      ) : scopes.new_scopes.length === 0 ? (
        <PanelEmpty text="No observations or fields defined yet." />
      ) : (
        <>
          <div className="divide-y divide-border">
            {scopes.new_scopes.map((s) => {
              const href = s.kind === 'observation'
                ? `/admin/intermediate-products?obs=${encodeURIComponent(s.name)}`
                : `/admin/nircam?field=${encodeURIComponent(s.name)}`;
              return (
                <Link key={`${s.kind}-${s.name}`} href={href} className="flex items-center gap-2 px-3 h-9 hover:bg-card-hover">
                  <span className={`font-mono text-[9px] uppercase px-1 py-px rounded border shrink-0 ${
                    s.kind === 'observation'
                      ? 'border-border text-text-secondary'
                      : 'border-primary/40 text-primary-text'
                  }`}>
                    {s.kind === 'observation' ? 'OBS' : 'FLD'}
                  </span>
                  {s.config_never_pushed && !s.retired && (
                    <span
                      className="w-1.5 h-1.5 rounded-full border border-warning shrink-0"
                      title="Defined locally, never pushed through the config plane (campfire config push)"
                    />
                  )}
                  <span className="font-mono text-xs text-text-primary truncate">{s.name}</span>
                  {s.program && (
                    <span className="text-[11px] text-text-tertiary truncate hidden sm:inline">
                      {s.program}
                    </span>
                  )}
                  <span className="ml-auto flex items-center gap-2 shrink-0">
                    {s.retired ? (
                      <span className="text-[10px] px-1 rounded bg-surface-2 text-text-tertiary">retired</span>
                    ) : s.last_deploy_at ? (
                      <span
                        className="text-[11px] text-text-secondary tabular-nums"
                        title={`${s.last_deploy_status ?? 'deployed'} · ${fmtWhen(s.last_deploy_at)}`}
                      >
                        deployed {fmtAgo(s.last_deploy_at)}
                      </span>
                    ) : (
                      <span className="text-[11px] text-warning">never deployed</span>
                    )}
                    <span
                      className="text-[11px] text-text-tertiary tabular-nums"
                      title={fmtWhen(s.created_at)}
                    >
                      added {fmtAgo(s.created_at)}
                    </span>
                  </span>
                </Link>
              );
            })}
          </div>
          {(scopes.config_never_pushed > 0 || scopes.retired_with_live_deployment > 0 || scopes.never_deployed > 0) && (
            <div className="px-3 py-1.5 border-t border-border flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-text-tertiary">
              {scopes.never_deployed > 0 && <span>{scopes.never_deployed} never deployed</span>}
              {scopes.config_never_pushed > 0 && (
                <span title="campfire config push">{scopes.config_never_pushed} config not pushed</span>
              )}
              {scopes.retired_with_live_deployment > 0 && (
                <span className="text-warning">{scopes.retired_with_live_deployment} retired with live deployment</span>
              )}
            </div>
          )}
        </>
      )}
    </Panel>
  );
}
