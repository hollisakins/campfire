/**
 * The leaf catalogue of the predicate tree (Catalog Read Model phase 3b): the
 * tables, columns and operators `catalog_tree_sql` accepts, mirrored from
 * `catalog_query_whitelist()` / `catalog_query_correlations()` in
 * supabase/schemas/functions.sql with the labels the builder shows.
 * catalog-query.test.ts fails when the column set drifts from the whitelist.
 *
 * The objects' member aggregates (programs / gratings / observations /
 * n_targets / n_spectra) are deliberately absent: they count hidden members,
 * so those questions are an exists over targets or spectra.
 */

export type LeafType = 'num' | 'int' | 'text' | 'bool' | 'ts' | 'bits' | 'text[]' | 'int[]';

export const TREE_TABLES = [
  'objects', 'targets', 'spectra', 'lines', 'line_fits', 'photometry', 'catalog_matches', 'object_list_members',
] as const;
export type TreeTable = typeof TREE_TABLES[number];

/** The tables a tree can be rooted at (catalog_tree_sql's p_root). */
export type TreeRoot = 'objects' | 'spectra' | 'targets';

/** Where a leaf's value comes from in the builder: a free input, or one of
 *  the option lists the page already carries. */
export type LeafChoices =
  | 'line' | 'band' | 'grating' | 'program' | 'observation' | 'field' | 'quality'
  | 'component' | 'deploy_status' | 'list' | 'dq_flags' | 'spectral_features' | 'line_flags';

export interface LeafDef {
  type: LeafType;
  label: string;
  unit?: string;
  choices?: LeafChoices;
}

type TableDef = Record<string, LeafDef>;

const num = (label: string, unit?: string, choices?: LeafChoices): LeafDef => ({ type: 'num', label, unit, choices });
const int = (label: string, choices?: LeafChoices): LeafDef => ({ type: 'int', label, choices });
const text = (label: string, choices?: LeafChoices): LeafDef => ({ type: 'text', label, choices });
const bool = (label: string): LeafDef => ({ type: 'bool', label });
const ts = (label: string): LeafDef => ({ type: 'ts', label });
const bits = (label: string, choices: LeafChoices): LeafDef => ({ type: 'bits', label, choices });

