'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { Card } from '@/components/ui/Card';
import { AlertTriangle, CheckCircle2, ChevronRight } from 'lucide-react';
import type { DashboardSummary } from '@/lib/actions/admin-dashboard';
import type { StorageBudget } from '@/lib/actions/storage-registry';
import { ATTENTION_RULES, evaluateAttention, type FiringRule, type Severity } from '@/lib/admin/attention';
import { fmtCount } from '@/lib/admin/format';

// ---------------------------------------------------------------------------
// The attention rail: only firing rules render, severity-ranked, each row a
// deep link with a STAGE tag indexing it into the panels below. Quiet when
// all is well; loud only where action is needed. Loading and error states are
// explicit — an unresolved query must never read as "all clear".
// ---------------------------------------------------------------------------

const VISIBLE_CAP = 8;

const DOT: Record<Severity, string> = {
  act: 'bg-danger',
  soon: 'bg-warning',
  info: 'bg-text-tertiary',
};

function RuleRow({ firing }: { firing: FiringRule }) {
  const { rule, count, detail } = firing;
  const isAnchor = rule.href.startsWith('#');
  const inner = (
    <>
      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${DOT[rule.severity]}`} />
      <span className="font-mono text-[10px] uppercase text-text-tertiary w-16 shrink-0">
        {rule.stage}
      </span>
      <span className="text-sm font-semibold tabular-nums w-14 text-right shrink-0 text-text-primary">
        {fmtCount(count)}
      </span>
      <span className="text-sm text-text-primary truncate">{rule.label}</span>
      {detail && (
        <span className="text-xs text-text-secondary truncate hidden sm:inline">{detail}</span>
      )}
      <ChevronRight className="w-3.5 h-3.5 text-text-tertiary ml-auto shrink-0" />
    </>
  );
  const cls =
    'flex items-center gap-3 px-3 h-9 hover:bg-card-hover transition-colors';
  return isAnchor ? (
    <a
      href={rule.href}
      className={cls}
      onClick={(e) => {
        e.preventDefault();
        document.getElementById(rule.href.slice(1))?.scrollIntoView({ behavior: 'smooth' });
      }}
    >
      {inner}
    </a>
  ) : (
    <Link href={rule.href} className={cls}>{inner}</Link>
  );
}

export function AttentionRail({
  summary, budget, loading, error, onRetry, updatedAt,
}: {
  summary: DashboardSummary | null;
  budget: StorageBudget | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
  updatedAt: number | undefined;
}) {
  const [expanded, setExpanded] = useState(false);
  const [showChecks, setShowChecks] = useState(false);

  if (loading) {
    return (
      <Card className="overflow-hidden">
        <div className="p-3 space-y-2.5">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-4 bg-surface-2 rounded animate-pulse" />
          ))}
        </div>
      </Card>
    );
  }

  if (error || !summary) {
    return (
      <Card className="overflow-hidden border-warning/50">
        <div className="flex items-center gap-2 px-3 h-10 text-sm text-warning">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          <span className="truncate" title={error}>
            Attention checks unavailable{error ? ` — ${error}` : ''}
          </span>
          <button onClick={onRetry} className="ml-auto text-primary-text hover:underline text-xs">
            Retry
          </button>
        </div>
      </Card>
    );
  }

  const { firing, checked } = evaluateAttention({ summary, budget });
  const checkedAt = updatedAt
    ? new Date(updatedAt).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : null;

  if (firing.length === 0) {
    return (
      <Card className="overflow-hidden">
        <div className="flex items-center gap-2 px-3 h-10 text-sm">
          <CheckCircle2 className="w-4 h-4 text-success shrink-0" />
          <span className="text-text-primary">Nothing needs attention</span>
          <button
            onClick={() => setShowChecks((v) => !v)}
            className="text-xs text-text-tertiary hover:text-text-secondary"
          >
            {checked} checks
          </button>
          {checkedAt && (
            <span className="ml-auto text-xs text-text-tertiary">checked {checkedAt}</span>
          )}
        </div>
        {showChecks && (
          <div className="border-t border-border px-3 py-2 grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1">
            {ATTENTION_RULES.map((r) => (
              <div key={r.id} className="flex items-center gap-2 text-xs text-text-tertiary">
                <span className="font-mono text-[10px] uppercase w-16 shrink-0">{r.stage}</span>
                <span className="truncate">{r.label}</span>
                <span className="ml-auto tabular-nums">0</span>
              </div>
            ))}
          </div>
        )}
      </Card>
    );
  }

  const visible = expanded ? firing : firing.slice(0, VISIBLE_CAP);
  const hidden = firing.length - visible.length;

  return (
    <Card className="overflow-hidden">
      <div className="divide-y divide-border">
        {visible.map((f) => <RuleRow key={f.rule.id} firing={f} />)}
      </div>
      {hidden > 0 && (
        <button
          onClick={() => setExpanded(true)}
          className="w-full px-3 h-8 text-xs text-text-secondary hover:bg-card-hover border-t border-border text-left"
        >
          Show {hidden} more
        </button>
      )}
    </Card>
  );
}
