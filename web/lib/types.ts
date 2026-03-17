// TypeScript type definitions for CAMPFIRE
// Matches the Supabase database schema

// ============================================
// Database Tables
// ============================================

export interface UserProfile {
  user_id: string;
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

export interface DbObject {
  id: number;
  object_id: string;
  program_slug: string;
  field: string;
  observation: string;
  ra: number;
  dec: number;
  redshift: number | null;           // Computed: COALESCE(redshift_inspected, redshift_auto)
  redshift_auto: number | null;      // From pipeline
  redshift_inspected: number | null; // Manual override
  redshift_quality: number;
  spectral_features: number;
  object_flags: number;
  dq_flags: number;
  last_inspected_at: string | null;
  last_inspected_by: string | null;
  created_at: string;
  updated_at: string;
  distance?: number | null;          // Only present when coordinate search is active (in degrees)
}

export interface Spectrum {
  id: number;
  object_id: string;  // FK to objects.object_id (text)
  grating: string;
  fits_path: string;
  reduction_version: string;
  signal_to_noise: number | null;
  exposure_time: number | null;
  created_at: string;
  // Pre-generated SVG thumbnails (included when p_include_thumbnails=true in RPC)
  thumbnail_svg_fnu?: string | null;
  thumbnail_svg_flambda?: string | null;
}

export interface Comment {
  id: number;
  object_id: number;
  user_id: string;
  content: string;
  created_at: string;
  edited_at: string | null;
  is_deleted: boolean;
}

export interface FlagAuditLog {
  id: number;
  object_id: number;
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

// Extended object with joined data for display
export interface SpectrumObject extends DbObject {
  program_name?: string;
  spectra: Spectrum[];
  max_snr?: number;
  max_exposure_time?: number;
  num_gratings?: number;
  comments?: CommentWithUser[];
  hasSedPlot?: boolean;
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
  objects_inspected: number;
  comments_posted: number;
  last_activity: string | null;
}

export interface CommentHistoryItem {
  id: number;
  content: string;
  created_at: string;
  edited_at: string | null;
  object_db_id: number;
  object_display_id: string;
}

export interface ProfileRecentComments {
  items: CommentHistoryItem[];
  total_count: number;
}

// ============================================
// Constants
// ============================================

export const QUALITY_LABELS: FlagDefinition[] = [
  { category: 'redshift_quality', bit_position: null, value: 0, label: 'Not Inspected', short_label: 'None', icon: '⚪', color: '#e0e0e0', description: 'Not yet visually inspected' },
  { category: 'redshift_quality', bit_position: null, value: 1, label: 'Impossible', short_label: 'Bad', icon: '🔴', color: '#dc3545', description: 'Impossible to determine redshift from available data' },
  { category: 'redshift_quality', bit_position: null, value: 2, label: 'Tentative', short_label: 'Tent.', icon: '🟠', color: '#ffc107', description: 'Redshift uncertain but plausible (~50% confidence)' },
  { category: 'redshift_quality', bit_position: null, value: 3, label: 'Probable', short_label: 'Prob.', icon: '🟡', color: '#ff9800', description: 'Redshift likely correct (~80% confidence)' },
  { category: 'redshift_quality', bit_position: null, value: 4, label: 'Secure', short_label: 'Secure', icon: '🟢', color: '#28a745', description: 'Redshift definitely correct (>95% confidence)' },
];

export const GRATINGS = ['PRISM', 'G140H', 'G140M', 'G235H', 'G235M', 'G395H', 'G395M'] as const;
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
  object_db_id: number;
  object_display_id: string;     // e.g., "ember_uds_p4_123456"
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

    // For bitmask fields, just show the numeric value (decoding would be complex)
    case 'spectral_features':
    case 'object_flags':
    case 'dq_flags':
      return `${value}`;

    default:
      return `${value}`;
  }
}

export function formatFieldName(fieldName: string): string {
  const names: Record<string, string> = {
    'redshift_quality': 'Redshift Quality',
    'redshift_inspected': 'Redshift (Manual)',
    'spectral_features': 'Spectral Features',
    'object_flags': 'Object Flags',
    'dq_flags': 'Data Quality',
  };
  return names[fieldName] || fieldName;
}
