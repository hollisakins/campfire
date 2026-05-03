'use client';

import React from 'react';
import { useRouter } from 'next/navigation';
import {
  Telescope,
  Users,
  ExternalLink,
  ArrowRight,
  Hash,
  Globe,
  Lock,
} from 'lucide-react';
import type { ProgramOverview } from '@/lib/actions/programs';

function formatNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toLocaleString();
}

function formatRelative(ts: string | null): string {
  if (!ts) return 'never';
  const t = new Date(ts).getTime();
  if (isNaN(t)) return 'never';
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

function formatGratings(gratings: string[]): string {
  if (gratings.length === 0) return '—';
  if (gratings.length <= 3) return gratings.join(', ');
  return `${gratings.slice(0, 3).join(', ')} +${gratings.length - 3}`;
}

interface ProgramRowProps {
  program: ProgramOverview;
}

export const ProgramRow: React.FC<ProgramRowProps> = ({ program }) => {
  const router = useRouter();
  const stsciHref = program.jwst_pids?.[0]
    ? `https://www.stsci.edu/jwst-program-info/program/?program=${program.jwst_pids[0]}`
    : null;

  return (
    <div
      className="group grid grid-cols-[auto,1fr,auto,auto] gap-4 px-4 py-3 border-b border-border dark:border-slate-700 hover:bg-card-hover dark:hover:bg-slate-700/40 cursor-pointer transition-colors"
      onClick={() => router.push(`/nirspec/metadata/programs/${program.slug}`)}
    >
      {/* Identity column */}
      <div className="flex items-start gap-3 min-w-0">
        <div className="w-8 h-8 rounded-md bg-primary/10 flex items-center justify-center flex-shrink-0 mt-0.5">
          <Telescope className="w-4 h-4 text-primary" />
        </div>
        <div className="min-w-0">
          <div className="font-semibold text-text-primary dark:text-slate-100 truncate">
            {program.program_name || program.slug}
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 mt-0.5 text-xs text-text-secondary dark:text-slate-400">
            <span className="font-mono">{program.slug}</span>
            {program.pi_name && (
              <span className="inline-flex items-center gap-1">
                <Users className="w-3 h-3" />
                {program.pi_name}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Stats column */}
      <div className="flex flex-col justify-center min-w-0">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-text-secondary dark:text-slate-400">
          <span className="inline-flex items-center gap-1">
            <Hash className="w-3 h-3" />
            <span className="text-text-primary dark:text-slate-100 font-medium">
              {formatNumber(program.target_count)}
            </span>{' '}
            targets
          </span>
          <span>
            <span className="text-text-primary dark:text-slate-100 font-medium">
              {program.n_observations}
            </span>{' '}
            obs
          </span>
          <span>
            <span className="text-text-primary dark:text-slate-100 font-medium">
              {program.fields.length}
            </span>{' '}
            {program.fields.length === 1 ? 'field' : 'fields'}
          </span>
          <span className="font-mono text-[11px]" title={program.gratings.join(', ')}>
            {formatGratings(program.gratings)}
          </span>
        </div>
      </div>

      {/* Status column */}
      <div className="flex flex-col items-end justify-center text-xs">
        <div className="flex items-center gap-2">
          {program.cycle != null && (
            <span className="inline-flex items-center px-2 py-0.5 bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300 rounded-full font-medium">
              Cycle {program.cycle}
            </span>
          )}
          {program.is_public ? (
            <span className="inline-flex items-center gap-1 text-emerald-700 dark:text-emerald-400" title="Public">
              <Globe className="w-3 h-3" />
              public
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 text-text-secondary dark:text-slate-400" title="Restricted">
              <Lock className="w-3 h-3" />
              restricted
            </span>
          )}
        </div>
        <div className="text-text-secondary dark:text-slate-400 mt-0.5">
          reduced {formatRelative(program.last_reduced_at)}
        </div>
      </div>

      {/* Action column */}
      <div className="flex items-center gap-2 self-center">
        {stsciHref && (
          <a
            href={stsciHref}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="text-text-secondary dark:text-slate-400 hover:text-primary transition-colors"
            title="View on STScI"
          >
            <ExternalLink className="w-4 h-4" />
          </a>
        )}
        <ArrowRight className="w-4 h-4 text-text-secondary dark:text-slate-400 group-hover:text-primary transition-colors" />
      </div>
    </div>
  );
};
