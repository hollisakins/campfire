'use client';

import React, { Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Card } from '@/components/ui/Card';
import { Loader2, ArrowLeft } from 'lucide-react';
import {
  getNirspecNodGrid, getNirspecSourceReviews, saveBkgOverrides, setWebStuckShutters,
} from '@/lib/actions/nirspec-nods';
import {
  buildNodGrid, decodeSource, NOD_DETECTORS,
  nodKey, normalizeBkgOverrides,
} from '@/lib/nirspec-nods';
import {
  nextWebMark, slitletOrdinals, stuckOrdinals, type StuckShutterMark,
} from '@/lib/nirspec-shutters';
import { fetchJson } from '@/lib/fetch-json';
import { zscaleLimits, type StretchMode, type ColormapName } from '@/lib/fits';
import type { SpectrumExposure, NirspecStuckShutter } from '@/lib/types';
import type { StuckShuttersResponse } from '@/app/api/admin/nirspec/stuck-shutters/route';
import NodCell from '@/components/nirspec/NodCell';

// Per-(exposure_root) background overrides, held in memory with optimistic updates.
interface ReviewState {
  bkg_overrides: Record<string, number[]>;
}
const EMPTY_REVIEW: ReviewState = { bkg_overrides: {} };

type Marks = Map<string, StuckShutterMark>;
const NO_MARKS: Marks = new Map();

