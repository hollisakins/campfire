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
