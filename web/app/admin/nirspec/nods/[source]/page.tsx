'use client';

import React, { Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import { Card } from '@/components/ui/Card';
import { Loader2, ArrowLeft } from 'lucide-react';
import { getNirspecNodGrid } from '@/lib/actions/nirspec-nods';
import { buildNodGrid, decodeSource, NOD_DETECTORS } from '@/lib/nirspec-nods';
import { zscaleLimits, type StretchMode, type ColormapName } from '@/lib/fits';
import type { SpectrumExposure } from '@/lib/types';
import NodCell from '@/components/nirspec/NodCell';

function sharedRange(arrays: Float32Array[]): [number, number] | null {
  if (arrays.length === 0) return null;
  const total = arrays.reduce((n, a) => n + a.length, 0);
  const concat = new Float32Array(total);
  let o = 0;
  for (const a of arrays) { concat.set(a, o); o += a.length; }
  return zscaleLimits(concat);
}

function NodGridInner() {
  const params = useParams();
  const decoded = decodeSource(String(params.source));

  const [rows, setRows] = useState<SpectrumExposure[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [bkgsub, setBkgsub] = useState(false);
  const [stretch, setStretch] = useState<StretchMode>('asinh');
  const colormap: ColormapName = 'gray';

  const [dataById, setDataById] = useState<Map<number, Float32Array>>(new Map());
  const [settledIds, setSettledIds] = useState<Set<number>>(new Set());
  const [range, setRange] = useState<[number, number] | null>(null);

  useEffect(() => {
    if (!decoded) { setError('Bad source id'); setLoading(false); return; }
    let cancelled = false;
    getNirspecNodGrid(decoded.observation, decoded.sourceId).then((res) => {
      if (cancelled) return;
      if (res.error) setError(res.error);
      else setRows(res.rows);
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [decoded?.observation, decoded?.sourceId]); // eslint-disable-line react-hooks/exhaustive-deps

  const grid = useMemo(() => buildNodGrid(rows), [rows]);
  const multiGroup = useMemo(
    () => new Set(grid.map((r) => r.exp_group ?? -1)).size > 1, [grid]);

  // Reset the shared-stretch accumulator whenever the view (bkgsub) changes — the
  // cells refetch the other HDU, so old pixels are stale.
  useEffect(() => {
    setDataById(new Map());
    setSettledIds(new Set());
    setRange(null);
  }, [bkgsub, rows]);

  const expectedIds = useMemo(
    () => rows.filter((r) => r.storage_key).map((r) => r.id), [rows]);

  const onData = useCallback((id: number, data: Float32Array) => {
    setDataById((prev) => new Map(prev).set(id, data));
  }, []);

  const onSettled = useCallback((id: number) => {
    setSettledIds((prev) => (prev.has(id) ? prev : new Set(prev).add(id)));
  }, []);

  // Once every expected cell has SETTLED (succeeded, or hit the absent/error
  // fallback — absent cells never produce pixels), compute one shared ZScale from
  // whichever cells did produce data. Gating on "settled" not "succeeded" is what
  // keeps a single absent cell from leaving the whole grid unpainted.
  useEffect(() => {
    if (expectedIds.length === 0) return;
    if (!expectedIds.every((id) => settledIds.has(id))) return;
    const arrays = expectedIds.map((id) => dataById.get(id)).filter((a): a is Float32Array => !!a);
    if (arrays.length > 0) setRange(sharedRange(arrays));
  }, [settledIds, dataById, expectedIds]);

  if (loading) {
    return <div className="flex items-center justify-center py-16"><Loader2 className="w-8 h-8 animate-spin text-primary" /></div>;
  }

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <Link href="/admin/nirspec/nods" className="text-text-secondary hover:text-text-primary" title="Back">
          <ArrowLeft className="w-5 h-5" />
        </Link>
        <div className="flex-1 min-w-0">
          <h1 className="text-xl font-semibold font-mono text-text-primary truncate">
            {decoded?.observation} · source {decoded?.sourceId}
          </h1>
          <p className="text-sm text-text-secondary">
            Live nods view — S2D cutouts grouped by (exp_group, nod) × detector.
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs">
          <button
            onClick={() => setBkgsub((v) => !v)}
            className={`px-2 py-1 rounded border border-border ${bkgsub ? 'bg-primary text-on-primary' : 'text-text-secondary'}`}
            title="Toggle background-subtracted view (S2D_BKGSUB_SCI)"
          >
            {bkgsub ? 'bkgsub' : 'cal'}
          </button>
          <select
            value={stretch}
            onChange={(e) => setStretch(e.target.value as StretchMode)}
            className="text-xs border border-border rounded px-2 py-1 bg-card text-text-primary"
          >
            {(['linear', 'sqrt', 'log', 'asinh'] as StretchMode[]).map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </div>
      </div>

      {error && (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 mb-4">
          <p className="text-red-800 dark:text-red-400">{error}</p>
        </div>
      )}

      {grid.length === 0 ? (
        <p className="text-text-secondary text-sm">No spectrum-exposure rows for this source.</p>
      ) : (
        <Card className="overflow-x-auto p-3">
          <div className="min-w-fit">
            {/* header row: detector labels */}
            <div className="flex gap-2 mb-1 pl-16 text-xs font-medium text-text-secondary">
              {NOD_DETECTORS.map((d) => (
                <div key={d} className="w-64 text-center">{d}</div>
              ))}
            </div>
            {grid.map((row) => (
              <div key={`${row.exp_group}-${row.nod}`} className="flex items-stretch gap-2 mb-2 h-32">
                <div className="w-16 flex-shrink-0 flex items-center text-xs font-mono text-text-secondary">
                  {multiGroup ? row.label : row.nod}
                </div>
                {NOD_DETECTORS.map((d) => (
                  <div key={d} className="w-64 h-32 border border-border rounded">
                    <NodCell
                      exposure={row.cells[d]}
                      range={range}
                      stretch={stretch}
                      colormap={colormap}
                      bkgsub={bkgsub}
                      onData={onData}
                      onSettled={onSettled}
                    />
                  </div>
                ))}
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}

export default function NodGridPage() {
  return (
    <Suspense fallback={<div className="flex items-center justify-center py-16"><Loader2 className="w-8 h-8 animate-spin text-primary" /></div>}>
      <NodGridInner />
    </Suspense>
  );
}
