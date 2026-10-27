'use client';

/**
 * The Filters modal (Catalog Read Model phase 3b, design doc Figures 6 and
 * 7): the two nouns as cards over the tree in `?q=`. Objects view: the
 * object's properties and photometry at the top, one card per spectrum.
 * Spectra view: the spectrum's own at the top and one "its object" section.
 * Edits go to a draft; Done applies it, Escape and Cancel discard it. The
 * same dialog serves the table page and the map.
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Check, ClipboardPaste, Copy, X } from 'lucide-react';
import type { FilterOption } from '@/components/ui/FilterChip';
import { DEFAULT_FILTERS, type FilterOptions } from '@/lib/actions/filter-params';
import type { ViewMode } from '@/lib/actions/spectra-types';
import { defaultSortColumn } from '@/lib/actions/spectra-types';
import type { Program } from '@/lib/types';
import { useAvailableListsQuery } from '@/lib/hooks/useAvailableListsQuery';
import { useDrTag } from '@/lib/dr/client';
import { filtersToURLParams } from '@/lib/utils/url-params';
import type { CatalogQueryCountResponse } from '@/app/api/catalog-query/count/route';
import { rootOf } from '@/lib/catalog-query/legacy';
import { encodeConditions, parseConditions, type TreeNode } from '@/lib/catalog-query/tree';
import { readConditions } from '@/lib/catalog-query/vocabulary';
import { fetchJson } from '@/lib/fetch-json';
import { NounBody, type CardsContext } from './FilterCards';
import { buildChoices, valueLabelFor } from './filter-choices';
import { applyConditions } from './filter-state';

interface FiltersModalProps {
  isOpen: boolean;
  onClose: () => void;
  filters: FilterOptions;
  /** The draft, on Done. */
  onApply: (filters: FilterOptions) => void;
  availablePrograms?: Program[];
  availableFields?: string[];
  availableObservations?: string[];
  availableBands?: string[];
  /** The viewer's tags, when the page already has them; else fetched on open. */
  availableLists?: FilterOption[];
  viewMode?: ViewMode;
  /** The map has its own position tools: no position chip there. */
  hidePosition?: boolean;
  title?: string;
}

const BTN = 'px-3 py-2 text-sm font-medium rounded-lg border border-border text-text-secondary hover:bg-card-hover disabled:opacity-50 disabled:cursor-not-allowed transition-all';
const EYEBROW = 'text-[11px] font-semibold uppercase tracking-wider text-text-tertiary';

