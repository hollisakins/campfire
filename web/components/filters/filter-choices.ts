/**
 * The option lists and value labels the filter cards show for the
 * choice-valued properties of the vocabulary: fixed catalogs (gratings,
 * quality, flags, the line list) here, the per-catalog lists (programs,
 * fields, observations, bands, the viewer's tags) from the page.
 */
import type { FilterOption } from '@/components/ui/FilterChip';
import type { LeafChoices } from '@/lib/catalog-query/catalogue';
import type { Scalar } from '@/lib/catalog-query/tree';
import type { ValueLabel } from '@/lib/catalog-query/vocabulary';
import { DQ_FLAGS, LINE_FLAGS, REDSHIFT_QUALITY, decodeBitmask } from '@/lib/flags';
import { LINE_FILTER_OPTIONS, lineLabel } from '@/lib/linelist';
import { GRATINGS, type Program } from '@/lib/types';
import { OBSERVATION_COLORS } from '@/components/map/observation-colors';

export interface FilterChoices {
  programs: FilterOption[];
  fields: FilterOption[];
  observations: FilterOption[];
  bands: FilterOption[];
  lists: FilterOption[];
}

export const EMPTY_CHOICES: FilterChoices = { programs: [], fields: [], observations: [], bands: [], lists: [] };

/** The page's lists as chip options. */
export function buildChoices(input: {
  programs?: Program[]; fields?: string[]; observations?: string[]; bands?: string[];
  lists?: { id: number; name: string; color?: string | null; icon?: string | null }[];
}): FilterChoices {
  const programs = [...(input.programs ?? [])]
    .sort((a, b) => (a.jwst_pids?.length ? Math.max(...a.jwst_pids) : 0) - (b.jwst_pids?.length ? Math.max(...b.jwst_pids) : 0))
    .map((p) => ({ value: p.slug, label: p.program_name || p.slug }));
  return {
    programs,
    fields: (input.fields ?? []).map((f) => ({ value: f, label: f.toUpperCase() })),
    observations: (input.observations ?? []).map((o, idx) => ({ value: o, label: o, color: OBSERVATION_COLORS[idx % OBSERVATION_COLORS.length] })),
    bands: (input.bands ?? []).map((b) => ({ value: b, label: b.toUpperCase() })),
    lists: (input.lists ?? []).map((l) => ({ value: l.id, label: l.name, color: l.color ?? undefined, icon: l.icon ?? undefined })),
  };
}

export const LINE_OPTIONS: FilterOption[] = LINE_FILTER_OPTIONS.map((o) => ({
  value: o.value,
  label: `${o.label}  ${o.wave < 10000 ? o.wave.toFixed(0) : (o.wave / 1e4).toFixed(2) + ' µm'}`,
  short: o.label,
}));

const QUALITY_OPTIONS: FilterOption[] = REDSHIFT_QUALITY.map((q) => ({ value: q.value, label: q.label, icon: q.icon, color: q.color }));
const GRATING_OPTIONS: FilterOption[] = GRATINGS.map((g) => ({ value: g, label: g }));
const DQ_OPTIONS: FilterOption[] = DQ_FLAGS.map((f) => ({ value: f.value, label: f.label, icon: f.icon, color: f.color }));
const LINE_FLAG_OPTIONS: FilterOption[] = LINE_FLAGS.map((f) => ({ value: f.value, label: f.label, icon: f.icon, color: f.color }));
const COMPONENT_OPTIONS: FilterOption[] = ['narrow', 'broad', 'doublet'].map((c) => ({ value: c, label: c }));

/** The options a choice-valued property offers. */
export function optionsFor(choices: LeafChoices | undefined, c: FilterChoices): FilterOption[] {
  switch (choices) {
    case 'program': return c.programs;
    case 'field': return c.fields;
    case 'observation': return c.observations;
    case 'band': return c.bands;
    case 'list': return c.lists;
    case 'line': return LINE_OPTIONS;
    case 'quality': return QUALITY_OPTIONS;
    case 'grating': return GRATING_OPTIONS;
    case 'dq_flags': return DQ_OPTIONS;
    case 'line_flags': return LINE_FLAG_OPTIONS;
    case 'component': return COMPONENT_OPTIONS;
    default: return [];
  }
}

/** A value's display name: a program's name, a line's label, a flag mask's names. */
export function valueLabelFor(c: FilterChoices): ValueLabel {
  return (choices, value: Scalar) => {
    if (choices === 'line' && typeof value === 'string') return lineLabel(value);
    if (choices === 'band' && typeof value === 'string') return value.toUpperCase();
    if ((choices === 'dq_flags' || choices === 'line_flags') && typeof value === 'number') {
      const defs = choices === 'dq_flags' ? DQ_FLAGS : LINE_FLAGS;
      const names = decodeBitmask(value, defs).map((v) => defs.find((d) => d.value === v)?.short ?? String(v));
      return names.join(', ') || String(value);
    }
    const opt = optionsFor(choices, c).find((o) => o.value === value);
    return opt ? (opt.short ?? opt.label) : String(value);
  };
}