export const CATALOGUE: Record<TreeTable, TableDef> = {
  objects: {
    id: int('Row id'), object_id: text('Object ID'), field: text('Field', 'field'),
    ra: num('RA', 'deg'), dec: num('Dec', 'deg'),
    max_snr: num('Max S/N'), max_exposure_time: num('Max exposure time', 's'),
    photo_z: num('Photo-z'), photo_z_err_lo: num('Photo-z error (low)'), photo_z_err_hi: num('Photo-z error (high)'),
    has_photometry: bool('Has photometry'),
    redshift: num('Redshift'), redshift_quality: int('Redshift quality', 'quality'),
    redshift_auto: num('Auto redshift'), redshift_inspected: num('Inspected redshift'),
    inspected_used_auto: bool('Inspection kept the auto redshift'), needs_review: bool('Needs review'),
    last_inspected_at: ts('Last inspected'), last_data_change_at: ts('Last data change'),
    version: int('Version'), is_active: bool('Active'),
    has_published_spectrum: bool('Has a published spectrum'), is_public: bool('Public'),
    created_at: ts('Created'), updated_at: ts('Updated'),
  },
  targets: {
    id: int('Row id'), target_id: text('Target ID'), field: text('Field', 'field'),
    ra: num('RA', 'deg'), dec: num('Dec', 'deg'),
    program_slug: text('Program', 'program'), observation: text('Observation', 'observation'),
    max_snr: num('Max S/N'), max_exposure_time: num('Max exposure time', 's'),
    redshift_auto: num('Auto redshift'),
    dq_flags: bits('Quality flags', 'dq_flags'), spectral_features: bits('Spectral features', 'spectral_features'),
    has_published_spectrum: bool('Has a published spectrum'), is_public: bool('Public'), object_id: int('Object row id'),
  },
  spectra: {
    id: int('Row id'), spectrum_id: text('Spectrum ID'), target_id: text('Target ID'),
    grating: text('Grating', 'grating'),
    signal_to_noise: num('S/N'), exposure_time: num('Exposure time', 's'),
    redshift_auto: num('Auto redshift'), chi2_min: num('Redshift fit χ² (min)'), confidence: num('Redshift fit confidence'),
    dq_flags: bits('Quality flags', 'dq_flags'), deploy_status: text('Deploy status', 'deploy_status'),
    program_slug: text('Program', 'program'), observation: text('Observation', 'observation'),
    date_obs: text('Observation date'), cfpipe_version: text('cfpipe version'), jwst_version: text('jwst version'),
    crds_context: text('CRDS context'), is_public: bool('Public'),
    reduced_at: ts('Reduced'), created_at: ts('Created'), updated_at: ts('Updated'),
  },
  lines: {
    spectrum_id: int('Spectrum row id'), object_id: int('Object row id'),
    line: text('Line', 'line'), component: text('Component', 'component'),
    wave_rest: num('Rest wavelength', 'Å'), wave_obs: num('Observed wavelength', 'Å'),
    flux: num('Flux', 'erg/s/cm²'), flux_err: num('Flux error', 'erg/s/cm²'), snr: num('S/N'),
    ew_rest: num('Rest EW', 'Å'), ew_rest_err: num('Rest EW error', 'Å'),
    cont: num('Continuum'), cont_err: num('Continuum error'),
    dv: num('Velocity offset', 'km/s'), dv_err: num('Velocity offset error', 'km/s'),
    sigma_v: num('Velocity dispersion', 'km/s'), sigma_v_err: num('Velocity dispersion error', 'km/s'),
    sigma_lsf_kms: num('LSF dispersion', 'km/s'),
    complex: int('Complex'), chi2: num('χ²'), dof: int('Degrees of freedom'), npix: int('Pixels'),
    flags: bits('Line flags', 'line_flags'), blend_into: text('Blended into'), tied_to: text('Tied to'),
    label: text('Label'), members: { type: 'text[]', label: 'Doublet members' },
  },
  line_fits: {
    spectrum_id: int('Spectrum row id'), target_id: text('Target ID'), grating: text('Grating', 'grating'),
    program_slug: text('Program', 'program'), observation: text('Observation', 'observation'),
    z_used: num('Redshift used'), z_source: text('Redshift source'), z_quality: int('Redshift quality', 'quality'),
    object_id: text('Object ID'), object_version: int('Object version'),
    z_fit: num('Fitted redshift'), z_fit_err: num('Fitted redshift error'),
    dv: num('Velocity offset', 'km/s'), dv_err: num('Velocity offset error', 'km/s'),
    sigma_v: num('Velocity dispersion', 'km/s'), sigma_v_err: num('Velocity dispersion error', 'km/s'),
    kin_source: text('Kinematics source'),
    n_lines: int('Lines fitted'), n_detected: int('Lines detected'), n_broad: int('Broad lines'),
    chi2: num('χ²'), dof: int('Degrees of freedom'),
    fit_version: text('Fit version'), cfpipe_version: text('cfpipe version'), f_lsf: num('LSF factor'),
    fitted_at: ts('Fitted'), stale_redshift: bool('Stale (redshift moved since the fit)'),
  },
  photometry: {
    match_id: int('Match row id'), object_id: int('Object row id'), band: text('Band', 'band'),
    flux: num('Flux', 'µJy'), flux_err: num('Flux error', 'µJy'),
    wav: num('Wavelength', 'µm'), wav_min: num('Wavelength (min)', 'µm'), wav_max: num('Wavelength (max)', 'µm'),
    snr: num('S/N'), mag: num('Magnitude', 'AB'),
  },
  catalog_matches: {
    id: int('Row id'), object_id: int('Object row id'), field: text('Field', 'field'),
    ra: num('RA', 'deg'), dec: num('Dec', 'deg'),
    catalog_name: text('Catalog'), catalog_id: text('Catalog ID'), match_distance_arcsec: num('Match distance', 'arcsec'),
    photo_z: num('Photo-z'), photo_z_err_lo: num('Photo-z error (low)'), photo_z_err_hi: num('Photo-z error (high)'),
    has_pz: bool('Has P(z)'),
  },
  object_list_members: {
    list_id: int('Tag', 'list'), object_id: int('Object row id'), added_at: ts('Added'),
  },
};