export function FiltersModal({
  isOpen, onClose, filters, onApply,
  availablePrograms = [], availableFields = [], availableObservations = [], availableBands = [], availableLists,
  viewMode = 'objects', hidePosition = false, title = 'Advanced Filters',
}: FiltersModalProps) {
  const root = rootOf(viewMode);
  const isSpectra = viewMode === 'spectra';

  // the draft: what the cards edit until Done
  const [draft, setDraft] = useState<FilterOptions>(filters);
  useEffect(() => { if (isOpen) setDraft(filters); }, [isOpen, filters]);

  // the viewer's tags: the page's, else read through the GET route while open
  const { data: fetched } = useAvailableListsQuery(isOpen && !availableLists);
  const listOptions = useMemo<FilterOption[]>(
    () => availableLists ?? (fetched?.lists ?? []).map((l) => ({ value: l.id, label: l.name, color: l.color ?? undefined, icon: l.icon ?? undefined })),
    [availableLists, fetched],
  );

  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = '';
    };
  }, [isOpen, onClose]);

  const choices = useMemo(() => {
    const c = buildChoices({ programs: availablePrograms, fields: availableFields, observations: availableObservations, bands: availableBands });
    return { ...c, lists: listOptions };
  }, [availablePrograms, availableFields, availableObservations, availableBands, listOptions]);
  const label = useMemo(() => valueLabelFor(choices), [choices]);

  const reading = useMemo(() => readConditions(draft.conditions, root), [draft.conditions, root]);
  const ctx: CardsContext = {
    root,
    nodes: draft.conditions,
    onChange: (next: TreeNode[]) => setDraft((d) => applyConditions(d, next, root)),
    choices,
    label,
    hideKeys: [...(hidePosition ? ['position'] : []), ...(listOptions.length === 0 ? ['tag'] : [])],
  };

  // the count of the draft, debounced and aborted by the next edit
  const dr = useDrTag();
  const [count, setCount] = useState<{ n: number; stale: boolean } | null>(null);
  useEffect(() => {
    if (!isOpen) return;
    setCount((c) => (c ? { ...c, stale: true } : null));
    const controller = new AbortController();
    const t = setTimeout(() => {
      const params = filtersToURLParams(draft, 1, 1, defaultSortColumn(viewMode), 'asc', viewMode);
      fetchJson<CatalogQueryCountResponse>(`/api/catalog-query/count?${params.toString()}`, { signal: controller.signal, dr })
        .then((r) => { if (r.total >= 0) setCount({ n: r.total, stale: false }); })
        .catch(() => undefined);
    }, 500);
    return () => { clearTimeout(t); controller.abort(); };
  }, [isOpen, draft, viewMode, dr]);

  // copy the query, paste one from the clipboard, copy its SQL
  const [notice, setNotice] = useState<{ text: string; error?: boolean } | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const say = (text: string, error = false) => {
    setNotice({ text, error });
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setNotice(null), error ? 3000 : 1500);
  };
  const copyQuery = async () => {
    const q = encodeConditions(draft.conditions) ?? '{"and":[]}';
    try { await navigator.clipboard.writeText(q); say('Query copied'); } catch { say('Clipboard not available', true); }
  };
  const pasteQuery = async () => {
    let text: string;
    try { text = (await navigator.clipboard.readText()).trim(); } catch { say('Clipboard not available', true); return; }
    const nodes = parseConditions(text, root);
    if (!nodes) { say('The clipboard is not a query this catalog knows', true); return; }
    setDraft((d) => applyConditions(d, nodes, root));
    say(`Query pasted: ${nodes.length} ${nodes.length === 1 ? 'condition' : 'conditions'}`);
  };

  if (!isOpen) return null;

  const spectrumItems = reading.items.filter((it) => it.noun === 'spectrum');
  const objectItems = reading.items.filter((it) => it.noun === 'object');
  const conditionCount = reading.items.length;
  const noun = isSpectra ? 'spectra' : 'objects';

  return (
    <div className="fixed inset-0 z-[10000]">
      <div className="absolute inset-0 bg-slate-900/60 dark:bg-black/70" onClick={onClose} />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[min(880px,94vw)] max-h-[88vh] flex flex-col bg-background border border-border rounded-2xl shadow-2xl overflow-hidden"
      >
        {/* Header */}
        <div className="flex items-center gap-2 px-5 py-3 border-b border-border bg-surface-2">
          <h2 className="text-base font-semibold text-text-primary">{title}</h2>
          {notice && (
            <span className={`inline-flex items-center gap-1 text-xs ${notice.error ? 'text-red-500' : 'text-text-secondary'}`}>
              {!notice.error && <Check className="w-3.5 h-3.5" />}{notice.text}
            </span>
          )}
          <span className="flex-1" />
          <button type="button" onClick={copyQuery} className={`${BTN} inline-flex items-center gap-1.5 py-1.5`} title="Copy the query to the clipboard">
            <Copy className="w-3.5 h-3.5" /> Copy
          </button>
          <button type="button" onClick={pasteQuery} className={`${BTN} inline-flex items-center gap-1.5 py-1.5`} title="Replace the conditions with the query on the clipboard">
            <ClipboardPaste className="w-3.5 h-3.5" /> Paste
          </button>
          <button type="button" onClick={() => setDraft((d) => ({ ...DEFAULT_FILTERS, search: d.search, search_scope: d.search_scope }))} disabled={conditionCount === 0} className={BTN}>
            Clear
          </button>
          <button onClick={onClose} className="p-2 rounded-lg text-text-secondary hover:bg-card-hover hover:text-text-primary transition-colors" aria-label="Cancel">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* The cards */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5">
          {isSpectra ? (
            <>
              <section>
                <div className={`${EYEBROW} mb-2`}>Spectrum</div>
                <NounBody noun="spectrum" items={spectrumItems} level={{ table: 'spectra', path: [] }} ctx={ctx} />
              </section>
              <section className="rounded-lg border border-border bg-card p-3 border-l-[3px] border-l-primary">
                <div className="text-sm font-semibold text-text-primary mb-2">its object</div>
                <NounBody noun="object" items={objectItems} level={{ table: 'spectra', path: [], itsObject: true }} ctx={ctx} demote={['program', 'observation', 'grating']} />
              </section>
            </>
          ) : (
            <section>
              <NounBody noun="object" items={objectItems} level={{ table: 'objects', path: [] }} ctx={ctx} />
            </section>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center gap-3 px-5 py-3 border-t border-border bg-card text-sm">
          <span className={`text-text-secondary ${count?.stale ? 'opacity-60' : ''}`}>
            {count ? `${count.n.toLocaleString()} ${noun} match` : '…'}
          </span>
          <span className="flex-1" />
          <span className="text-xs text-text-tertiary">{conditionCount} {conditionCount === 1 ? 'condition' : 'conditions'}</span>
          <button onClick={onClose} className={BTN}>Cancel</button>
          <button onClick={() => { onApply(draft); onClose(); }}
            className="px-5 py-2 text-sm font-medium rounded-lg bg-primary text-on-primary hover:bg-primary-hover shadow-sm hover:shadow transition-all">
            Done
          </button>
        </div>
      </div>
    </div>
  );
}
