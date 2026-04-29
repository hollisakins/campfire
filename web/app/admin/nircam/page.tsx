'use client';

import React, { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Loader2, RefreshCw, ChevronRight } from 'lucide-react';
import {
  getNircamExposures,
  getReductionProgress,
  getExposureFilterOptions,
  type ReductionProgress,
} from '@/lib/actions/nircam-exposures';
import type { NircamExposure } from '@/lib/types';

// ---------------------------------------------------------------------------
// Status badge helpers
// ---------------------------------------------------------------------------

function ReviewBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    pending: 'bg-yellow-100 dark:bg-yellow-900 text-yellow-800 dark:text-yellow-300',
    approved: 'bg-green-100 dark:bg-green-900 text-green-800 dark:text-green-300',
    excluded: 'bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-300',
  };
  return (
    <span className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full ${colors[status] || 'bg-gray-100 dark:bg-slate-700 text-gray-800 dark:text-slate-300'}`}>
      {status}
    </span>
  );
}

function ActionBadge({ status, label }: { status: string; label: string }) {
  if (status === 'none') return <span className="text-xs text-text-secondary dark:text-slate-500">&mdash;</span>;
  const colors: Record<string, string> = {
    needed: 'bg-orange-100 dark:bg-orange-900 text-orange-800 dark:text-orange-300',
    done: 'bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-300',
  };
  return (
    <span className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full ${colors[status] || ''}`}>
      {label}: {status}
    </span>
  );
}

