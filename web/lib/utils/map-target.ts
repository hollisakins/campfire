/**
 * The map's pinned-target URL contract (`/map?target=<ra>,<dec>`).
 *
 * The go-to tool pins a sky-locked crosshair at a coordinate; `target=` makes that
 * position shareable. It is deliberately NOT the existing `ra`/`dec` pair: those
 * mean the view CENTRE and are rewritten continuously by the debounced pan/zoom
 * sync, so they cannot also carry a fixed mark. Both are read on load — a link can
 * centre somewhere and pin somewhere else.
 *
 * Parsing and formatting live here (pure, no React/DOM) because the server page
 * reads the param and the client surface writes it; one definition keeps the two
 * ends from drifting. Any new map param must also be added to `mapParamKeys` in
 * `app/map/MapPageContent.tsx`, or the filter sync deletes it.
 */

export interface MapTarget {
  /** ICRS right ascension, degrees in [0, 360). */
  ra: number;
  /** ICRS declination, degrees in [-90, 90]. */
  dec: number;
}

/** Decimal places kept in the URL: ~0.36 mas, far finer than any pixel scale. */
const TARGET_DECIMALS = 6;

/**
 * Serialize a pinned target for the `target=` param. Rounded rather than full
 * precision so the URL stays readable and a pan-triggered rewrite is byte-stable.
 */
export function formatTargetParam(target: MapTarget): string {
  return `${target.ra.toFixed(TARGET_DECIMALS)},${target.dec.toFixed(TARGET_DECIMALS)}`;
}

/**
 * Parse a `target=` param value, or null when absent/malformed. Never throws:
 * a hand-edited or stale link degrades to "no pinned target", never an error page.
 *
 * Accepts decimal degrees only (`"150.1,2.5"`), comma- or whitespace-separated —
 * this is a machine-written param, not the user-facing input box, which takes
 * free-form sexagesimal through FitsGL's `parseSkyCoord`.
 */
export function parseTargetParam(value: string | string[] | undefined): MapTarget | null {
  if (typeof value !== 'string') return null;
  const parts = value.trim().split(/[\s,]+/).filter((p) => p.length > 0);
  if (parts.length !== 2) return null;
  const ra = Number(parts[0]);
  const dec = Number(parts[1]);
  if (!Number.isFinite(ra) || !Number.isFinite(dec)) return null;
  if (ra < 0 || ra >= 360) return null;
  if (dec < -90 || dec > 90) return null;
  return { ra, dec };
}
