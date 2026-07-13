// TypeScript type definitions for CAMPFIRE
// Matches the Supabase database schema

import { REDSHIFT_QUALITY } from '@/lib/flags';

// ============================================
// Database Tables
// ============================================

export interface UserProfile {
  user_id: string;
  username: string;
  full_name: string;
  created_at: string;
  is_group_account: boolean;
  can_comment: boolean;
  can_inspect: boolean;
  is_admin?: boolean;
  preferences?: UserPreferences;
}

// ============================================
// User Preferences
// ============================================

export type ThemeSetting = 'light' | 'dark' | 'system';
export type FluxUnit = 'fnu' | 'flambda';
export type Colorscale2D = 'Viridis' | 'Plasma' | 'Inferno' | 'Magma' | 'Cividis' | 'Greys';

// ============================================
// Accent Color System
// ============================================

export type AccentColorName = 'ember' | 'magenta' | 'blue' | 'emerald' | 'red' | 'orange' | 'violet' | 'cyan' | 'lime';

export interface AccentColor {
  name: AccentColorName;
  label: string;
  light: string;      // Vibrant for light mode
  dark: string;       // Muted/pale for dark mode
  hover: {
    light: string;
    dark: string;
  };
}

// Accent colors with light/dark mode variants
// Dark mode uses more muted/pale versions for better contrast
export const ACCENT_COLORS: AccentColor[] = [
  // Ember — Direction 2 (Ember & Dusk) signature accent. AA-verified on card/dusk surfaces.
  { name: 'ember', label: 'Ember', light: '#c63f0c', dark: '#fb923c', hover: { light: '#ad3408', dark: '#fdba74' } },
  { name: 'magenta', label: 'Magenta', light: '#c026d3', dark: '#e879f9', hover: { light: '#a21caf', dark: '#f0abfc' } },
  { name: 'blue', label: 'Blue', light: '#2563eb', dark: '#60a5fa', hover: { light: '#1d4ed8', dark: '#93c5fd' } },
  { name: 'emerald', label: 'Emerald', light: '#059669', dark: '#34d399', hover: { light: '#047857', dark: '#6ee7b7' } },
  { name: 'red', label: 'Red', light: '#dc2626', dark: '#f87171', hover: { light: '#b91c1c', dark: '#fca5a5' } },
  { name: 'orange', label: 'Orange', light: '#ea580c', dark: '#fb923c', hover: { light: '#c2410c', dark: '#fdba74' } },
  { name: 'violet', label: 'Violet', light: '#7c3aed', dark: '#a78bfa', hover: { light: '#6d28d9', dark: '#c4b5fd' } },
  { name: 'cyan', label: 'Cyan', light: '#0891b2', dark: '#22d3ee', hover: { light: '#0e7490', dark: '#67e8f9' } },
  { name: 'lime', label: 'Lime', light: '#65a30d', dark: '#a3e635', hover: { light: '#4d7c0f', dark: '#bef264' } },
];

export const DEFAULT_ACCENT_COLOR: AccentColorName = 'ember';

// Helper to get accent color by name
export function getAccentColor(name: AccentColorName): AccentColor {
  return ACCENT_COLORS.find(c => c.name === name) || ACCENT_COLORS[0];
}

// ============================================
// User Preferences
// ============================================

export interface SpectrumPreferences {
  fluxUnit: FluxUnit;
  colorscale2D: Colorscale2D;
  snrMin: number;
  snrMax: number;
}

export interface UserPreferences {
  theme: ThemeSetting;
  accentColor: AccentColorName;
  spectrum: SpectrumPreferences;
}

export const DEFAULT_SPECTRUM_PREFERENCES: SpectrumPreferences = {
  fluxUnit: 'flambda',
  colorscale2D: 'Viridis',
  snrMin: -5,
  snrMax: 10,
};

export const DEFAULT_USER_PREFERENCES: UserPreferences = {
  theme: 'system',
  accentColor: DEFAULT_ACCENT_COLOR,
  spectrum: DEFAULT_SPECTRUM_PREFERENCES,
};

