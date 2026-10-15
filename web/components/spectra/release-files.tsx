'use client';

import React from 'react';

/**
 * Which of an object's files the browsed release holds archived (P6c-2).
 * The object page's server render resolves the release manifest once and
 * provides the two sets; a member whose copy is not archived yet has no plot
 * and no download (the control is omitted, nothing explains why). Null =
 * the live catalog, where every file is served from its live key.
 */
export interface ReleaseFiles {
  /** fits_paths whose spectrum JSON (the plot payload) is archived. */
  plottable: string[];
  /** fits_paths whose FITS itself is archived. */
  downloadable: string[];
}

const ReleaseFilesContext = React.createContext<ReleaseFiles | null>(null);

export function ReleaseFilesProvider({ value, children }: { value: ReleaseFiles | null; children: React.ReactNode }) {
  return <ReleaseFilesContext.Provider value={value}>{children}</ReleaseFilesContext.Provider>;
}

export function useReleaseFiles(): ReleaseFiles | null {
  return React.useContext(ReleaseFilesContext);
}

/** Whether one spectrum's plot payload and FITS are available to this viewer. */
export function useFileAvailability(fitsPath: string): { plot: boolean; download: boolean } {
  const files = useReleaseFiles();
  if (!files) return { plot: true, download: true };
  return { plot: files.plottable.includes(fitsPath), download: files.downloadable.includes(fitsPath) };
}
