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
  { value: 2, label: 'Tentative', short: 'Tent.', icon: '🟠', color: '#ff9800', description: 'Redshift uncertain but plausible (~50% confidence)' },
  { value: 3, label: 'Probable', short: 'Prob.', icon: '🟡', color: '#ffc107', description: 'Redshift likely correct (~80% confidence)' },
  { value: 4, label: 'Secure', short: 'Secure', icon: '🟢', color: '#28a745', description: 'Redshift definitely correct (>95% confidence)' },
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

// Per-line flags of the emission-line catalog (spectrum_line_fits.lines[<line>].flags,
// spectrum_lines.flags) — set by `cfpipe nirspec linefit`; mirrors
// campfire.flags.LineFlags / linefit.FLAG_* (docs/design-emission-line-fitting.md).
export const LINE_FLAGS: FlagDef[] = [
  { key: 'tied', bit: 0, value: 1, label: 'Tied', short: 'TIED', icon: '🔗', color: '#e0e0e0', description: 'Flux ratio-tied to its doublet primary ([OIII], [NII], [OI])' },
  { key: 'blended', bit: 1, value: 2, label: 'Blended', short: 'BLENDED', icon: '⊂', color: '#e0e0e0', description: 'Unresolved at this resolution: folded into a blend primary, no flux of its own' },
  { key: 'blend', bit: 2, value: 4, label: 'Blend', short: 'BLEND', icon: '⊃', color: '#ffe0b2', description: 'This flux includes blended companions' },
  { key: 'edge', bit: 3, value: 8, label: 'Edge', short: 'EDGE', icon: '⇤', color: '#fff9c4', description: 'Fit window truncated by the valid wavelength range' },
  { key: 'kin_global', bit: 4, value: 16, label: 'Global kinematics', short: 'KIN', icon: '↔', color: '#e0e0e0', description: 'Velocity offset and width fixed to the spectrum\'s global (anchor) kinematics' },
  { key: 'kin_default', bit: 5, value: 32, label: 'Default kinematics', short: 'KIN0', icon: '↔', color: '#e0e0e0', description: 'Velocity offset and width fixed to defaults (no line anchored the kinematics)' },
  { key: 'broad', bit: 6, value: 64, label: 'Broad', short: 'BROAD', icon: '⌒', color: '#f3e5f5', description: 'A broad component was accepted on this line (<line>_broad carries it)' },
  { key: 'no_continuum', bit: 7, value: 128, label: 'No continuum', short: 'NOCONT', icon: '—', color: '#e0e0e0', description: 'Continuum undetected at the line: no equivalent width' },
  { key: 'fit_failed', bit: 8, value: 256, label: 'Fit failed', short: 'FAIL', icon: '⚠️', color: '#ffcdd2', description: 'Nonlinear refinement failed; the grid / linear solution was kept' },
  { key: 'masked', bit: 9, value: 512, label: 'Masked', short: 'MASK', icon: '▒', color: '#ffecb3', description: 'More than 30% of the window pixels were masked' },
  { key: 'sigma_unresolved', bit: 10, value: 1024, label: 'Width unresolved', short: 'σ?', icon: '≈', color: '#e0e0e0', description: 'Intrinsic width not constrained by the LSF' },
  { key: 'resolved', bit: 11, value: 2048, label: 'Resolved doublet', short: 'RES', icon: '✓', color: '#c8e6c9', description: 'Doublet total whose members were fit as separate components (each carries its own flux)' },
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