/** How the builder names a table's rows ("has a line where …"). */
export const TABLE_NOUN: Record<TreeTable, { singular: string; exists: string }> = {
  objects: { singular: 'object', exists: 'has an object where' },
  targets: { singular: 'target', exists: 'has a target where' },
  spectra: { singular: 'spectrum', exists: 'has a spectrum where' },
  lines: { singular: 'line', exists: 'has a line where' },
  line_fits: { singular: 'line fit', exists: 'has a line fit where' },
  photometry: { singular: 'band', exists: 'has photometry where' },
  catalog_matches: { singular: 'catalog match', exists: 'has a catalog match where' },
  object_list_members: { singular: 'tag', exists: 'is in a tag where' },
};

/** The child tables each table can test with an exists (catalog_query_correlations). */
export const CORRELATIONS: Record<TreeTable, readonly TreeTable[]> = {
  objects: ['targets', 'spectra', 'lines', 'line_fits', 'photometry', 'catalog_matches', 'object_list_members'],
  targets: ['objects', 'spectra', 'lines', 'line_fits', 'photometry', 'catalog_matches', 'object_list_members'],
  spectra: ['targets', 'objects', 'lines', 'line_fits', 'photometry', 'catalog_matches', 'object_list_members'],
  lines: ['line_fits', 'spectra'],
  line_fits: ['lines', 'spectra'],
  photometry: ['catalog_matches'],
  catalog_matches: ['photometry'],
  object_list_members: [],
};

export const TREE_OPS = [
  'eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'between', 'in', 'nin', 'ilike',
  'overlaps', 'contains', 'has', 'nhas', 'anybits', 'allbits', 'nobits', 'is_null', 'not_null',
] as const;
export type TreeOp = typeof TREE_OPS[number];

/** The operators each leaf type takes (catalog_query_node_sql). */
export const OPS_BY_TYPE: Record<LeafType, readonly TreeOp[]> = {
  num: ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'between', 'in', 'nin', 'is_null', 'not_null'],
  int: ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'between', 'in', 'nin', 'is_null', 'not_null'],
  ts: ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'between', 'is_null', 'not_null'],
  text: ['eq', 'neq', 'in', 'nin', 'ilike', 'is_null', 'not_null'],
  bool: ['eq', 'neq', 'is_null', 'not_null'],
  'text[]': ['overlaps', 'contains', 'has', 'nhas', 'is_null', 'not_null'],
  'int[]': ['overlaps', 'contains', 'has', 'nhas', 'is_null', 'not_null'],
  bits: ['anybits', 'allbits', 'nobits', 'eq', 'is_null', 'not_null'],
};

export const OP_LABEL: Record<TreeOp, string> = {
  eq: '=', neq: '≠', gt: '>', gte: '≥', lt: '<', lte: '≤', between: 'between', in: 'is one of', nin: 'is none of',
  ilike: 'matches', overlaps: 'has any of', contains: 'has all of', has: 'includes', nhas: 'excludes',
  anybits: 'has any of', allbits: 'has all of', nobits: 'has none of', is_null: 'is empty', not_null: 'is set',
};

/** The value shape an operator takes. */
export type OpArity = 'none' | 'one' | 'pair' | 'many';
export function opArity(op: TreeOp): OpArity {
  switch (op) {
    case 'is_null': case 'not_null': return 'none';
    case 'between': return 'pair';
    case 'in': case 'nin': case 'overlaps': case 'contains': return 'many';
    default: return 'one';
  }
}

export function leafDef(table: TreeTable, col: string): LeafDef | undefined {
  return Object.prototype.hasOwnProperty.call(CATALOGUE[table], col) ? CATALOGUE[table][col] : undefined;
}

export function isTreeTable(x: unknown): x is TreeTable {
  return typeof x === 'string' && (TREE_TABLES as readonly string[]).includes(x);
}

export function isTreeOp(x: unknown): x is TreeOp {
  return typeof x === 'string' && (TREE_OPS as readonly string[]).includes(x);
}

/** Tables with a sky position (a cone applies). */
export function hasSkyPosition(table: TreeTable): boolean {
  return 'ra' in CATALOGUE[table] && 'dec' in CATALOGUE[table];
}
