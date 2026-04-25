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

export type AccentColorName = 'magenta' | 'blue' | 'emerald' | 'red' | 'orange' | 'violet' | 'cyan' | 'lime';

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
  { name: 'magenta', label: 'Magenta', light: '#c026d3', dark: '#e879f9', hover: { light: '#a21caf', dark: '#f0abfc' } },
  { name: 'blue', label: 'Blue', light: '#2563eb', dark: '#60a5fa', hover: { light: '#1d4ed8', dark: '#93c5fd' } },
  { name: 'emerald', label: 'Emerald', light: '#059669', dark: '#34d399', hover: { light: '#047857', dark: '#6ee7b7' } },
  { name: 'red', label: 'Red', light: '#dc2626', dark: '#f87171', hover: { light: '#b91c1c', dark: '#fca5a5' } },
  { name: 'orange', label: 'Orange', light: '#ea580c', dark: '#fb923c', hover: { light: '#c2410c', dark: '#fdba74' } },
  { name: 'violet', label: 'Violet', light: '#7c3aed', dark: '#a78bfa', hover: { light: '#6d28d9', dark: '#c4b5fd' } },
  { name: 'cyan', label: 'Cyan', light: '#0891b2', dark: '#22d3ee', hover: { light: '#0e7490', dark: '#67e8f9' } },
  { name: 'lime', label: 'Lime', light: '#65a30d', dark: '#a3e635', hover: { light: '#4d7c0f', dark: '#bef264' } },
];

export const DEFAULT_ACCENT_COLOR: AccentColorName = 'magenta';

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
  reduction_version: string;
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
  version: string;
  extension: string;  // sci, err, rms, srcmask
  file_path: string;
  file_size?: number; // in bytes, if available
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
  hasSedPlot?: boolean;
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
  has_sed_plot: boolean;
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
