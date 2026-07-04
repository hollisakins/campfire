'use client';

import React, { Suspense } from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { Card } from '@/components/ui/Card';
import { Loader2, ChevronRight } from 'lucide-react';
import { listNirspecNodSources } from '@/lib/actions/nirspec-nods';
import { encodeSource } from '@/lib/nirspec-nods';

function NodsIndexInner() {
  const { data, isLoading } = useQuery({
    queryKey: ['admin-nirspec-nod-sources'],
    queryFn: listNirspecNodSources,
    staleTime: 60_000,
  });

  const sources = data?.sources ?? [];

  return (
    <div>
      <h1 className="text-2xl font-semibold text-text-primary mb-2">NIRSpec Nods</h1>
      <p className="text-text-secondary text-sm mb-6">
        Per-source live nods view — the browser equivalent of the pipeline&rsquo;s
        <span className="font-mono"> *_nods.pdf</span>, from the deployed S2D cutouts.
        Pick a source to see its <span className="font-mono">(exp_group, nod) × detector</span> grid.
      </p>

      {data?.error && (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 mb-6">
          <p className="text-red-800 dark:text-red-400">{data.error}</p>
        </div>
      )}

      {isLoading ? (
        <div className="flex items-center justify-center py-16"><Loader2 className="w-8 h-8 animate-spin text-primary" /></div>
      ) : sources.length === 0 ? (
        <p className="text-text-secondary text-sm">
          No spectrum-exposure grid rows yet. Deploy an observation with{' '}
          <span className="font-mono">campfire deploy --obs &lt;obs&gt;</span> to populate the grid.
        </p>
      ) : (
        <Card className="overflow-hidden divide-y divide-border">
          {sources.map((s) => (
            <Link
              key={`${s.observation}-${s.source_id}`}
              href={`/admin/nirspec/nods/${encodeSource(s.observation, s.source_id)}`}
              className="flex items-center gap-3 px-4 py-3 hover:bg-card-hover transition-colors"
            >
              <div className="flex-1 min-w-0">
                <div className="font-mono text-sm text-text-primary truncate">
                  {s.observation} · source {s.source_id}
                </div>
                <div className="text-xs text-text-secondary">
                  {s.exposureRootCount} exposure{s.exposureRootCount === 1 ? '' : 's'} ·{' '}
                  {s.cellCount} cell{s.cellCount === 1 ? '' : 's'} ·{' '}
                  {s.detectors.join(', ') || '—'}
                  {s.grating ? ` · ${s.grating}` : ''}
                </div>
              </div>
              <ChevronRight className="w-4 h-4 text-text-secondary" />
            </Link>
          ))}
        </Card>
      )}
    </div>
  );
}

export default function NodsIndexPage() {
  return (
    <Suspense fallback={<div className="flex items-center justify-center py-16"><Loader2 className="w-8 h-8 animate-spin text-primary" /></div>}>
      <NodsIndexInner />
    </Suspense>
  );
}
