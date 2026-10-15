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
 * `has_1d` / `has_zfit` / `has_lines` say whether the `_spec_1d.json`
 * sidecar / the zfit JSON / the line-fit JSON (`_lines.json`, the emission-line
 * model behind the plot's "Lines" toggle) is a registered object of its own,
 * independent of the front: true = it is; false = definitively absent (a
 * spectrum that predates the 1-D sidecar answers the 1-D query with its full
 * payload, so a second download is waste; a spectrum with no redshift fit or
 * no line fit needs no round trip to learn that); null = the registry did not
 * answer (the client fetches / falls back to be safe).
 */
export interface SpectrumSidecarUrls {
  front: boolean;
  spectrum: string | null;
  spectrum_1d: string | null;
  zfit: string | null;
  lines: string | null;
  /** Whether the spectrum's own (full) JSON is a registered object: under a
   *  release, whether its archived copy exists (the plot's payload when the
   *  1-D sidecar is absent or the front is off). Optional for older callers. */
  has_spectrum?: boolean | null;
  has_1d: boolean | null;
  has_zfit: boolean | null;
  has_lines: boolean | null;
}

/** The "front off / nothing known" answer: every fetch goes to the app routes. */
export const NO_FRONT: SpectrumSidecarUrls = {
  front: false, spectrum: null, spectrum_1d: null, zfit: null, lines: null,
  has_1d: null, has_zfit: null, has_lines: null,
};

// TanStack keys, keyed on the FITS path — what is fetched, never the viewer
// (the QueryClient is cleared on sign-out). Under a data release (P6c-2) the
// release tag is part of the key: the same path resolves to the release's
// archived copy, not the live file, so the two must never share an entry.
type SidecarKey<P extends string> = readonly [P, string] | readonly [P, string, string];
function keyed<P extends string>(prefix: P, fitsPath: string, dr?: string | null): SidecarKey<P> {
  return dr ? ([prefix, fitsPath, dr] as const) : ([prefix, fitsPath] as const);
}
export const spectrumSidecarsKey = (fitsPath: string, dr?: string | null) => keyed('spectrum-sidecars', fitsPath, dr);
export const spectrumJsonKey = (fitsPath: string, dr?: string | null) => keyed('spectrum-json', fitsPath, dr);
export const spectrum1dKey = (fitsPath: string, dr?: string | null) => keyed('spectrum-1d', fitsPath, dr);
export const redshiftFitKey = (fitsPath: string, dr?: string | null) => keyed('redshift-fit', fitsPath, dr);
export const lineFitKey = (fitsPath: string, dr?: string | null) => keyed('line-fit', fitsPath, dr);

/** `url` with `dr=<tag>` appended (no-op without a tag). Same rule as
 * lib/fetch-json's withDrParam; duplicated here so this module stays free
 * of client-only imports for the server render. */
export function sidecarRouteUrl(url: string, dr?: string | null): string {
  if (!dr) return url;
  return `${url}${url.includes('?') ? '&' : '?'}dr=${encodeURIComponent(dr)}`;
}

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
  dr?: string | null,
): { front: string | null; route: string } {
  const u = urls ?? NO_FRONT;
  return {
    front: u.front ? (u.spectrum_1d ?? u.spectrum) : null,
    route: sidecarRouteUrl(`/api/spectrum?path=${encodeURIComponent(fitsPath)}&include=1d`, dr),
  };
}
