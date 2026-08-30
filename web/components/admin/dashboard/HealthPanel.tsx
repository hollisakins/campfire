'use client';

import React, { useState } from 'react';
import { Activity, Check, Copy } from 'lucide-react';
import type { DashboardSummary, LifecycleStatus } from '@/lib/actions/admin-dashboard';
import { Panel, PanelError, SkeletonRows } from './primitives';
import { fmtBytes, fmtCount } from '@/lib/admin/format';

// ---------------------------------------------------------------------------
// Health & integrity: single-line checks. Rows whose state is genuinely
// CLI-only (cloud verify, config diff) render the exact command with a copy
// affordance instead of a green tick we cannot substantiate — honest absence
// beats a fabricated pass.
// ---------------------------------------------------------------------------

function CopyCmd({ cmd }: { cmd: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => {
        navigator.clipboard?.writeText(cmd).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
      className="inline-flex items-center gap-1 font-mono text-[10px] text-text-tertiary hover:text-text-primary"
      title="Copy command"
    >
      {cmd}
      {copied ? <Check className="w-3 h-3 text-success" /> : <Copy className="w-3 h-3" />}
    </button>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-2 px-3 h-8 text-xs">
      <span className="text-text-secondary shrink-0">{label}</span>
      <span className="text-right min-w-0 flex items-center gap-1.5 justify-end">{children}</span>
    </div>
  );
}

function Dot({ tone }: { tone: 'ok' | 'warn' | 'bad' | 'off' }) {
  const cls = {
    ok: 'bg-success', warn: 'bg-warning', bad: 'bg-danger', off: 'bg-text-tertiary',
  }[tone];
  return <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${cls}`} />;
}

export function HealthPanel({
  summary, lifecycle, loading, error, onRetry,
}: {
  summary: DashboardSummary | null;
  lifecycle: LifecycleStatus | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  return (
    <Panel icon={Activity} title="Health">
      {loading ? (
        <SkeletonRows n={7} />
      ) : error || !summary ? (
        <PanelError message={error} onRetry={onRetry} />
      ) : (
        <div className="divide-y divide-border">
          <Row label="Lifecycle">
            {lifecycle ? (
              <>
                <Dot tone={lifecycle.enabled ? 'ok' : 'bad'} />
                <span className={lifecycle.enabled ? 'text-text-primary' : 'text-danger'}>
                  {lifecycle.enabled ? `ok · v${lifecycle.version}` : 'degraded'}
                </span>
              </>
            ) : (
              <span className="text-text-tertiary">—</span>
            )}
          </Row>
          <Row label="Cloud verify">
            <CopyCmd cmd="campfire verify --cloud" />
          </Row>
          <Row label="Config diff">
            <CopyCmd cmd="campfire config diff" />
          </Row>
          <Row label="Off-release published">
            <Dot tone={summary.deployments.unreleased_published > 0 ? 'warn' : 'ok'} />
            <span className="tabular-nums text-text-primary">
              {fmtCount(summary.deployments.unreleased_published)}
            </span>
          </Row>
          <Row label="CRDS contexts live">
            <Dot tone={summary.deployments.distinct_crds_contexts > 1 ? 'warn' : 'ok'} />
            <span className="tabular-nums text-text-primary">
              {fmtCount(summary.deployments.distinct_crds_contexts)}
            </span>
          </Row>
          <Row label="Missing provenance">
            <Dot tone={summary.deployments.missing_provenance > 0 ? 'warn' : 'ok'} />
            <span className="tabular-nums text-text-primary">
              {fmtCount(summary.deployments.missing_provenance)}
            </span>
          </Row>
          <Row label="Provisional hashes">
            <Dot tone={summary.storage.provisional_hashes > 0 ? 'warn' : 'ok'} />
            <span className="tabular-nums text-text-primary">
              {fmtCount(summary.storage.provisional_hashes)}
            </span>
          </Row>
          <Row label="Pushed, undeployed 14d">
            <Dot tone={summary.storage.pushed_undeployed_14d > 0 ? 'warn' : 'ok'} />
            <span className="tabular-nums text-text-primary">
              {fmtCount(summary.storage.pushed_undeployed_14d)}
            </span>
          </Row>
          <Row label="Reclaimable">
            <span className="tabular-nums text-text-primary">
              {fmtBytes(summary.storage.reclaimable_bytes)}
            </span>
          </Row>
        </div>
      )}
    </Panel>
  );
}
