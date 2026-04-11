/**
 * Flag definitions for NIRSpec visual inspection and quality assessment.
 * Converted from old/static/config/inspection_flags.json
 */

export interface FlagDef {
  key: string;
  bit: number;
  value: number;
  label: string;
  short: string;
  icon: string;
  color: string;
  description: string;
}

export interface QualityDef {
  value: number;
  label: string;
  short: string;
  icon: string;
  color: string;
  description: string;
}

// Redshift quality options (0-4 enum, not bitmask)
export const REDSHIFT_QUALITY: QualityDef[] = [
  { value: 0, label: 'Not Inspected', short: 'None', icon: '⚪', color: '#e0e0e0', description: 'Not yet visually inspected' },
  { value: 1, label: 'Impossible', short: 'Bad', icon: '🔴', color: '#dc3545', description: 'Impossible to determine redshift from available data' },
  { value: 2, label: 'Tentative', short: 'Tent.', icon: '🟠', color: '#ffc107', description: 'Redshift uncertain but plausible (~50% confidence)' },
  { value: 3, label: 'Probable', short: 'Prob.', icon: '🟡', color: '#ff9800', description: 'Redshift likely correct (~80% confidence)' },
  { value: 4, label: 'Secure', short: 'Secure', icon: '🟢', color: '#28a745', description: 'Redshift definitely correct (>95% confidence)' },
];

// Spectral features used for redshift determination (bitmask)
export const SPECTRAL_FEATURES: FlagDef[] = [
  { key: 'continuum_break', bit: 0, value: 1, label: 'Continuum Shape', short: 'Cont', icon: '📊', color: '#e8f5e9', description: 'Redshift constrained by the overall continuum shape' },
  { key: 'lyman_break', bit: 1, value: 2, label: 'Lyman Break', short: 'LB', icon: '💧', color: '#e3f2fd', description: 'Clear Lyman break' },
  { key: 'balmer_break', bit: 2, value: 4, label: 'Balmer Break', short: 'BB', icon: '📈', color: '#f3e5f5', description: 'Clear Balmer break' },
  { key: 'absorption_features', bit: 3, value: 8, label: 'Absorption Features', short: 'ABS', icon: '〰️', color: '#f1f8e9', description: 'Absorption lines/features identified' },
  { key: 'single_emission', bit: 4, value: 16, label: 'Single Emission Line', short: '1EM', icon: '☝️', color: '#fff3e0', description: 'Single emission line' },
  { key: 'multi_emission', bit: 5, value: 32, label: 'Multiple Emission Lines', short: 'MEM', icon: '✌️', color: '#ffebee', description: 'Multiple emission lines' },
];

// Data quality issues (bitmask)
export const DQ_FLAGS: FlagDef[] = [
  { key: 'chip_gap', bit: 0, value: 1, label: 'Chip Gap', short: 'GAP', icon: '⚠️', color: '#fff9c4', description: 'Spectrum affected by detector chip gap' },
  { key: 'contamination', bit: 1, value: 2, label: 'Contamination', short: 'CONTAM', icon: '🚫', color: '#ffe0b2', description: 'Contamination from nearby source or open shutter' },
  { key: 'stuck_shutter', bit: 2, value: 4, label: 'Stuck Closed Shutter', short: 'CLOSED', icon: '🔒', color: '#ffcdd2', description: 'Possible stuck closed shutter' },
  { key: 'multiple_sources', bit: 3, value: 8, label: 'Multiple Sources', short: 'MULT', icon: '👥', color: '#b3e5fc', description: 'Multiple sources in slitlet' },
  { key: 'no_detection', bit: 4, value: 16, label: 'No Detection', short: 'NONE', icon: '❌', color: '#e0e0e0', description: 'No source detected in spectrum' },
  { key: 'low_snr', bit: 5, value: 32, label: 'Low S/N', short: 'SNR', icon: '📉', color: '#ffecb3', description: 'Low signal-to-noise ratio' },
  { key: 'spectral_overlap', bit: 6, value: 64, label: 'Spectral Overlap', short: 'OVER', icon: '🔗', color: '#f3e5f5', description: 'Spectral overlap in grating spectrum' },
  { key: 'prism_corrupted', bit: 7, value: 128, label: 'PRISM Corrupted', short: 'P-BAD', icon: '🌈❌', color: '#ffccbc', description: 'PRISM data corrupted or unusable' },
  { key: 'grating_corrupted', bit: 8, value: 256, label: 'Grating Corrupted', short: 'G-BAD', icon: '🔴❌', color: '#ffcdd2', description: 'Grating data corrupted or unusable' },
];

// Helper functions

/**
 * Decode a bitmask into an array of flag values
 */
export function decodeBitmask(bitmask: number, flags: FlagDef[]): number[] {
  return flags
    .filter(flag => (bitmask & flag.value) !== 0)
    .map(flag => flag.value);
}

/**
 * Encode an array of flag values into a bitmask
 */
export function encodeBitmask(values: (string | number)[]): number {
  return values.reduce<number>((bitmask, value) => bitmask | (typeof value === 'number' ? value : 0), 0);
}

/**
 * Get quality definition by value
 */
export function getQualityDef(value: number): QualityDef {
  return REDSHIFT_QUALITY.find(q => q.value === value) || REDSHIFT_QUALITY[0];
}

/**
 * Get flag definition by key
 */
export function getFlagDef(key: string, flags: FlagDef[]): FlagDef | undefined {
  return flags.find(f => f.key === key);
}

/**
 * Get contrasting text color for a background color
 */
export function getContrastColor(bgColor: string): string {
  const hex = bgColor.replace('#', '');
  const r = parseInt(hex.substr(0, 2), 16);
  const g = parseInt(hex.substr(2, 2), 16);
  const b = parseInt(hex.substr(4, 2), 16);
  const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return luminance > 0.5 ? '#000000' : '#ffffff';
}