export interface AccessCode {
  id: string;
  code: string;
  description: string | null;
  grants_all_programs: boolean;
  program_slugs: string[] | null;
  created_by: string | null;
  created_at: string;
  expires_at: string | null;
  max_uses: number | null;
  use_count: number;
  is_active: boolean;
}

export interface CodeRedemption {
  id: string;
  code_id: string;
  user_id: string;
  redeemed_at: string;
}

export interface Program {
  slug: string;
  program_name: string | null;
  pi_name: string | null;
  description: string | null;
  is_public: boolean;
  cycle: number | null;
  jwst_pids?: number[];
  created_at: string;
}

export interface UserProgramAccess {
  user_id: string;
  program_slug: string;
  granted_at: string;
  granted_by: string | null;
}

// Account request status types
export type AccountRequestStatus = 'pending' | 'approved' | 'rejected';

export interface AccountRequest {
  id: number;
  email: string;
  full_name: string;
  status: AccountRequestStatus;
  is_admin: boolean;
  can_comment: boolean;
  program_slugs: string[];
  created_at: string;
  reviewed_at: string | null;
  reviewed_by: string | null;
  reviewed_by_name?: string; // Joined from user_profiles
  rejection_reason: string | null;
}

// Spectra-mode rows: redshift / redshift_quality / last_inspected_* fields
// are pulled from the parent object by the spectra RPC and surfaced here so
// the table renderer can type them as `DbTarget`.
export interface DbTarget {
  id: number;
  target_id: string;
  program_slug: string;
  field: string;
  observation: string;
  ra: number;
  dec: number;
  redshift: number | null;           // From parent object
  redshift_auto: number | null;      // From spectra (per-grating) or transitional target value
  redshift_inspected: number | null; // From parent object
  redshift_quality: number;          // From parent object
  last_inspected_at: string | null;  // From parent object
  last_inspected_by: string | null;
  created_at: string;
  updated_at: string;
  distance?: number | null;          // Only present when coordinate search is active (in degrees)
}

export interface Spectrum {
  id: number;
  /** Stable filename-derived identifier (basename of fits_path with `_spec.fits` stripped). */
  spectrum_id: string;
  target_id: string;  // FK to targets.target_id (text)
  grating: string;
  fits_path: string;
  cfpipe_version: string | null;
  signal_to_noise: number | null;
  exposure_time: number | null;
  created_at: string;
  redshift_auto?: number | null;
  dq_flags?: number;
  // Pre-generated SVG thumbnails (included when p_include_thumbnails=true in RPC)
  thumbnail_svg_fnu?: string | null;
  thumbnail_svg_flambda?: string | null;
}

export interface Comment {
  id: number;
  target_id: number | null;
  object_id: number | null;
  user_id: string;
  content: string;
  created_at: string;
  edited_at: string | null;
  is_deleted: boolean;
}

