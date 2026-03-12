'use client';

import React, { useState, useEffect } from 'react';
import Link from 'next/link';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { Card } from '@/components/ui/Card';
import { getProgramsOverview } from '@/lib/actions/programs';
import type { ProgramOverview } from '@/lib/actions/programs';
import { LogIn, Loader2, Telescope, ExternalLink, Users, Hash } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';

function formatGratings(gratings: string[]): string {
  if (gratings.length === 0) return '';
  if (gratings.length <= 3) return gratings.join(', ');
  return `${gratings.slice(0, 3).join(', ')} +${gratings.length - 3}`;
}

function ProgramCard({ program }: { program: ProgramOverview }) {
  return (
    <Link href={`/programs/${program.program_id}`}>
      <Card hover className="p-5 h-full">
        <div className="flex items-start justify-between mb-3">
          <div>
            <h3 className="text-lg font-semibold text-text-primary dark:text-slate-100">
              {program.program_name || `Program ${program.program_id}`}
            </h3>
            <p className="text-sm text-text-secondary dark:text-slate-400">
              PID {program.program_id}
            </p>
          </div>
          <a
            href={`https://www.stsci.edu/cgi-bin/get-proposal-info?id=${program.program_id}&observatory=JWST`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-text-secondary dark:text-slate-400 hover:text-primary transition-colors flex-shrink-0"
            onClick={(e) => e.stopPropagation()}
            title="View on STScI"
          >
            <ExternalLink className="w-4 h-4" />
          </a>
        </div>

        {program.pi_name && (
          <div className="flex items-center gap-1.5 text-sm text-text-secondary dark:text-slate-400 mb-2">
            <Users className="w-3.5 h-3.5 flex-shrink-0" />
            <span>PI: {program.pi_name}</span>
          </div>
        )}

        {program.description && (
          <p className="text-sm text-text-secondary dark:text-slate-400 mb-4 line-clamp-2">
            {program.description}
          </p>
        )}

        <div className="flex flex-wrap gap-2 mt-auto">
          <span className="inline-flex items-center gap-1 px-2.5 py-1 bg-primary/10 text-primary rounded-full text-xs font-medium">
            <Hash className="w-3 h-3" />
            {program.object_count.toLocaleString()} objects
          </span>
          {program.gratings.length > 0 && (
            <span className="inline-flex items-center px-2.5 py-1 bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300 rounded-full text-xs font-medium">
              {formatGratings(program.gratings)}
            </span>
          )}
          {program.fields.length > 0 && (
            <span className="inline-flex items-center px-2.5 py-1 bg-emerald-50 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-300 rounded-full text-xs font-medium">
              {program.fields.length} {program.fields.length === 1 ? 'field' : 'fields'}
            </span>
          )}
        </div>
      </Card>
    </Link>
  );
}

export default function ProgramsPage() {
  const { user, loading: authLoading } = useAuth();
  const [programs, setPrograms] = useState<ProgramOverview[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (authLoading) return;

    async function fetchPrograms() {
      setLoading(true);
      setError(null);
      try {
        const result = await getProgramsOverview();
        if (result.error) {
          setError(result.error);
        } else {
          setPrograms(result.programs);
        }
      } catch {
        setError('Failed to fetch programs');
      } finally {
        setLoading(false);
      }
    }

    fetchPrograms();
  }, [authLoading, user]);

  // Show login prompt if not authenticated
  if (!authLoading && !user) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'Programs' },
          ]}
          className="mb-6"
        />

        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card dark:bg-slate-800 rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary dark:text-slate-400" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary dark:text-slate-100 mb-2">
            Sign in to view programs
          </h2>
          <p className="text-text-secondary dark:text-slate-400 mb-6 max-w-md">
            Access to JWST program information requires authentication. Please sign in with your
            CAMPFIRE account to browse programs.
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

  return (
    <div className="container mx-auto px-4 py-8">
      <Breadcrumbs
        items={[
          { label: 'CAMPFIRE', href: '/' },
          { label: 'Programs' },
        ]}
        className="mb-6"
      />

      {/* Page Header */}
      <div className="mb-8">
        <div className="flex items-center gap-3 mb-2">
          <Telescope className="w-8 h-8 text-primary" />
          <h1 className="text-2xl font-bold text-text-primary dark:text-slate-100">JWST Programs</h1>
        </div>
        <p className="text-text-secondary dark:text-slate-400">
          Browse JWST programs with data available in CAMPFIRE
        </p>
      </div>

      {/* Loading State */}
      {loading && (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
          <span className="ml-3 text-text-secondary dark:text-slate-400">Loading programs...</span>
        </div>
      )}

      {/* Error State */}
      {error && !loading && (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 mb-4">
          <p className="text-red-800 dark:text-red-400">{error}</p>
        </div>
      )}

      {/* Program Cards */}
      {!loading && !error && (
        <>
          {programs.length === 0 ? (
            <div className="text-center py-16 bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg">
              <Telescope className="w-12 h-12 text-text-secondary dark:text-slate-400 mx-auto mb-4" />
              <p className="text-text-secondary dark:text-slate-400">
                No programs available yet.
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {programs.map((program) => (
                <ProgramCard key={program.program_id} program={program} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
