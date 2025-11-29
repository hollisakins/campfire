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
}

export interface AccessCode {
  id: string;
  code: string;
  description: string | null;
  grants_all_programs: boolean;
  program_ids: number[] | null;
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
  program_id: number;
  program_name: string | null;
  pi_name: string | null;
  description: string | null;
  is_public: boolean;
  created_at: string;
}

export interface UserProgramAccess {
  user_id: string;
  program_id: number;
  granted_at: string;
  granted_by: string | null;
}

export interface DbObject {
  id: number;
  object_id: string;
  program_id: number;
  field: string;
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
  created_at: string;
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
  num_gratings?: number;
  comments?: CommentWithUser[];
  hasSedPlot?: boolean;
}

// Comment with user profile info
export interface CommentWithUser extends Comment {
  user_profile?: UserProfile;
}

export interface FilterState {
  programs: number[];
  fields: string[];
  gratings: string[];
  redshift_quality: number[];
  snr_range: [number, number];
  flags: number[];
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

export const GRATINGS = ['PRISM', 'G140M', 'G235M', 'G395M'] as const;
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
