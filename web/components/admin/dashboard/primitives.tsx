'use client';

import React from 'react';
import Link from 'next/link';
import { Card } from '@/components/ui/Card';
import { AlertTriangle } from 'lucide-react';

// ---------------------------------------------------------------------------
// Dashboard primitives. Every panel follows one state contract:
//   loading → skeleton rows of roughly the final height (no layout shift)
//   error   → an explicit inline unavailable line with Retry — NEVER a zero
//   empty   → one muted line
// The old dashboard painted confident zeros while queries were in flight or
// failed; these primitives make that state unrepresentable.
// ---------------------------------------------------------------------------

export function Panel({
  id, icon: Icon, title, right, children, className = '',
}: {
  id?: string;
  icon: React.ElementType;
  title: string;
  right?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Card id={id} className={`overflow-hidden flex flex-col ${className}`}>
      <div className="px-3 py-2 border-b border-border bg-surface-2 flex items-center gap-2 min-h-[38px]">
        <Icon className="w-3.5 h-3.5 text-text-tertiary shrink-0" />
        <h2 className="text-[11px] font-medium text-text-secondary uppercase tracking-wider truncate">
          {title}
        </h2>
        {right && <div className="ml-auto flex items-center gap-2 text-xs">{right}</div>}
      </div>
      <div className="flex-1 min-h-0">{children}</div>
    </Card>
  );
}

export function ViewAllLink({ href, label = 'View all' }: { href: string; label?: string }) {
  return (
    <Link href={href} className="text-xs text-primary-text hover:underline whitespace-nowrap">
      {label}
    </Link>
  );
}

export function SkeletonRows({ n = 4, className = '' }: { n?: number; className?: string }) {
  return (
    <div className={`p-3 space-y-2.5 ${className}`}>
      {Array.from({ length: n }).map((_, i) => (
        <div key={i} className="h-3.5 bg-surface-2 rounded animate-pulse" />
      ))}
    </div>
  );
}

export function PanelError({
  message, onRetry,
}: {
  message?: string;
  onRetry?: () => void;
}) {
  return (
    <div className="p-3 flex items-center gap-2 text-xs text-warning">
      <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
      <span className="truncate" title={message}>Unavailable{message ? ` — ${message}` : ''}</span>
      {onRetry && (
        <button onClick={onRetry} className="ml-auto text-primary-text hover:underline shrink-0">
          Retry
        </button>
      )}
    </div>
  );
}

export function PanelEmpty({ text }: { text: string }) {
  return <p className="p-3 text-xs text-text-tertiary">{text}</p>;
}

// Proportional segmented bar. Segments render left to right; the container is
// the sum of segment values unless `totalOverride` widens it (budget meters).
export function Meter({
  segments, totalOverride, height = 6, className = '',
}: {
  segments: { value: number; className: string; title?: string }[];
  totalOverride?: number;
  height?: number;
  className?: string;
}) {
  const sum = segments.reduce((s, x) => s + x.value, 0);
  const total = Math.max(totalOverride ?? sum, sum, 1);
  return (
    <div
      className={`w-full rounded-full bg-surface-2 overflow-hidden flex ${className}`}
      style={{ height }}
    >
      {segments.filter((s) => s.value > 0).map((s, i) => (
        <div
          key={i}
          className={s.className}
          style={{ width: `${(s.value / total) * 100}%` }}
          title={s.title}
        />
      ))}
    </div>
  );
}

// Dependency-free day-bucketed bar chart. Fills calendar gaps with zero-height
// ticks so quiet days read as gaps rather than being skipped.
export function Sparkline({
  series, days, height = 36,
}: {
  series: { day: string; count: number }[];
  days: number;
  height?: number;
}) {
  const byDay = new Map(series.map((s) => [s.day.slice(0, 10), Number(s.count) || 0]));
  const bars: { day: string; count: number }[] = [];
  const today = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today.getTime() - i * 86400000);
    const key = d.toISOString().slice(0, 10);
    bars.push({ day: key, count: byDay.get(key) ?? 0 });
  }
  const max = Math.max(1, ...bars.map((b) => b.count));
  return (
    <div className="flex items-end gap-px w-full" style={{ height }}>
      {bars.map((b) => (
        <div
          key={b.day}
          className="flex-1 rounded-t-sm bg-primary/50 min-w-0"
          style={{ height: b.count > 0 ? `${Math.max(8, (b.count / max) * 100)}%` : 2 }}
          title={`${b.day} · ${b.count}`}
        />
      ))}
    </div>
  );
}

export function StatCell({
  value, label, href, title, accent = false,
}: {
  value: React.ReactNode;
  label: string;
  href?: string;
  title?: string;
  accent?: boolean;
}) {
  const body = (
    <div title={title} className="min-w-0">
      <div className={`text-base font-semibold tabular-nums leading-tight ${accent ? 'text-warning' : 'text-text-primary'}`}>
        {value}
      </div>
      <div className="text-[10px] uppercase tracking-wider text-text-tertiary truncate">{label}</div>
    </div>
  );
  return href ? (
    <Link href={href} className="block min-w-0 hover:opacity-80 transition-opacity">{body}</Link>
  ) : body;
}
