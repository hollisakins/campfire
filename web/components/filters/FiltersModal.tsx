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
import { Check, ChevronDown, X } from 'lucide-react';
import type { FilterOption } from '@/components/ui/FilterChip';
import { Popover, isInsidePopover } from '@/components/ui/Popover';
import { DEFAULT_FILTERS, type FilterOptions } from '@/lib/actions/filter-params';
import type { ViewMode } from '@/lib/actions/spectra-types';
import { defaultSortColumn } from '@/lib/actions/spectra-types';
import type { Program } from '@/lib/types';
import { useDrTag } from '@/lib/dr/client';
import { filtersToURLParams } from '@/lib/utils/url-params';
import type { CatalogQueryCountResponse } from '@/app/api/catalog-query/count/route';
import { getAvailableLists } from '@/lib/actions/lists';
import { rootOf } from '@/lib/catalog-query/legacy';
import { encodeConditions, parseConditions, treeOf, type TreeNode } from '@/lib/catalog-query/tree';
import { readConditions } from '@/lib/catalog-query/vocabulary';
import { fetchJson } from '@/lib/fetch-json';
import type { CatalogQuerySqlResponse } from '@/app/api/catalog-query/sql/route';
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
  viewMode?: ViewMode;
  /** The map has its own position tools: no position chip there. */
  hidePosition?: boolean;
  title?: string;
}

const BTN = 'px-3 py-2 text-sm font-medium rounded-lg border border-border text-text-secondary hover:bg-card-hover disabled:opacity-50 disabled:cursor-not-allowed transition-all';
const EYEBROW = 'text-[11px] font-semibold uppercase tracking-wider text-text-tertiary';

export function FiltersModal({
  isOpen, onClose, filters, onApply,
  availablePrograms = [], availableFields = [], availableObservations = [], availableBands = [],
  viewMode = 'objects', hidePosition = false, title = 'Filters',
}: FiltersModalProps) {
  const root = rootOf(viewMode);
  const isSpectra = viewMode === 'spectra';

  // the draft: what the cards edit until Done
  const [draft, setDraft] = useState<FilterOptions>(filters);
  useEffect(() => { if (isOpen) setDraft(filters); }, [isOpen, filters]);

  // the viewer's tags, once per opening
  const [listOptions, setListOptions] = useState<FilterOption[]>([]);
  useEffect(() => {
    if (!isOpen) return;
    getAvailableLists().then(({ lists }) => {
      setListOptions(lists.map((l) => ({ value: l.id, label: l.name, color: l.color ?? undefined, icon: l.icon ?? undefined })));
    });
  }, [isOpen]);

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
    showLine: draft.show_line,
    showBand: draft.show_band,
    onShowColumn: (kind, key) => setDraft((d) => (kind === 'line' ? { ...d, show_line: key } : { ...d, show_band: key })),
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

  // the Query menu: copy, paste, SQL
  const [menuOpen, setMenuOpen] = useState(false);
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasteText, setPasteText] = useState('');
  const [pasteError, setPasteError] = useState<string | null>(null);
  const [copied, setCopied] = useState<'query' | 'sql' | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onDown = (e: MouseEvent) => { if (!isInsidePopover(e.target, menuRef.current)) { setMenuOpen(false); setPasteOpen(false); } };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, []);
  const flash = (what: 'query' | 'sql') => { setCopied(what); setTimeout(() => setCopied(null), 1500); };
  const copyQuery = async () => {
    const q = encodeConditions(draft.conditions) ?? '{"and":[]}';
    await navigator.clipboard.writeText(q);
    flash('query');
    setMenuOpen(false);
  };
  const copySql = async () => {
    const tree = treeOf(draft.conditions);
    if (!tree) { await navigator.clipboard.writeText(`SELECT r.* FROM public.${root} r;`); flash('sql'); setMenuOpen(false); return; }
    try {
      const res = await fetchJson<CatalogQuerySqlResponse>(`/api/catalog-query/sql?root=${root}&q=${encodeURIComponent(JSON.stringify(tree))}`);
      await navigator.clipboard.writeText(`SELECT r.*\nFROM public.${root} r\nWHERE ${res.sql};`);
      flash('sql');
    } catch {
      setPasteError('Could not compile the query');
    }
    setMenuOpen(false);
  };
  const applyPaste = () => {
    const nodes = parseConditions(pasteText.trim(), root);
    if (!nodes) { setPasteError('Not a query this catalog knows'); return; }
    setDraft((d) => applyConditions(d, nodes, root));
    setPasteText('');
    setPasteError(null);
    setPasteOpen(false);
    setMenuOpen(false);
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
          <h2 className="text-base font-semibold text-text-primary">{title} · {noun}</h2>
          <span className="flex-1" />
          <div ref={menuRef} className="relative">
            <button type="button" onClick={() => setMenuOpen(!menuOpen)} className={`${BTN} inline-flex items-center gap-1.5 py-1.5`}>
              {copied ? <Check className="w-4 h-4" /> : null}
              {copied === 'query' ? 'Copied' : copied === 'sql' ? 'SQL copied' : 'Query'}
              <ChevronDown className="w-3.5 h-3.5" />
            </button>
            {menuOpen && (
              <Popover anchorRef={menuRef} open={menuOpen} className="w-[320px] bg-card border border-border rounded-lg shadow-lg p-1 text-sm">
                <button type="button" onClick={copyQuery} className="w-full text-left px-3 py-2 rounded-md hover:bg-card-hover text-text-primary">Copy query</button>
                <button type="button" onClick={() => { setPasteOpen(!pasteOpen); setPasteError(null); }} className="w-full text-left px-3 py-2 rounded-md hover:bg-card-hover text-text-primary">Paste query…</button>
                {pasteOpen && (
                  <div className="px-2 pb-2">
                    <textarea value={pasteText} onChange={(e) => setPasteText(e.target.value)} rows={4} placeholder='{"and":[…]}'
                      className="w-full px-2 py-1.5 text-xs font-mono border border-border rounded-md bg-background text-text-primary placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/40" />
                    {pasteError && <p className="text-xs text-red-500 mt-1">{pasteError}</p>}
                    <div className="flex justify-end mt-1.5">
                      <button type="button" onClick={applyPaste} className="px-3 py-1 text-xs font-medium rounded-md bg-primary text-on-primary hover:bg-primary-hover">Use this query</button>
                    </div>
                  </div>
                )}
                <div className="border-t border-border my-1" />
                <button type="button" onClick={copySql} className="w-full text-left px-3 py-2 rounded-md hover:bg-card-hover text-text-secondary">Copy as SQL</button>
              </Popover>
            )}
          </div>
          <button type="button" onClick={() => setDraft((d) => ({ ...DEFAULT_FILTERS, search: d.search, search_scope: d.search_scope }))} disabled={conditionCount === 0 && !draft.show_line && !draft.show_band} className={BTN}>
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
              <div className={`${EYEBROW} mb-2`}>Object</div>
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
