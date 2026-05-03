'use client';

import React from 'react';
import { Database, Telescope, MapPin, Hash, Layers, HardDrive, Clock } from 'lucide-react';
import type { DatabaseOverview } from '@/lib/actions/programs';

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

function formatNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toLocaleString();
}

function formatRelative(ts: string | null): string {
  if (!ts) return '—';
  const t = new Date(ts).getTime();
  if (isNaN(t)) return '—';
  const diffMs = Date.now() - t;
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}

interface ScopeHeaderProps {
  overview: DatabaseOverview | null;
  loading?: boolean;
}

const Stat: React.FC<{
  icon: React.ComponentType<{ className?: string }>;
  value: string;
  label: string;
}> = ({ icon: Icon, value, label }) => (
  <div className="flex items-center gap-2 px-3 py-2">
    <Icon className="w-4 h-4 text-text-secondary dark:text-slate-400 flex-shrink-0" />
    <div className="flex flex-col leading-tight">
      <span className="text-sm font-semibold text-text-primary dark:text-slate-100 tabular-nums">
        {value}
      </span>
      <span className="text-[10px] uppercase tracking-wider text-text-secondary dark:text-slate-400">
        {label}
      </span>
    </div>
  </div>
);

export const ScopeHeader: React.FC<ScopeHeaderProps> = ({ overview, loading }) => {
  if (loading || !overview) {
    return (
      <div className="bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg px-2 py-1 mb-6 h-14 animate-pulse" />
    );
  }

  const lastReductionLabel = overview.latest_reduction_version
    ? `${overview.latest_reduction_version} · ${formatRelative(overview.latest_deployed_at)}`
    : formatRelative(overview.latest_deployed_at);

  return (
    <div className="bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg px-2 py-1 mb-6 flex flex-wrap items-center divide-x divide-border dark:divide-slate-700">
      <Stat icon={Database} value={formatNumber(overview.n_programs)} label="programs" />
      <Stat icon={Telescope} value={formatNumber(overview.n_observations)} label="observations" />
      <Stat icon={MapPin} value={formatNumber(overview.n_pointings)} label="pointings" />
      <Stat icon={Hash} value={formatNumber(overview.n_targets)} label="targets" />
      <Stat icon={Layers} value={formatNumber(overview.n_spectra)} label="spectra" />
      <Stat icon={HardDrive} value={formatBytes(overview.total_size_bytes)} label="size" />
      <Stat icon={Clock} value={lastReductionLabel} label="last reduction" />
    </div>
  );
};