// Object lists (replaces object_flags bitmask)
export interface ObjectList {
  id: number;
  name: string;
  slug: string;
  description: string | null;
  visibility: 'private' | 'public_read' | 'public_edit';
  is_system: boolean;
  color: string | null;
  icon: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface ObjectListMember {
  id: number;
  list_id: number;
  object_id: number | null;
  ra: number;
  dec: number;
  notes: string | null;
  added_by: string | null;
  added_at: string;
}

export interface ObjectListWithMembership extends ObjectList {
  is_member: boolean;
}

export interface ObjectListOverview extends ObjectList {
  member_count: number;
  creator_name: string | null;
}

export interface ObjectListMemberWithObject extends ObjectListMember {
  object: {
    id: number;
    object_id: string;
    field: string;
    ra: number;
    dec: number;
    redshift: number | null;
    redshift_quality: number;
    n_spectra: number;
    max_snr: number | null;
  } | null;
}

export interface FlagAuditLog {
  id: number;
  // Exactly one of these three is non-null (check constraint).
  target_id: number | null;
  object_id: number | null;
  spectrum_id: number | null;
  user_id: string;
  field_name: string;
  old_value: number | null;
  new_value: number | null;
  changed_at: string;
}

export interface FlagDefinition {
  category: string;
  bit_position: number | null;
  value: number;
  label: string;
  short_label: string | null;
  icon: string | null;
  color: string | null;
  description: string | null;
}

export interface NircamImage {
  id?: number;
  field: string;
  tile: string;
  filter: string;
  pixel_scale: string;
  extension: string;  // sci, err, rms, srcmask
  epoch?: string;     // exposure-subset name ('' = full field)
  file_path: string;
  file_size?: number; // logical (uncompressed) bytes, if available
  /** Bytes as stored in the bucket for gzipped mosaics (registry
   *  stored_size_bytes); undefined = stored verbatim or not yet recorded. */
  file_size_stored?: number;
}

// A per-(field, filter) exposure-coverage map (product_type 'nircam_expmap',
// sourced from the storage_objects registry). Unlike mosaics there is one
// fiducial map per field/filter — no tile/scale/extension axes.
export interface NircamExpmap {
  field: string;
  filter: string;
  storage_key: string;
  file_size?: number; // size_bytes from the registry, if available
}

// One row of the /nircam landing grid (get_nircam_fields RPC + a presigned
// layout-plot URL). Coverage areas and center come from the `fields` table
// and are null until the field's first post-redesign deploy.
export interface NircamFieldCard {
  field: string;
  display_name: string;
  center_ra: number | null;
  center_dec: number | null;
  coverage_area_arcmin2: number | null;
  coverage_area_deg2: number | null;
  n_filters: number;
  n_tiles: number;
  n_files: number;
  total_bytes: number;
  last_updated: string | null;
  layout_url: string | null;  // presigned <field>_layout.png GET, if deployed
}

// The /nircam/[field] overview (get_nircam_field_summary RPC): the card fields
// plus the per-field facet arrays that drive the metadata grid.
export interface NircamFieldSummary extends Omit<NircamFieldCard, 'layout_url'> {
  filters: string[];
  tiles: string[];
  pixel_scales: string[];
  extensions: string[];
  epochs: string[];
  /** Provenance of the latest published deployment (deployments table);
   *  null when no deployment row is visible. */
  cfpipe_version: string | null;
  jwst_version: string | null;
  crds_context: string | null;
}

// One row of the field-page data-products table: a mosaic (from nircam_images)
// or an exposure map folded in as a synthetic `extension: 'exp'` row with no
// tile/scale axes. `file_path` doubles as the canonical storage key for both
// kinds — the download action routes on `kind`.
export interface NircamProductRow {
  kind: 'mosaic' | 'expmap';
  field: string;
  filter: string;
  tile: string | null;        // null on expmap rows
  pixel_scale: string | null; // null on expmap rows
  extension: string;          // sci, err, wht, srcmask, ... or 'exp'
  epoch?: string;             // '' = full field (mosaics only)
  file_path: string;          // canonical storage key
  file_size?: number;         // logical (uncompressed) bytes
  file_size_stored?: number;  // bucket bytes for gzipped mosaics (else undefined)
}

// NIRCam pipeline step names. Matches campfire_pipeline.common.cfp.CFP_KEYS
// order: 'uncal' means raw exists but no canonical file yet; each subsequent
// value is the name of the highest-completed step (process phase →
// detector1..jhat, combine phase → apply_mask..outlier).
export const NIRCAM_STAGES = [
  'uncal',
  'detector1',
  'persistence',
  'wisp',
  'image2',
  'edge',
  'bkg',
  'diag_striping',
  'wcs_shift',
  'preview',
  'jhat',
  'apply_mask',
  'bad_pixel',
  'outlier',
] as const;
export type NircamStage = typeof NIRCAM_STAGES[number];

// A polygon in image-pixel coordinates, FITS 1-indexed (DS9 `image` frame),
// suitable for round-tripping through ds9 .reg files and the `regions` library.
export interface MaskPolygon {
  id: string;                      // client-generated uuid
  vertices: [number, number][];    // [[x, y], ...] in DS9 image coords (1-indexed)
  label?: string;
  source: 'imported' | 'web';
  original_frame?: 'fk5' | 'icrs' | 'image' | string;
  imported_from?: string;
  imported_at?: string;
  created_at?: string;
  modified_at?: string;
}

export interface MaskRegionsPayload {
  version: 1;
  polygons: MaskPolygon[];
}

export interface NircamExposure {
  id: number;
  field: string;
  filter: string;
  detector: string;
  filename: string;
  visit: string | null;
  date_obs: string | null;
  ra_center: number | null;
  dec_center: number | null;
  stage: NircamStage;
  review_status: 'pending' | 'approved' | 'excluded';
  correction: 'none' | 'needed' | 'done';
  png_path: string | null;
  full_png_path: string | null;
  image_width: number | null;
  image_height: number | null;
  mask_regions: MaskRegionsPayload | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

// NIRSpec rate-file detector triage (NIRSpec review loop, P2/P3). Detector-grain
// analogue of NircamExposure: source-independent rate-level masks reviewed on the
// web. Mirrors the supabase nirspec_rate_exposures table.
export interface NirspecRateExposure {
  id: number;
  observation: string;
  exposure_root: string;
  detector: string;               // 'nrs1' | 'nrs2'
  filename: string;
  grating: string | null;
  image_width: number | null;
  image_height: number | null;
  storage_key: string | null;     // canonical nirspec_rate key for the FITS proxy
  stage: string;
  review_status: 'pending' | 'approved' | 'excluded';
  mask_regions: MaskRegionsPayload | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

// NIRSpec nods-renderer grid row (review loop P4/P5). One per canonical per-source
// spectrum-exposure; the web nods renderer groups these as rows=(exp_group, nod) ×
// cols=detector per source. Mirrors the supabase spectrum_exposures table. NB:
// exposure_root here is the 2-token pipeline root (nod split out) — different
// semantics from NirspecRateExposure.exposure_root (3 tokens).
export interface SpectrumExposure {
  id: number;
  observation: string;
  exposure_root: string;
  nod: string;
  detector: string;               // 'nrs1' | 'nrs2'
  source_id: number;
  exp_group: number | null;
  grating: string | null;
  filename: string;
  storage_key: string | null;     // canonical nirspec_spectrum_exposure key
  image_width: number | null;
  image_height: number | null;
  stage: string;
  review_status: 'pending' | 'approved' | 'excluded';
  notes: string | null;
  created_at: string;
  updated_at: string;
}

// Source-scoped editable review flags for the NIRSpec nods loop (P6). One row per
// (observation, exposure_root, source_id). Both jsonb channels mirror the local
// reference/nirspec/<obs>/ TOMLs 1:1: stuck_shutters is an ordinal list [1,2,3];
// bkg_overrides is {nod: [bkg nods]} keyed by exposure-sequence number (e.g. {"3":[1]}).
export interface NirspecSourceReview {
  id: number;
  observation: string;
  exposure_root: string;
  source_id: number;
  stuck_shutters: number[] | null;
  bkg_overrides: Record<string, number[]> | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

// One entry in observations.pointings — a NIRSpec MSA pointing.
export interface Pointing {
  msametid: number;
  msametfl: string;
  ra_center: number;
  dec_center: number;
  pa_aper: number;
  gratings: string[];
  filters: string[];
  jwst_program: number;
  jwst_obs_ids: string[];
  n_exposures: number;
  n_dithers: number;
  exptime_total: number;
  date_obs_start: string;
  date_obs_end: string;
  footprint: number[][][]; // 4 quadrants × 4 corners × [ra, dec]
}

// ============================================
// Frontend-specific Types
// ============================================

// Extended target with joined data for display
export interface SpectrumTarget extends DbTarget {
  program_name?: string;
  spectra: Spectrum[];
  max_snr?: number;
  max_exposure_time?: number;
  num_gratings?: number;
  comments?: CommentWithUser[];
  parent_object_id?: string;
  // Objects mode fields (populated when viewing objects)
  n_targets?: number;
  n_spectra?: number;
  programs?: string[];
  gratings?: string[];
  photo_z?: number | null;
  has_photometry?: boolean;
  member_targets?: { target_id: string; program_slug: string; observation: string; redshift_auto: number | null }[];
  lists?: { id: number; name: string; slug: string; icon: string | null; color: string | null }[];
  // Staleness fields surfaced in objects mode only.
  staleness_reason?: 'new_target' | 'reprocessed' | 'membership_changed' | 'migration_conflict' | null;
  last_data_change_at?: string | null;
  // Legacy shim — DQ flags now live per-spectrum (spectra[i].dq_flags);
  // a few UI surfaces still read a per-target summary.
  dq_flags?: number;
}

// Member targets are stateless provenance — inspection state lives on the
// parent object. The optional inspection fields are legacy shims for UI
// surfaces still reading per-target columns.
export interface ObjectMemberTarget {
  id: number;
  target_id: string;
  program_slug: string;
  program_name: string;
  observation: string;
  ra: number;
  dec: number;
  redshift_auto: number | null;
  max_snr: number | null;
  max_exposure_time: number | null;
  spectra: Spectrum[];
  // Legacy shims reflecting deprecated targets.* columns — not authoritative.
  redshift?: number | null;
  redshift_inspected?: number | null;
  redshift_quality?: number;
  dq_flags?: number;
  last_inspected_at?: string | null;
  last_inspected_by?: string | null;
}

// Photometry band measurement
export interface PhotometryBand {
  flux: number;
  flux_err: number;
  wav?: number;
  wav_min?: number;
  wav_max?: number;
}

// Photometric catalog cross-match data
export interface ObjectPhotometry {
  catalog_name: string;
  catalog_id: string;
  match_distance_arcsec: number;
  photometry: {
    flux_unit: string;
    bands: Record<string, PhotometryBand>;
  };
  photo_z: number | null;
  photo_z_err_lo: number | null;
  photo_z_err_hi: number | null;
  has_pz: boolean;
}

export interface ObjectDetail {
  id: number;
  object_id: string;
  field: string;
  ra: number;
  dec: number;
  n_targets: number;
  n_spectra: number;
  programs: string[];
  gratings: string[];
  max_snr: number | null;
  max_exposure_time: number | null;
  redshift: number | null;
  redshift_quality: number;
  redshift_inspected: number | null;
  redshift_auto: number | null;
  // True when redshift_inspected was auto-pinned from redshift_auto at sign-off
  // (inspector accepted the auto-fit rather than typing a number). The UI
  // suppresses the "(overridden)" hint and shows an empty override input when
  // this is true. False for explicit user-typed overrides and for
  // uninspected/impossible rows.
  inspected_used_auto: boolean;
  last_inspected_at: string | null;
  last_inspected_by: string | null;
  last_data_change_at: string | null;
  staleness_reason: 'new_target' | 'reprocessed' | 'membership_changed' | 'migration_conflict' | null;
  version: number;
  is_active: boolean;
  photo_z: number | null;
  photo_z_err_lo: number | null;
  photo_z_err_hi: number | null;
  has_photometry: boolean;
  created_at: string;
  member_targets: ObjectMemberTarget[];
  photometry: ObjectPhotometry | null;
}

// Comment with user profile info
export interface CommentWithUser extends Comment {
  user_profile?: UserProfile;
}

export interface FilterState {
  programs: string[];
  fields: string[];
  gratings: string[];
  redshift_quality: number[];
  snr_range: [number, number];
  flags: number[];
}

// ============================================
// Profile Stats & Activity Types
// ============================================

export interface ProfileStats {
  targets_inspected: number;
  comments_posted: number;
  last_activity: string | null;
}

export interface CommentHistoryItem {
  id: number;
  content: string;
  created_at: string;
  edited_at: string | null;
  target_db_id: number | null;
  target_display_id: string | null;
  object_db_id: number | null;
  object_display_id: string | null;
}

export interface ProfileRecentComments {
  items: CommentHistoryItem[];
  total_count: number;
}

// ============================================
// Constants
// ============================================

// Derived from REDSHIFT_QUALITY so the two exports can't drift apart.
// Consumers of QUALITY_LABELS expect the FlagDefinition shape (`short_label`,
// nullable fields); REDSHIFT_QUALITY uses `short` with non-nullable fields.
export const QUALITY_LABELS: FlagDefinition[] = REDSHIFT_QUALITY.map(q => ({
  category: 'redshift_quality',
  bit_position: null,
  value: q.value,
  label: q.label,
  short_label: q.short,
  icon: q.icon,
  color: q.color,
  description: q.description,
}));

// Map-marker fill/stroke color per redshift_quality (0–4), shared by the Leaflet
// (CanvasMarkerLayer) and FitsGL (FitsGLMapSurface) marker renderers so both map
// engines color objects identically.
export const MARKER_QUALITY_COLORS: Record<number, string> = {
  0: '#9ca3af', // Not inspected - gray
  1: '#ef4444', // Impossible - red
  2: '#f97316', // Tentative - orange
  3: '#f59e0b', // Probable - amber
  4: '#22c55e', // Secure - green
};

export const GRATINGS = ['PRISM', 'G140H', 'G140M', 'G235H', 'G235M', 'G395H', 'G395M'] as const;

// D3 category10 palette for coloring member targets in object detail views
export const MEMBER_COLORS = [
  '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
  '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
];
export type Grating = typeof GRATINGS[number];

// Helper to get flag definition by category and value
export function getFlagDefinition(category: string, value: number): FlagDefinition | undefined {
  if (category === 'redshift_quality') {
    return QUALITY_LABELS.find(f => f.value === value);
  }
  return undefined;
}

// Helper to decode bitmask flags
export function decodeBitmaskFlags(bitmask: number, category: string, allFlags: FlagDefinition[]): FlagDefinition[] {
  return allFlags.filter(
    flag => flag.category === category && flag.bit_position !== null && (bitmask & flag.value) > 0
  );
}

// ============================================
// Activity Feed Types
// ============================================

export type ActivityType = 'comment' | 'inspection';

export interface BaseActivity {
  id: string;                    // "comment-{id}" or "audit-{id}"
  type: ActivityType;
  target_db_id: number;
  target_display_id: string;     // e.g., "ember_uds_p4_123456"
  user_id: string;
  timestamp: string;
  user_profile?: UserProfile;
}

export interface CommentActivity extends BaseActivity {
  type: 'comment';
  content: string;
  edited_at: string | null;
}

export interface InspectionActivity extends BaseActivity {
  type: 'inspection';
  field_name: string;
  old_value: number | null;
  new_value: number | null;
}

export type Activity = CommentActivity | InspectionActivity;

export interface ActivityUser {
  user_id: string;
  full_name: string;
}

export interface ActivityFeedResponse {
  activities: Activity[];
  total_count: number;
  page: number;
  page_size: number;
  has_next_page: boolean;
  available_users: ActivityUser[];
}

// Helper functions for activity formatting
export function formatActivityField(fieldName: string, value: number | null): string {
  if (value === null) return 'none';

  switch (fieldName) {
    case 'redshift_quality':
      const quality = QUALITY_LABELS.find(q => q.value === value);
      return quality ? `${quality.icon} ${quality.label}` : `${value}`;

    case 'redshift_inspected':
      return value.toFixed(4);

    case 'dq_flags':
      // Bitmask — display the numeric value; the badge UI decodes per-bit
      return `${value}`;

    default:
      return `${value}`;
  }
}

export function formatFieldName(fieldName: string): string {
  const names: Record<string, string> = {
    'redshift_quality': 'Redshift Quality',
    'redshift_inspected': 'Redshift (Manual)',
    'dq_flags': 'Data Quality',
  };
  return names[fieldName] || fieldName;
}