function StageBadge({ stage }: { stage: string }) {
  const colors: Record<string, string> = {
    uncal: 'bg-gray-100 dark:bg-slate-700 text-gray-600 dark:text-slate-400',
    rate: 'bg-blue-50 dark:bg-blue-950 text-blue-600 dark:text-blue-400',
    cal: 'bg-indigo-100 dark:bg-indigo-900 text-indigo-700 dark:text-indigo-300',
    jhat: 'bg-purple-100 dark:bg-purple-900 text-purple-700 dark:text-purple-300',
    crf: 'bg-green-100 dark:bg-green-900 text-green-700 dark:text-green-300',
  };
  return (
    <span className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full ${colors[stage] || ''}`}>
      {stage}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Progress table
// ---------------------------------------------------------------------------

function ProgressTable({ progress }: { progress: ReductionProgress[] }) {
  if (progress.length === 0) {
    return (
      <p className="text-sm text-text-secondary dark:text-slate-400 py-4">
        No reduction data available. Deploy exposures with <code className="text-xs bg-card dark:bg-slate-700 px-1 py-0.5 rounded">campfire deploy nircam</code>.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="border-b border-border dark:border-slate-700">
          <tr>
            <th className="px-3 py-2 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">Field</th>
            <th className="px-3 py-2 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">Filter</th>
            <th className="px-3 py-2 text-right text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">Total</th>
            <th className="px-3 py-2 text-right text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">rate</th>
            <th className="px-3 py-2 text-right text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">cal</th>
            <th className="px-3 py-2 text-right text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">jhat</th>
            <th className="px-3 py-2 text-right text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">crf</th>
            <th className="px-3 py-2 text-right text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">Pending</th>
            <th className="px-3 py-2 text-right text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">Masking</th>
            <th className="px-3 py-2 text-right text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">Correction</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border dark:divide-slate-700">
          {progress.map((row) => (
            <tr key={`${row.field}-${row.filter}`} className="hover:bg-card/50 dark:hover:bg-slate-700/50">
              <td className="px-3 py-2 font-medium text-text-primary dark:text-slate-100">{row.field}</td>
              <td className="px-3 py-2 text-text-primary dark:text-slate-100">{row.filter}</td>
              <td className="px-3 py-2 text-right text-text-primary dark:text-slate-100">{row.total}</td>
              <td className="px-3 py-2 text-right text-text-secondary dark:text-slate-400">{row.at_rate || '—'}</td>
              <td className="px-3 py-2 text-right text-text-secondary dark:text-slate-400">{row.at_cal || '—'}</td>
              <td className="px-3 py-2 text-right text-text-secondary dark:text-slate-400">{row.at_jhat || '—'}</td>
              <td className="px-3 py-2 text-right text-text-secondary dark:text-slate-400">{row.at_crf || '—'}</td>
              <td className="px-3 py-2 text-right">
                {row.pending_review > 0 ? (
                  <span className="text-yellow-600 dark:text-yellow-400 font-medium">{row.pending_review}</span>
                ) : (
                  <span className="text-text-secondary dark:text-slate-500">0</span>
                )}
              </td>
              <td className="px-3 py-2 text-right">
                {row.needs_masking > 0 ? (
                  <span className="text-orange-600 dark:text-orange-400 font-medium">{row.needs_masking}</span>
                ) : (
                  <span className="text-text-secondary dark:text-slate-500">0</span>
                )}
              </td>
              <td className="px-3 py-2 text-right">
                {row.needs_correction > 0 ? (
                  <span className="text-orange-600 dark:text-orange-400 font-medium">{row.needs_correction}</span>
                ) : (
                  <span className="text-text-secondary dark:text-slate-500">0</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function AdminNircamPage() {
  const [progress, setProgress] = useState<ReductionProgress[]>([]);
  const [exposures, setExposures] = useState<NircamExposure[]>([]);
  const [filterOptions, setFilterOptions] = useState<{ fields: string[]; filters: string[]; stages: string[] }>({
    fields: [], filters: [], stages: [],
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [selectedField, setSelectedField] = useState<string>('');
  const [selectedFilter, setSelectedFilter] = useState<string>('');
  const [selectedReview, setSelectedReview] = useState<string>('');
  const [selectedStage, setSelectedStage] = useState<string>('');

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const [progressResult, exposuresResult, optionsResult] = await Promise.all([
        getReductionProgress(),
        getNircamExposures({
          field: selectedField || undefined,
          filter: selectedFilter || undefined,
          reviewStatus: selectedReview || undefined,
          stage: selectedStage || undefined,
        }),
        getExposureFilterOptions(),
      ]);

      if (progressResult.error) throw new Error(progressResult.error);
      if (exposuresResult.error) throw new Error(exposuresResult.error);

      setProgress(progressResult.progress);
      setExposures(exposuresResult.exposures);
      if (!optionsResult.error) {
        setFilterOptions(optionsResult);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load data');
    } finally {
      setLoading(false);
    }
  }, [selectedField, selectedFilter, selectedReview, selectedStage]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  if (loading && exposures.length === 0) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-semibold text-text-primary dark:text-slate-100">NIRCam Reductions</h1>
        <Button variant="secondary" size="sm" onClick={fetchData} disabled={loading}>
          <RefreshCw className={`w-4 h-4 mr-2 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </Button>
      </div>

      {error && (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 mb-6">
          <p className="text-red-800 dark:text-red-400">{error}</p>
        </div>
      )}

      {/* Progress summary */}
      <Card className="mb-6 overflow-hidden">
        <div className="px-4 py-3 border-b border-border dark:border-slate-700">
          <h2 className="text-sm font-medium text-text-primary dark:text-slate-100 uppercase tracking-wider">
            Reduction Progress
          </h2>
        </div>
        <ProgressTable progress={progress} />
      </Card>

      {/* Filter bar */}
      <div className="flex flex-wrap gap-3 mb-4">
        <select
          value={selectedField}
          onChange={(e) => setSelectedField(e.target.value)}
          className="text-sm border border-border dark:border-slate-600 rounded-lg px-3 py-1.5 bg-white dark:bg-slate-800 text-text-primary dark:text-slate-100"
        >
          <option value="">All fields</option>
          {filterOptions.fields.map(f => <option key={f} value={f}>{f}</option>)}
        </select>
        <select
          value={selectedFilter}
          onChange={(e) => setSelectedFilter(e.target.value)}
          className="text-sm border border-border dark:border-slate-600 rounded-lg px-3 py-1.5 bg-white dark:bg-slate-800 text-text-primary dark:text-slate-100"
        >
          <option value="">All filters</option>
          {filterOptions.filters.map(f => <option key={f} value={f}>{f}</option>)}
        </select>
        <select
          value={selectedStage}
          onChange={(e) => setSelectedStage(e.target.value)}
          className="text-sm border border-border dark:border-slate-600 rounded-lg px-3 py-1.5 bg-white dark:bg-slate-800 text-text-primary dark:text-slate-100"
        >
          <option value="">All stages</option>
          {filterOptions.stages.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <select
          value={selectedReview}
          onChange={(e) => setSelectedReview(e.target.value)}
          className="text-sm border border-border dark:border-slate-600 rounded-lg px-3 py-1.5 bg-white dark:bg-slate-800 text-text-primary dark:text-slate-100"
        >
          <option value="">All review statuses</option>
          <option value="pending">Pending</option>
          <option value="approved">Approved</option>
          <option value="excluded">Excluded</option>
        </select>
        {(selectedField || selectedFilter || selectedStage || selectedReview) && (
          <button
            onClick={() => { setSelectedField(''); setSelectedFilter(''); setSelectedStage(''); setSelectedReview(''); }}
            className="text-sm text-primary hover:underline"
          >
            Clear filters
          </button>
        )}
      </div>

      {/* Exposure table */}
      <Card className="overflow-hidden">
        <table className="w-full">
          <thead className="bg-card dark:bg-slate-800 border-b border-border dark:border-slate-700">
            <tr>
              <th className="px-4 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">Filename</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">Filter</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">Detector</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">Stage</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">Review</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">Masking</th>
              <th className="px-4 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase">Correction</th>
              <th className="px-4 py-3"></th>
            </tr>
          </thead>
          <tbody className="bg-white dark:bg-slate-800 divide-y divide-border dark:divide-slate-700">
            {exposures.length === 0 ? (
              <tr>
                <td colSpan={8} className="px-4 py-12 text-center text-text-secondary dark:text-slate-400">
                  No exposures found.
                </td>
              </tr>
            ) : (
              exposures.map((exp) => (
                <tr key={exp.id} className="hover:bg-card/50 dark:hover:bg-slate-700/50">
                  <td className="px-4 py-3">
                    <Link
                      href={`/admin/nircam/${exp.id}`}
                      className="text-sm font-mono text-primary hover:underline"
                    >
                      {exp.filename}
                    </Link>
                  </td>
                  <td className="px-4 py-3 text-sm text-text-primary dark:text-slate-100">{exp.filter}</td>
                  <td className="px-4 py-3 text-sm text-text-secondary dark:text-slate-400">{exp.detector}</td>
                  <td className="px-4 py-3"><StageBadge stage={exp.stage} /></td>
                  <td className="px-4 py-3"><ReviewBadge status={exp.review_status} /></td>
                  <td className="px-4 py-3"><ActionBadge status={exp.masking} label="mask" /></td>
                  <td className="px-4 py-3"><ActionBadge status={exp.correction} label="corr" /></td>
                  <td className="px-4 py-3 text-right">
                    <Link href={`/admin/nircam/${exp.id}`}>
                      <ChevronRight className="w-4 h-4 text-text-secondary dark:text-slate-400" />
                    </Link>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </Card>

      <p className="text-xs text-text-secondary dark:text-slate-500 mt-2">
        {exposures.length} exposure{exposures.length !== 1 ? 's' : ''}
      </p>
    </div>
  );
}
