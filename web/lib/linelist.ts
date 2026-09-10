/**
 * The emission-line catalog the pipeline measures (`cfpipe nirspec linefit`,
 * docs/design-emission-line-fitting.md), mirrored from
 * pipeline/campfire_pipeline/nirspec/linelist.py by scripts/sync_linelist.py
 * into linelist.json (CI fails on drift). Keys are the `lines` jsonb keys of
 * spectrum_line_fits and the `line` column of spectrum_lines.
 *
 * Two kinds of entry: a `line` (one catalog line; `doublet` names the total it
 * is a component of, when any) and a `doublet` total (`members` = its two
 * components). The catalog FILTER offers totals and stand-alone lines only —
 * a doublet component means "the 1907 line" on a grating that resolves the
 * pair and "the whole blend" on one that does not, so selecting on it would
 * mix two populations; the total means the same thing everywhere.
 */
import catalog from './linelist.json';

export interface CatalogLine {
  name: string;
  label: string;
  /** Rest vacuum wavelength, Angstrom (weight-averaged for a doublet total). */
  wave: number;
  kind: 'line' | 'doublet';
  tied_to: string | null;
  broad: boolean;
  /** kind = 'line': the doublet total this line is a component of. */
  doublet?: string | null;
  /** kind = 'doublet': the two component line names, blue first. */
  members?: string[];
}

export const LINE_CATALOG: CatalogLine[] = catalog.lines as CatalogLine[];

const BY_NAME: Map<string, CatalogLine> = new Map(LINE_CATALOG.map((l) => [l.name, l]));

/** Catalog entry for a jsonb key; `<line>_broad` resolves to its line. */
export function catalogLine(name: string): CatalogLine | undefined {
  const base = name.endsWith('_broad') ? name.slice(0, -'_broad'.length) : name;
  return BY_NAME.get(base);
}

/** Display label for any jsonb key (falls back to the key itself). */
export function lineLabel(name: string): string {
  const entry = catalogLine(name);
  if (!entry) return name;
  return name.endsWith('_broad') ? `${entry.label} (broad)` : entry.label;
}

/** Rest wavelength used to order rows (`_broad` sorts with its line). */
export function lineWave(name: string): number {
  return catalogLine(name)?.wave ?? Number.POSITIVE_INFINITY;
}

export interface LineFilterOption {
  value: string;
  label: string;
  wave: number;
  group: 'Doublet totals' | 'Lines';
}

/**
 * What the catalog line filter offers, by rest wavelength: the doublet
 * totals and every line that is not a doublet component.
 */
export const LINE_FILTER_OPTIONS: LineFilterOption[] = LINE_CATALOG
  .filter((l) => l.kind === 'doublet' || !l.doublet)
  .map((l) => ({
    value: l.name,
    label: l.label,
    wave: l.wave,
    group: l.kind === 'doublet' ? 'Doublet totals' as const : 'Lines' as const,
  }));

const FILTERABLE = new Set(LINE_FILTER_OPTIONS.map((o) => o.value));

/** True for a name the line filter accepts (a total or a stand-alone line). */
export function isFilterableLine(name: string): boolean {
  return FILTERABLE.has(name);
}
