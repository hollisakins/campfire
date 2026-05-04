// Filter state for /nirspec/metadata. Serialized to/from URL search params so
// filtered views are shareable. All filtering happens client-side over the
// cached programs/observations overview payloads — the dataset is small enough
// that a server roundtrip per filter change would be wasteful.

export type MetadataTab = 'programs' | 'observations';

export interface MetadataFilters {
  tab: MetadataTab;
  search: string;
  // Programs scope
  cycle: number[];
  pi: string[];
  is_public: boolean | null;
  // Observations scope
  programs: string[];
  reduction_version: string[];
  crds_context: string[];
  has_patches: boolean | null;
  // Shared
  fields: string[];
  gratings: string[];
  recency_days: number | null;
}

const VALID_TABS: MetadataTab[] = ['programs', 'observations'];
const VALID_RECENCY = [7, 30, 90];

export const defaultMetadataFilters: MetadataFilters = {
  tab: 'programs',
  search: '',
  cycle: [],
  pi: [],
  is_public: null,
  programs: [],
  reduction_version: [],
  crds_context: [],
  has_patches: null,
  fields: [],
  gratings: [],
  recency_days: null,
};

const parseStringArray = (sp: URLSearchParams, key: string): string[] => {
  const v = sp.get(key);
  return v ? v.split(',').filter(Boolean) : [];
};

const parseNumberArray = (sp: URLSearchParams, key: string): number[] => {
  const v = sp.get(key);
  return v ? v.split(',').filter(Boolean).map(Number).filter(n => !isNaN(n)) : [];
};

const parseBoolean = (sp: URLSearchParams, key: string): boolean | null => {
  const v = sp.get(key);
  if (v === 'true') return true;
  if (v === 'false') return false;
  return null;
};

const parseRecency = (sp: URLSearchParams): number | null => {
  const v = sp.get('recency');
  if (!v) return null;
  const n = parseInt(v, 10);
  return VALID_RECENCY.includes(n) ? n : null;
};

export function parseMetadataFiltersFromURL(searchParams: URLSearchParams): MetadataFilters {
  const tabParam = searchParams.get('tab');
  const tab: MetadataTab = VALID_TABS.includes(tabParam as MetadataTab)
    ? (tabParam as MetadataTab)
    : 'programs';

  return {
    tab,
    search: searchParams.get('q') ?? '',
    cycle: parseNumberArray(searchParams, 'cycle'),
    pi: parseStringArray(searchParams, 'pi'),
    is_public: parseBoolean(searchParams, 'public'),
    programs: parseStringArray(searchParams, 'programs'),
    reduction_version: parseStringArray(searchParams, 'rv'),
    crds_context: parseStringArray(searchParams, 'crds'),
    has_patches: parseBoolean(searchParams, 'patches'),
    fields: parseStringArray(searchParams, 'fields'),
    gratings: parseStringArray(searchParams, 'gratings'),
    recency_days: parseRecency(searchParams),
  };
}

export function metadataFiltersToURLParams(filters: MetadataFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.tab !== 'programs') params.set('tab', filters.tab);
  if (filters.search) params.set('q', filters.search);
  if (filters.cycle.length) params.set('cycle', filters.cycle.join(','));
  if (filters.pi.length) params.set('pi', filters.pi.join(','));
  if (filters.is_public !== null) params.set('public', String(filters.is_public));
  if (filters.programs.length) params.set('programs', filters.programs.join(','));
  if (filters.reduction_version.length)
    params.set('rv', filters.reduction_version.join(','));
  if (filters.crds_context.length) params.set('crds', filters.crds_context.join(','));
  if (filters.has_patches !== null) params.set('patches', String(filters.has_patches));
  if (filters.fields.length) params.set('fields', filters.fields.join(','));
  if (filters.gratings.length) params.set('gratings', filters.gratings.join(','));
  if (filters.recency_days !== null) params.set('recency', String(filters.recency_days));
  return params;
}

export function hasActiveFilters(filters: MetadataFilters): boolean {
  return (
    filters.search !== '' ||
    filters.cycle.length > 0 ||
    filters.pi.length > 0 ||
    filters.is_public !== null ||
    filters.programs.length > 0 ||
    filters.reduction_version.length > 0 ||
    filters.crds_context.length > 0 ||
    filters.has_patches !== null ||
    filters.fields.length > 0 ||
    filters.gratings.length > 0 ||
    filters.recency_days !== null
  );
}

export function isWithinRecency(timestamp: string | null, days: number | null): boolean {
  if (days === null) return true;
  if (!timestamp) return false;
  const t = new Date(timestamp).getTime();
  if (isNaN(t)) return false;
  const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
  return t >= cutoff;
}
