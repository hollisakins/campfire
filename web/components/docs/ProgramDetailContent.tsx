'use client';

import React from 'react';
import Link from 'next/link';
import { Badge } from '@/components/ui/Badge';
import { Card } from '@/components/ui/Card';
import { MarkdownRenderer } from '@/components/docs';
import { useProgramDetailQuery } from '@/lib/hooks/useProgramsQuery';
import { LogIn, Loader2, Telescope, ExternalLink, ArrowRight, AlertCircle, ChevronRight } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';

// Editorial content registry
import ember from '@/lib/docs/content/programs/7076.md';
const programContent: Record<number, string> = {
  7076: ember,
};

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

export default function ProgramDetailContent({ programId }: { programId: number }) {
  const { user, loading: authLoading } = useAuth();
  const { data, isLoading } = useProgramDetailQuery(programId, !authLoading && !!user);
  const program = data?.program ?? null;
  const observations = data?.observations ?? [];
  const error = data?.error ?? null;

  if (!authLoading && !user) {
    return (
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
    );
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
        <span className="ml-3 text-text-secondary dark:text-slate-400">Loading program...</span>
      </div>
    );
  }

  if (error || !program) {
    return (
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
          href="/docs/data-products/programs"
          className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
        >
          Back to Programs
        </Link>
      </div>
    );
  }

  const editorialContent = programContent[program.program_id];
  const stsciUrl = `https://www.stsci.edu/jwst-program-info/program/?program=${program.program_id}`;

  const programLabel = program.program_name
    ? `${program.program_name} (${program.program_id})`
    : `Program ${program.program_id}`;

  return (
    <div>
      {/* Breadcrumbs */}
      <nav className="flex items-center gap-1 text-sm text-text-secondary dark:text-slate-400 mb-6">
        <Link href="/docs" className="hover:text-primary transition-colors">Docs</Link>
        <ChevronRight className="w-4 h-4" />
        <Link href="/docs/data-products" className="hover:text-primary transition-colors">Data Products</Link>
        <ChevronRight className="w-4 h-4" />
        <Link href="/docs/data-products/programs" className="hover:text-primary transition-colors">NIRSpec Programs</Link>
        <ChevronRight className="w-4 h-4" />
        <span className="text-text-primary dark:text-slate-200">{programLabel}</span>
      </nav>

      {/* Header */}
      <div className="mb-8">
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center gap-3">
            <Telescope className="w-8 h-8 text-primary flex-shrink-0" />
            <div>
              <h1 className="text-2xl font-bold text-text-primary dark:text-slate-100">
                {program.program_name || `Program ${program.program_id}`}
              </h1>
              <p className="text-sm text-text-secondary dark:text-slate-400">
                PID {program.program_id}
                {program.cycle != null && <> &middot; Cycle {program.cycle}</>}
                {program.pi_name && <> &middot; PI: {program.pi_name}</>}
              </p>
            </div>
          </div>
          <a
            href={stsciUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-text-secondary dark:text-slate-400 hover:text-primary border border-border dark:border-slate-700 rounded-lg transition-colors"
          >
            <ExternalLink className="w-4 h-4" />
            STScI
          </a>
        </div>

        {program.description && (
          <p className="text-text-secondary dark:text-slate-400 mb-6">
            {program.description}
          </p>
        )}

        {/* Stat Badges */}
        <div className="flex flex-wrap gap-3">
          <Badge value={program.object_count.toLocaleString()} label="Objects" compact />
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
          <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100 mb-4">
            Observations
          </h2>
          <Card className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border dark:border-slate-700 bg-gray-50 dark:bg-slate-800/50">
                    <th className="text-left px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Observation</th>
                    <th className="text-left px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Field</th>
                    <th className="text-right px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Objects</th>
                    <th className="text-right px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Spectra</th>
                    <th className="text-right px-4 py-3 font-medium text-text-secondary dark:text-slate-400">Total Size</th>
                    <th className="text-right px-4 py-3 font-medium text-text-secondary dark:text-slate-400"></th>
                  </tr>
                </thead>
                <tbody>
                  {observations.map((obs) => (
                    <tr
                      key={obs.observation}
                      className="border-b border-border dark:border-slate-700 last:border-b-0 hover:bg-gray-50 dark:hover:bg-slate-800/30 transition-colors"
                    >
                      <td className="px-4 py-3 text-text-primary dark:text-slate-100 font-medium">
                        {obs.observation}
                      </td>
                      <td className="px-4 py-3 text-text-secondary dark:text-slate-400">
                        {obs.field}
                      </td>
                      <td className="px-4 py-3 text-right text-text-primary dark:text-slate-100">
                        {obs.object_count.toLocaleString()}
                      </td>
                      <td className="px-4 py-3 text-right text-text-primary dark:text-slate-100">
                        {obs.spectrum_count.toLocaleString()}
                      </td>
                      <td className="px-4 py-3 text-right text-text-secondary dark:text-slate-400">
                        {formatBytes(obs.total_size_bytes)}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <Link
                          href={`/spectra?programs=${program.program_id}&observations=${obs.observation}`}
                          className="text-primary hover:text-primary-hover transition-colors"
                          title="View spectra"
                        >
                          <ArrowRight className="w-4 h-4" />
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      )}

      {/* View All Link */}
      <div className="text-center">
        <Link
          href={`/spectra?programs=${program.program_id}`}
          className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
        >
          View all spectra from this program
          <ArrowRight className="w-4 h-4" />
        </Link>
      </div>
    </div>
  );
}
