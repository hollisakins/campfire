/**
 * Spectrum sidecar addressing shared by the server (the object page render,
 * the /api/spectrum/sidecars route) and the client (lib/hooks/useSpectrumJson).
 * No directive on purpose: both sides import it.
 */

/**
 * Where a spectrum's sidecars are served from. `front: true` => every url is
 * on the delivery front (CORS-readable, content-addressed; null = the object
 * is not deployed, e.g. no redshift fit). `front: false` => the front is not
 * configured and the client fetches /api/spectrum and /api/redshift-fit
 * instead, which stream the bytes.
 *
 * `has_1d` / `has_zfit` say whether the `_spec_1d.json` sidecar / the zfit
 * JSON is a registered object of its own, independent of the front: true =
 * it is; false = definitively absent (a spectrum that predates the 1-D
 * sidecar answers the 1-D query with its full payload, so a second download
 * is waste; a spectrum with no redshift fit needs no /api/redshift-fit round
 * trip to learn that); null = the registry did not answer (the client fetches
 * / falls back to be safe).
 */
export interface SpectrumSidecarUrls {
  front: boolean;
  spectrum: string | null;
  spectrum_1d: string | null;
  zfit: string | null;
  has_1d: boolean | null;
  has_zfit: boolean | null;
}

/** The "front off / nothing known" answer: every fetch goes to the app routes. */
export const NO_FRONT: SpectrumSidecarUrls = {
  front: false, spectrum: null, spectrum_1d: null, zfit: null, has_1d: null, has_zfit: null,
};

// TanStack keys, keyed on the FITS path — what is fetched, never the viewer
// (the QueryClient is cleared on sign-out).
export const spectrumSidecarsKey = (fitsPath: string) => ['spectrum-sidecars', fitsPath] as const;
export const spectrumJsonKey = (fitsPath: string) => ['spectrum-json', fitsPath] as const;
export const spectrum1dKey = (fitsPath: string) => ['spectrum-1d', fitsPath] as const;
export const redshiftFitKey = (fitsPath: string) => ['redshift-fit', fitsPath] as const;

/**
 * Where the 1-D payload comes from. `front`: the front url when the front is
 * on — the 1-D sidecar, or the full JSON (a superset) for a spectrum deployed
 * before the sidecar existed — else null (a null front url is never read as
 * absence: the front may be off, the registry row not active yet, the presign
 * failed). `route`: the app route that streams the same bytes, which is also
 * the fallback when the front answer fails. The object page preloads one of
 * these from the HTML head; the client fetches the same url, so the browser
 * matches the two.
 */
export function spectrum1dSources(
  urls: SpectrumSidecarUrls | null | undefined,
  fitsPath: string,
): { front: string | null; route: string } {
  const u = urls ?? NO_FRONT;
  return {
    front: u.front ? (u.spectrum_1d ?? u.spectrum) : null,
    route: `/api/spectrum?path=${encodeURIComponent(fitsPath)}&include=1d`,
  };
}