/** exposure_root shared by a nod-grid row's ≤2 detector cells (nrs1/nrs2 of one nod). */
function rowRoot(row: { cells: Record<string, SpectrumExposure | null> }): string | null {
  return (row.cells.nrs1 ?? row.cells.nrs2)?.exposure_root ?? null;
}

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

  // --- Background overrides (P6): nirspec_source_review, keyed by exposure_root ---
  const [reviews, setReviews] = useState<Map<string, ReviewState>>(new Map());
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    if (!decoded) return;
    let cancelled = false;
    getNirspecSourceReviews(decoded.observation, decoded.sourceId).then((res) => {
      if (cancelled) return;
      if (res.error) { setSaveError(res.error); return; }
      const m = new Map<string, ReviewState>();
      for (const r of res.reviews) {
        m.set(r.exposure_root, { bkg_overrides: r.bkg_overrides ?? {} });
      }
      setReviews(m);
    });
    return () => { cancelled = true; };
  }, [decoded?.observation, decoded?.sourceId]); // eslint-disable-line react-hooks/exhaustive-deps

  // --- Stuck shutters: nirspec_stuck_shutters, physical keys per exposure_root ---
  // The source's slitlet per root (deploy-populated from the metafile).
  const rootShutters = useMemo(() => {
    const m = new Map<string, string[] | null>();
    for (const r of rows) {
      if (!m.get(r.exposure_root)) m.set(r.exposure_root, r.msa_shutters?.length ? r.msa_shutters : null);
    }
    return m;
  }, [rows]);
  const roots = useMemo(() => [...rootShutters.keys()].sort(), [rootShutters]);

  const queryClient = useQueryClient();
  const stuckKey = useMemo(
    () => ['nirspec-stuck-shutters', decoded?.observation ?? '', roots.join(',')] as const,
    [decoded?.observation, roots]);
  const stuckQuery = useQuery({
    queryKey: stuckKey,
    queryFn: ({ signal }) => fetchJson<StuckShuttersResponse>(
      `/api/admin/nirspec/stuck-shutters?observation=${encodeURIComponent(decoded!.observation)}`
      + `&roots=${encodeURIComponent(roots.join(','))}`, { signal }),
    enabled: !!decoded && roots.length > 0,
  });

  const marks = useMemo(() => {
    const m = new Map<string, Marks>();
    for (const r of stuckQuery.data?.rows ?? []) {
      const per = m.get(r.exposure_root) ?? new Map();
      per.set(r.shutter, { origin: r.origin, stuck: r.stuck });
      m.set(r.exposure_root, per);
    }
    return m;
  }, [stuckQuery.data]);

  // Optimistic write of one root's web marks; the query is refetched afterwards.
  const setMark = useCallback((root: string, keys: string[], stuck: boolean | null) => {
    if (!decoded || keys.length === 0) return;
    queryClient.setQueryData<StuckShuttersResponse>(stuckKey, (prev) => {
      const kept = (prev?.rows ?? []).filter((r) => !(r.exposure_root === root && keys.includes(r.shutter)));
      if (stuck === null) return { rows: kept };
      const now = new Date().toISOString();
      const added: NirspecStuckShutter[] = keys.map((shutter) => ({
        id: -1, observation: decoded.observation, exposure_root: root, shutter, origin: 'web', stuck,
        note: null, created_at: now, updated_at: now, updated_by: null,
      }));
      return { rows: [...kept, ...added] };
    });
    setWebStuckShutters(decoded.observation, root, keys, stuck).then((res) => {
      if (res.error) setSaveError(res.error);
      queryClient.invalidateQueries({ queryKey: stuckKey });
    });
  }, [decoded?.observation, queryClient, stuckKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // A click on a cutout's shutter band: the ordinal's physical shutter(s) of that root.
  const handleToggleShutter = useCallback((root: string, ordinal: number) => {
    const keys = slitletOrdinals(rootShutters.get(root) ?? []).get(ordinal) ?? [];
    if (keys.length === 0) { setSaveError(`${root}: no shutter map (redeploy)`); return; }
    const rm = marks.get(root) ?? NO_MARKS;
    if (keys.some((k) => rm.get(k)?.origin === 'manual')) { setSaveError(`${keys.join(', ')} set locally (manual)`); return; }
    setMark(root, keys, keys.every((k) => rm.get(k)?.stuck) ? null : true);
  }, [rootShutters, marks, setMark]);

  const handleCycleShutter = useCallback((root: string, key: string) => {
    const next = nextWebMark((marks.get(root) ?? NO_MARKS).get(key));
    if (next !== undefined) setMark(root, [key], next);
  }, [marks, setMark]);

  // Candidate background nods for the whole source: distinct nod sequence numbers.
  const availableNods = useMemo(() => {
    const s = new Set<string>();
    for (const r of rows) s.add(nodKey(r.nod));
    return [...s].sort((a, b) => Number(a) - Number(b));
  }, [rows]);

  // Optimistic save of a root's background overrides.
  const persist = useCallback((root: string, next: ReviewState) => {
    if (!decoded) return;
    setReviews((prev) => new Map(prev).set(root, next));
    saveBkgOverrides(
      decoded.observation, root, decoded.sourceId,
      normalizeBkgOverrides(next.bkg_overrides),
    ).then((res) => { if (res.error) setSaveError(res.error); });
  }, [decoded?.observation, decoded?.sourceId]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleBkgChange = useCallback((root: string, nk: string, list: number[] | null) => {
    const cur = reviews.get(root) ?? EMPTY_REVIEW;
    const bkg = { ...cur.bkg_overrides };
    if (list === null) delete bkg[nk];
    else bkg[nk] = list;
    persist(root, { ...cur, bkg_overrides: bkg });
  }, [reviews, persist]);

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

      {(error || saveError) && (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 mb-4">
          <p className="text-red-800 dark:text-red-400">{error ?? saveError}</p>
        </div>
      )}

      {roots.length > 0 && (
        <Card className="p-3 mb-3">
          <div className="flex flex-col gap-1 text-xs">
            {roots.map((root) => (
              <SlitletShutters
                key={root}
                root={root}
                shutters={rootShutters.get(root) ?? null}
                marks={marks.get(root) ?? NO_MARKS}
                onCycle={(key) => handleCycleShutter(root, key)}
              />
            ))}
          </div>
        </Card>
      )}

      {grid.length === 0 ? (
        <p className="text-text-secondary text-sm">No spectrum-exposure rows for this source.</p>
      ) : (
        <Card className="overflow-x-auto p-3">
          <div className="min-w-fit">
            {/* header row: detector labels + flag column */}
            <div className="flex gap-2 mb-1 pl-16 text-xs font-medium text-text-secondary">
              {NOD_DETECTORS.map((d) => (
                <div key={d} className="w-64 text-center">{d}</div>
              ))}
              <div className="w-44 text-center">bkg override</div>
            </div>
            <p className="pl-16 mb-2 text-[10px] text-text-tertiary">
              Click a shutter band on a cutout to toggle it stuck. Set per-nod background below.
            </p>
            {grid.map((row) => {
              const root = rowRoot(row);
              const review = (root && reviews.get(root)) || EMPTY_REVIEW;
              const nk = nodKey(row.nod);
              return (
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
                        stuckList={root ? stuckOrdinals(rootShutters.get(root) ?? [], marks.get(root) ?? NO_MARKS) : []}
                        onToggleShutter={(ordinal) => { if (root) handleToggleShutter(root, ordinal); }}
                      />
                    </div>
                  ))}
                  <div className="w-44 h-32 border border-border rounded p-2 overflow-y-auto">
                    <BkgOverrideControl
                      disabled={!root}
                      availableNods={availableNods.filter((n) => n !== nk)}
                      value={review.bkg_overrides[nk]}
                      onChange={(list) => { if (root) handleBkgChange(root, nk, list); }}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        </Card>
      )}
    </div>
  );
}

/**
 * One exposure root's slitlet as physical shutters, in ordinal order (column
 * ascending). A click cycles the web mark: none → stuck → not stuck → none. Manual
 * marks (set in the local TOML, pushed on deploy) show dashed and are read-only.
 */
function SlitletShutters({
  root, shutters, marks, onCycle,
}: {
  root: string;
  shutters: string[] | null;
  marks: Marks;
  onCycle: (key: string) => void;
}) {
  const ordinals = shutters ? [...slitletOrdinals(shutters).entries()] : [];
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="w-44 font-mono text-text-secondary">{root}</span>
      {!shutters && <span className="text-text-tertiary">no shutter map</span>}
      {ordinals.flatMap(([ordinal, keys]) => keys.map((key) => {
        const m = marks.get(key);
        const manual = m?.origin === 'manual';
        const tone = !m ? 'border-border text-text-secondary'
          : m.stuck ? 'bg-red-500/20 border-red-500 text-red-600 dark:text-red-400'
            : 'border-emerald-500 text-emerald-600 dark:text-emerald-400';
        const state = !m ? '' : m.stuck ? ' stuck' : ' not stuck';
        return (
          <button
            key={key}
            onClick={() => onCycle(key)}
            disabled={manual}
            className={`px-1.5 py-0.5 rounded border font-mono ${tone} ${manual ? 'border-dashed cursor-not-allowed' : ''}`}
            title={`${ordinal}: ${key}${state}${manual ? ' (manual)' : m ? ' (web)' : ''}`}
          >
            {key}
          </button>
        );
      }))}
    </div>
  );
}

/**
 * Per-nod background-override control. Three states, mirroring the TOML semantics:
 *  - absent (undefined) → no override; pipeline uses its default background logic.
 *  - explicit list [1,2] → use only those nods as background for this nod.
 *  - empty list []       → exclude this nod entirely (CFP_BKG='excluded:override').
 */
function BkgOverrideControl({
  disabled, availableNods, value, onChange,
}: {
  disabled: boolean;
  availableNods: string[];
  value: number[] | undefined;
  onChange: (list: number[] | null) => void;
}) {
  const excluded = Array.isArray(value) && value.length === 0;
  const selected = new Set(value ?? []);
  const toggleNod = (n: number) => {
    const next = new Set(selected);
    if (next.has(n)) next.delete(n); else next.add(n);
    onChange(next.size ? [...next].sort((a, b) => a - b) : null);
  };
  return (
    <div className={`flex flex-col gap-1 text-[10px] ${disabled ? 'opacity-40 pointer-events-none' : ''}`}>
      <div className="flex flex-wrap gap-1">
        {availableNods.length === 0 && <span className="text-text-tertiary">no other nods</span>}
        {availableNods.map((n) => {
          const num = Number(n);
          const on = selected.has(num) && !excluded;
          return (
            <button
              key={n}
              onClick={() => toggleNod(num)}
              disabled={excluded}
              className={`px-1.5 py-0.5 rounded border ${on ? 'bg-primary text-on-primary border-primary' : 'border-border text-text-secondary'} ${excluded ? 'opacity-40' : ''}`}
              title={`Use nod ${n} as background`}
            >
              {n}
            </button>
          );
        })}
      </div>
      <div className="flex gap-1">
        <button
          onClick={() => onChange(excluded ? null : [])}
          className={`px-1.5 py-0.5 rounded border ${excluded ? 'bg-red-500 text-white border-red-500' : 'border-border text-text-secondary'}`}
          title="Exclude this nod (empty background list)"
        >
          excl
        </button>
        {(value !== undefined) && (
          <button
            onClick={() => onChange(null)}
            className="px-1.5 py-0.5 rounded border border-border text-text-tertiary"
            title="Clear override (use pipeline default)"
          >
            clear
          </button>
        )}
      </div>
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
