'use client';

import React, { useState } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { Badge } from '@/components/ui/Badge';
import { Card } from '@/components/ui/Card';
import { MarkdownRenderer } from '@/components/docs';
import { useProgramDetailQuery } from '@/lib/hooks/useProgramsQuery';
import { LogIn, Loader2, Telescope, ExternalLink, ArrowRight, AlertCircle, ChevronRight, ChevronDown, Download } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';
import { pointingsToCsv, pointingsToDs9, flattenPointings, downloadBlob } from '@/lib/pointings';
import type { Pointing } from '@/lib/types';

// Editorial content registry — add imports as markdown files are authored
import ember from '@/lib/docs/content/programs/7076.md';
const programContent: Record<string, string> = {
  ember: ember,
};

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

export default function ProgramDetailPage() {
  const params = useParams();
  const programSlug = params.slug as string;
  const { user, loading: authLoading } = useAuth();
  const { data, isLoading } = useProgramDetailQuery(programSlug, !authLoading && !!user);
  const program = data?.program ?? null;
  const observations = data?.observations ?? [];
  const error = data?.error ?? null;
  const loading = isLoading;
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const toggleExpanded = (obsName: string) => {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(obsName)) next.delete(obsName);
      else next.add(obsName);
      return next;
    });
  };

  const totalPointings = observations.reduce((acc, o) => acc + (o.pointings?.length ?? 0), 0);

  const downloadAllCsv = () => {
    const rows = flattenPointings(observations);
    if (rows.length === 0) return;
    downloadBlob(pointingsToCsv(rows), `${programSlug}_pointings.csv`, 'text/csv');
  };

  const downloadAllDs9 = () => {
    const rows = flattenPointings(observations);
    if (rows.length === 0) return;
    downloadBlob(pointingsToDs9(rows), `${programSlug}_pointings.reg`, 'text/plain');
  };

  const downloadObsCsv = (obsName: string, pointings: Pointing[]) => {
    const rows = flattenPointings([{ observation: obsName, pointings }]);
    downloadBlob(pointingsToCsv(rows), `${obsName}_pointings.csv`, 'text/csv');
  };

  const downloadObsDs9 = (obsName: string, pointings: Pointing[]) => {
    const rows = flattenPointings([{ observation: obsName, pointings }]);
    downloadBlob(pointingsToDs9(rows), `${obsName}_pointings.reg`, 'text/plain');
  };

  // Auth gate
  if (!authLoading && !user) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'NIRSpec', href: '/nirspec' },
          { label: 'Programs', href: '/nirspec/programs' },
            { label: programSlug },
          ]}
          className="mb-6"
        />
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card dark:bg-slate-800 rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary dark:text-slate-400" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary dark:text-slate-100 mb-2">
            Sign in to view program details
          </h2>
          <p className="text-text-secondary dark:text-slate-400 mb-6 max-w-md">
            Access to program information requires authentication.
          </p>
          <Link
            href="/login"
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
          >
            <LogIn className="w-5 h-5" />
            Sign In
          </Link>
        </div>
      </div>
    );
  }

  // Loading
  if (loading) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'NIRSpec', href: '/nirspec' },
          { label: 'Programs', href: '/nirspec/programs' },
            { label: '...' },
          ]}
          className="mb-6"
        />
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
          <span className="ml-3 text-text-secondary dark:text-slate-400">Loading program...</span>
        </div>
      </div>
    );
  }

  // Error / not found
  if (error || !program) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'NIRSpec', href: '/nirspec' },
          { label: 'Programs', href: '/nirspec/programs' },
            { label: programSlug },
          ]}
          className="mb-6"
        />
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-red-100 dark:bg-red-950 rounded-full flex items-center justify-center mb-4">
            <AlertCircle className="w-8 h-8 text-red-600 dark:text-red-400" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary dark:text-slate-100 mb-2">
            {error === 'Access denied' ? 'Access Denied' : 'Program Not Found'}
          </h2>
          <p className="text-text-secondary dark:text-slate-400 mb-6">
            {error === 'Access denied'
              ? 'You do not have access to this program.'
              : 'The program you are looking for does not exist.'}
          </p>
          <Link
            href="/nirspec/programs"
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
          >
            Back to Programs
          </Link>
        </div>
      </div>
    );
  }

  const editorialContent = programContent[program.slug];
  const firstPid = program.jwst_pids?.[0];

  return (
    <div className="container mx-auto px-4 py-8 max-w-5xl">
      <Breadcrumbs
        items={[
          { label: 'CAMPFIRE', href: '/' },
          { label: 'NIRSpec', href: '/nirspec' },
          { label: 'Programs', href: '/nirspec/programs' },
          { label: program.program_name || program.slug },
        ]}
        className="mb-6"
      />

      {/* Header */}
      <div className="mb-8">
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center gap-3">
            <Telescope className="w-8 h-8 text-primary flex-shrink-0" />
            <div>
              <h1 className="text-2xl font-bold text-text-primary dark:text-slate-100">
                {program.program_name || program.slug}
              </h1>
              <p className="text-sm text-text-secondary dark:text-slate-400">
                {program.jwst_pids && program.jwst_pids.length > 0 && (
                  <>PID{program.jwst_pids.length > 1 ? 's' : ''} {program.jwst_pids.join(', ')}</>
                )}
                {program.cycle != null && <> &middot; Cycle {program.cycle}</>}
                {program.pi_name && <> &middot; PI: {program.pi_name}</>}
              </p>
            </div>
          </div>
          {program.jwst_pids && program.jwst_pids.length > 0 && (
            <div className="flex gap-2">
              {program.jwst_pids.map((pid) => (
                <a
                  key={pid}
                  href={`https://www.stsci.edu/jwst-program-info/program/?program=${pid}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-text-secondary dark:text-slate-400 hover:text-primary border border-border dark:border-slate-700 rounded-lg transition-colors"
                >
                  <ExternalLink className="w-4 h-4" />
                  {program.jwst_pids.length > 1 ? `PID ${pid}` : 'STScI'}
                </a>
              ))}
            </div>
          )}
        </div>

        {program.description && (
          <p className="text-text-secondary dark:text-slate-400 mb-6">
            {program.description}
          </p>
        )}

        {/* Stat Badges */}
        <div className="flex flex-wrap gap-3">
          <Badge value={program.target_count.toLocaleString()} label="Targets" compact />
          <Badge value={program.gratings.length} label="Gratings" compact />
          <Badge value={program.fields.length} label="Fields" compact />
          <Badge value={program.observations.length} label="Observations" compact />
        </div>
      </div>

      {/* Editorial Content */}
      {editorialContent && (
        <Card className="p-6 mb-8">
          <MarkdownRenderer content={editorialContent} />
        </Card>
      )}

      {/* Observations Table */}
      {observations.length > 0 && (
        <div className="mb-8">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100">
              Observations
            </h2>
            {totalPointings > 0 && (
              <div className="flex gap-2 text-xs">
                <button
                  onClick={downloadAllCsv}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-text-secondary dark:text-slate-400 hover:text-primary border border-border dark:border-slate-700 rounded transition-colors"
                  title={`Download all ${totalPointings} pointings as CSV`}
                >
                  <Download className="w-3.5 h-3.5" />
                  Pointings CSV
                </button>
                <button
                  onClick={downloadAllDs9}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-text-secondary dark:text-slate-400 hover:text-primary border border-border dark:border-slate-700 rounded transition-colors"
                  title={`Download all ${totalPointings} pointings as DS9 region file`}
                >
                  <Download className="w-3.5 h-3.5" />
                  Pointings DS9
                </button>
              </div>
            )}
          </div>
          <Card className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border dark:border-slate-700 bg-gray-50 dark:bg-slate-800/50">
                    <th className="px-2 py-3 w-8"></th>
                    <th className="text-left px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Observation</th>
                    <th className="text-left px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Field</th>
                    <th className="text-right px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Pointings</th>
                    <th className="text-right px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Targets</th>
                    <th className="text-right px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Spectra</th>
                    <th className="text-right px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Total Size</th>
                    <th className="text-right px-4 py-3 font-medium text-text-secondary dark:text-slate-400"></th>
                  </tr>
                </thead>
                <tbody>
                  {observations.map((obs) => {
                    const hasPointings = obs.pointings != null && obs.pointings.length > 0;
                    const isExpanded = expanded.has(obs.observation);
                    return (
                      <React.Fragment key={obs.observation}>
                        <tr
                          className="border-b border-border dark:border-slate-700 last:border-b-0 hover:bg-gray-50 dark:hover:bg-slate-800/30 transition-colors"
                        >
                          <td className="px-2 py-3 text-center">
                            {hasPointings ? (
                              <button
                                onClick={() => toggleExpanded(obs.observation)}
                                className="text-text-secondary hover:text-primary transition-colors"
                                title={isExpanded ? 'Collapse pointings' : 'Show pointings'}
                              >
                                {isExpanded ? (
                                  <ChevronDown className="w-4 h-4" />
                                ) : (
                                  <ChevronRight className="w-4 h-4" />
                                )}
                              </button>
                            ) : (
                              <span className="text-text-secondary/40">—</span>
                            )}
                          </td>
                          <td className="px-4 py-3 text-text-primary dark:text-slate-100 font-medium">
                            {obs.observation}
                          </td>
                          <td className="px-4 py-3 text-text-secondary dark:text-slate-400">
                            {obs.field}
                          </td>
                          <td className="px-4 py-3 text-right text-text-primary dark:text-slate-100">
                            {hasPointings ? obs.pointings!.length : '—'}
                          </td>
                          <td className="px-4 py-3 text-right text-text-primary dark:text-slate-100">
                            {obs.target_count.toLocaleString()}
                          </td>
                          <td className="px-4 py-3 text-right text-text-primary dark:text-slate-100">
                            {obs.spectrum_count.toLocaleString()}
                          </td>
                          <td className="px-4 py-3 text-right text-text-secondary dark:text-slate-400">
                            {formatBytes(obs.total_size_bytes)}
                          </td>
                          <td className="px-4 py-3 text-right">
                            <Link
                              href={`/nirspec?programs=${program.slug}&observations=${obs.observation}`}
                              className="text-primary hover:text-primary-hover transition-colors"
                              title="View spectra"
                            >
                              <ArrowRight className="w-4 h-4" />
                            </Link>
                          </td>
                        </tr>
                        {isExpanded && hasPointings && (
                          <tr className="border-b border-border dark:border-slate-700 bg-gray-50/50 dark:bg-slate-800/20">
                            <td colSpan={8} className="px-4 py-3">
                              <PointingsSubtable
                                obsName={obs.observation}
                                pointings={obs.pointings!}
                                onDownloadCsv={() => downloadObsCsv(obs.observation, obs.pointings!)}
                                onDownloadDs9={() => downloadObsDs9(obs.observation, obs.pointings!)}
                              />
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      )}

      {/* View All Link */}
      <div className="text-center">
        <Link
          href={`/nirspec?programs=${program.slug}`}
          className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
        >
          View all spectra from this program
          <ArrowRight className="w-4 h-4" />
        </Link>
      </div>
    </div>
  );
}

interface PointingsSubtableProps {
  obsName: string;
  pointings: Pointing[];
  onDownloadCsv: () => void;
  onDownloadDs9: () => void;
}

function PointingsSubtable({ pointings, onDownloadCsv, onDownloadDs9 }: PointingsSubtableProps) {
  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wide">
          Pointings ({pointings.length})
        </span>
        <div className="flex gap-2">
          <button
            onClick={onDownloadCsv}
            className="inline-flex items-center gap-1 px-2 py-1 text-xs text-text-secondary dark:text-slate-400 hover:text-primary border border-border dark:border-slate-700 rounded transition-colors"
            title="Download these pointings as CSV"
          >
            <Download className="w-3 h-3" />
            CSV
          </button>
          <button
            onClick={onDownloadDs9}
            className="inline-flex items-center gap-1 px-2 py-1 text-xs text-text-secondary dark:text-slate-400 hover:text-primary border border-border dark:border-slate-700 rounded transition-colors"
            title="Download these pointings as DS9 region file"
          >
            <Download className="w-3 h-3" />
            DS9
          </button>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border/60 dark:border-slate-700/60 text-text-secondary dark:text-slate-400">
              <th className="text-left px-2 py-2 font-medium">MSA Design</th>
              <th className="text-right px-2 py-2 font-medium">RA</th>
              <th className="text-right px-2 py-2 font-medium">Dec</th>
              <th className="text-right px-2 py-2 font-medium">PA</th>
              <th className="text-left px-2 py-2 font-medium">Gratings</th>
              <th className="text-right px-2 py-2 font-medium">Dithers</th>
              <th className="text-right px-2 py-2 font-medium">Exptime</th>
              <th className="text-left px-2 py-2 font-medium">Date</th>
            </tr>
          </thead>
          <tbody>
            {pointings.map((p) => (
              <tr key={p.msametid} className="border-b border-border/30 dark:border-slate-700/30 last:border-b-0">
                <td className="px-2 py-1.5 text-text-primary dark:text-slate-100 font-mono">
                  {p.msametid}
                </td>
                <td className="px-2 py-1.5 text-right text-text-primary dark:text-slate-100 font-mono">
                  {p.ra_center.toFixed(5)}
                </td>
                <td className="px-2 py-1.5 text-right text-text-primary dark:text-slate-100 font-mono">
                  {p.dec_center.toFixed(5)}
                </td>
                <td className="px-2 py-1.5 text-right text-text-primary dark:text-slate-100 font-mono">
                  {p.pa_aper.toFixed(2)}
                </td>
                <td className="px-2 py-1.5 text-text-secondary dark:text-slate-400">
                  {p.gratings.join(', ')}
                </td>
                <td className="px-2 py-1.5 text-right text-text-primary dark:text-slate-100">
                  {p.n_dithers}
                </td>
                <td className="px-2 py-1.5 text-right text-text-secondary dark:text-slate-400">
                  {p.exptime_total.toFixed(0)}s
                </td>
                <td className="px-2 py-1.5 text-text-secondary dark:text-slate-400">
                  {p.date_obs_start}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
